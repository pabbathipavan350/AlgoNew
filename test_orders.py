# ============================================================
# TEST_ORDERS.PY — Kotak order status / fill / cancel test
# ============================================================
# What this script does:
#   1. Places ONE aggressive limit BUY order
#   2. Polls order status using order_history + order_report fallback
#   3. Reads filled quantity / pending quantity / average price
#   4. Cancels the remaining quantity if order is still open / partial
#   5. Shows the final snapshot after cancellation
#
# Use this during market hours on a liquid option for a real fill test.
# After you confirm the BUY flow, you can manually square off the filled qty.
# ============================================================

import datetime
import os
import sys
import time


def _load_env(path=".env"):
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


_load_env()

import config
from auth import get_kotak_session
from option_manager import OptionManager, get_next_expiry

TEST_STRIKE = int(os.getenv("TEST_STRIKE", "0"))
TEST_OPTION_TYPE = os.getenv("TEST_OPTION_TYPE", "CE").strip().upper() or "CE"
TEST_QTY = int(os.getenv("TEST_QTY", "130"))
SEGMENT = config.FO_SEGMENT
PRODUCT = "NRML"
POLL_SECS = float(os.getenv("TEST_POLL_SECS", str(config.ORDER_STATUS_POLL_SECS)))
TIMEOUT_SECS = int(os.getenv("TEST_TIMEOUT_SECS", str(config.ORDER_FILL_TIMEOUT_SECS)))

def divider(title):
    print(f"\n{'=' * 68}")
    print(f"  {title}")
    print(f"{'=' * 68}")


def safe_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default


def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def extract_order_id(resp):
    if isinstance(resp, dict):
        return str(resp.get("nOrdNo") or resp.get("order_id") or resp.get("orderId") or "")
    return ""


def normalize_rows(resp):
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        data = resp.get("data") or resp.get("message") or []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    return []


def get_ltp(client, token):
    quotes = client.quotes(
        instrument_tokens=[{
            "instrument_token": token,
            "exchange_segment": SEGMENT,
        }],
        quote_type="ltp",
    )
    if isinstance(quotes, list) and quotes:
        return safe_float(quotes[0].get("last_price") or quotes[0].get("ltp"), 0.0)
    if isinstance(quotes, dict):
        data = quotes.get("message") or quotes.get("data") or quotes
        if isinstance(data, list) and data:
            return safe_float(data[0].get("last_price") or data[0].get("ltp"), 0.0)
        return safe_float(data.get("last_price") or data.get("ltp"), 0.0)
    return 0.0




def get_nifty_spot(client):
    try:
        resp = client.quotes(
            instrument_tokens=[{
                "instrument_token": "Nifty 50",
                "exchange_segment": config.CM_SEGMENT,
            }],
            quote_type="ltp",
        )
        data = resp if isinstance(resp, list) else (resp.get("message") or resp.get("data") or [])
        if data:
            return safe_float(data[0].get("ltp") or data[0].get("last_price"), 0.0)
    except Exception as e:
        print(f"  [info] spot fetch failed: {e}")
    return 0.0


def resolve_test_contract(client, opt_mgr, expiry_date):
    spot = get_nifty_spot(client)
    step = getattr(config, "STRIKE_STEP", 50)
    atm = int(round(spot / step) * step) if spot > 0 else 0

    strikes_to_try = []
    if TEST_STRIKE > 0:
        strikes_to_try.append(TEST_STRIKE)
    if atm > 0:
        for offset in [0, -step, step, -2*step, 2*step, -3*step, 3*step]:
            s = atm + offset
            if s not in strikes_to_try:
                strikes_to_try.append(s)

    for strike in strikes_to_try:
        token = opt_mgr.get_option_token(strike, TEST_OPTION_TYPE, expiry_date)
        symbol = opt_mgr.get_trading_symbol(strike, TEST_OPTION_TYPE, expiry_date)
        if token and symbol:
            return strike, token, symbol, spot, atm

    return None, None, None, spot, atm


def get_order_snapshot(client, order_id):
    snapshot = {}
    try:
        hist = client.order_history(order_id=order_id)
        rows = normalize_rows(hist)
        if rows:
            snapshot.update(rows[-1])
    except Exception as e:
        print(f"  [info] order_history failed: {e}")

    if not snapshot:
        try:
            rpt = client.order_report()
            rows = normalize_rows(rpt)
            for row in rows:
                row_id = str(row.get("nOrdNo") or row.get("order_id") or row.get("orderId") or "")
                if row_id == str(order_id):
                    snapshot = row
                    break
        except Exception as e:
            print(f"  [info] order_report fallback failed: {e}")

    status = str(snapshot.get("ordSt") or snapshot.get("stat") or snapshot.get("status") or "").lower()
    qty = safe_int(snapshot.get("qty") or snapshot.get("quantity") or snapshot.get("ordQty"), 0)
    filled_qty = safe_int(snapshot.get("fldQty") or snapshot.get("filled_quantity") or snapshot.get("fillQty"), 0)
    pending_qty = safe_int(snapshot.get("unFldSz") or snapshot.get("pending_quantity") or max(qty - filled_qty, 0), 0)
    avg_price = safe_float(snapshot.get("avgPrc") or snapshot.get("avg_price") or snapshot.get("average_price"), 0.0)
    rej_reason = snapshot.get("rejRsn") or snapshot.get("rejReason") or snapshot.get("rejMsg") or snapshot.get("remarks") or snapshot.get("message") or ""
    return {
        **snapshot,
        "status": status,
        "qty": qty,
        "filled_qty": filled_qty,
        "pending_qty": pending_qty,
        "avg_price": avg_price,
        "rej_reason": rej_reason,
    }


def print_snapshot(label, snap):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"  [{ts}] {label}")
    print(f"         status       : {snap.get('status') or '-'}")
    print(f"         qty          : {snap.get('qty')}")
    print(f"         filled_qty   : {snap.get('filled_qty')}")
    print(f"         pending_qty  : {snap.get('pending_qty')}")
    print(f"         avg_price    : {snap.get('avg_price')}")
    if snap.get("rej_reason"):
        print(f"         reason       : {snap.get('rej_reason')}")
    raw = str(snap)
    if len(raw) > 260:
        raw = raw[:260] + " ..."
    print(f"         raw          : {raw}")


def cancel_order_safely(client, order_id, amo):
    last_error = None
    for kwargs in (
        {"order_id": order_id, "amo": amo, "isVerify": True},
        {"order_id": order_id, "isVerify": True},
        {"order_id": order_id, "amo": amo},
        {"order_id": order_id},
    ):
        try:
            resp = client.cancel_order(**kwargs)
            return resp, kwargs
        except Exception as e:
            last_error = e
    raise RuntimeError(last_error)


def main():
    divider("CONNECTING TO KOTAK NEO")
    try:
        client = get_kotak_session()
        print("  ✅ Login successful")
        opt_mgr = OptionManager(client)
        expiry_date = get_next_expiry()
        test_strike, TEST_TOKEN, TEST_SYMBOL, spot, atm = resolve_test_contract(client, opt_mgr, expiry_date)
        if not TEST_TOKEN or not TEST_SYMBOL:
            print(f"  ❌ Could not resolve contract automatically for expiry {expiry_date}. spot={spot:.2f} atm={atm}")
            sys.exit(1)
        print(f"  Using upcoming weekly contract: {TEST_SYMBOL} | token={TEST_TOKEN} | expiry={expiry_date}")
        print(f"  Spot={spot:.2f} | ATM={atm} | chosen_strike={test_strike}")
    except Exception as e:
        print(f"  ❌ Login failed: {e}")
        sys.exit(1)

    divider("FETCHING CURRENT LTP")
    ltp = get_ltp(client, TEST_TOKEN)
    if ltp <= 0:
        ltp = 250.0
        print(f"  Could not fetch live LTP — using fallback Rs {ltp:.2f}")
    else:
        print(f"  Resolved contract LTP = Rs {ltp:.2f}")

    limit_buy_price = round(ltp + config.BUY_AGGRESSIVE_BUFFER, 2)
    now_t = datetime.datetime.now().time()
    amo_flag = "YES"

    print("\n  Test order inputs:")
    print(f"    symbol         : {TEST_SYMBOL}")
    print(f"    token          : {TEST_TOKEN}")
    print(f"    qty            : {TEST_QTY}")
    print(f"    ref_ltp        : Rs {ltp:.2f}")
    print(f"    buy_limit      : Rs {limit_buy_price:.2f}")
    print(f"    amo            : {amo_flag} (forced for test)")
    print(f"    poll_secs      : {POLL_SECS}")
    print(f"    timeout_secs   : {TIMEOUT_SECS}")

    divider("STEP 1 — PLACE AGGRESSIVE LIMIT BUY")
    try:
        resp = client.place_order(
            exchange_segment=SEGMENT,
            product=PRODUCT,
            trading_symbol=TEST_SYMBOL,
            transaction_type="B",
            quantity=str(TEST_QTY),
            order_type="L",
            price=str(limit_buy_price),
            validity="DAY",
            amo=amo_flag,
            disclosed_quantity="0",
            market_protection="0",
            pf="N",
            trigger_price="0",
            tag="TEST_FILL_CANCEL",
        )
    except Exception as e:
        print(f"  ❌ place_order failed: {e}")
        sys.exit(1)

    order_id = extract_order_id(resp)
    print(f"  place_order response: {resp}")
    print(f"  order_id            : {order_id or 'NOT FOUND'}")
    if not order_id:
        print("  ❌ No order id returned, cannot test fill status.")
        sys.exit(1)

    divider("STEP 2 — POLL STATUS / FILLED QTY")
    deadline = time.time() + max(TIMEOUT_SECS, 1)
    last_snap = {"qty": TEST_QTY, "filled_qty": 0, "pending_qty": TEST_QTY, "status": "unknown", "avg_price": 0.0}
    terminal = {"complete", "completed", "traded", "cancelled", "canceled", "rejected"}

    while time.time() < deadline:
        snap = get_order_snapshot(client, order_id)
        if snap:
            last_snap = snap
            print_snapshot("live snapshot", snap)
            if snap.get("status") in terminal or safe_int(snap.get("pending_qty"), TEST_QTY) <= 0:
                break
        time.sleep(POLL_SECS)

    divider("STEP 3 — CANCEL REMAINING QTY IF NEEDED")
    pending_qty = safe_int(last_snap.get("pending_qty"), max(TEST_QTY - safe_int(last_snap.get("filled_qty"), 0), 0))
    if pending_qty > 0 and last_snap.get("status") not in {"cancelled", "canceled", "rejected"}:
        try:
            cancel_resp, cancel_kwargs = cancel_order_safely(client, order_id, amo_flag)
            print(f"  cancel_order kwargs : {cancel_kwargs}")
            print(f"  cancel_order resp   : {cancel_resp}")
        except Exception as e:
            print(f"  ❌ cancel_order failed: {e}")
    else:
        print("  No pending quantity left to cancel.")

    time.sleep(1)
    final_snap = get_order_snapshot(client, order_id) or last_snap

    divider("FINAL RESULT")
    print_snapshot("final snapshot", final_snap)
    print("\n  Useful interpretation:")
    print("    - filled_qty > 0 means the order really executed for that quantity")
    print("    - pending_qty > 0 after timeout means the order was not fully executed")
    print("    - after cancel_order, pending quantity should become 0 or status should become cancelled")
    print("    - this is exactly the same logic now added into main.py for entry monitoring")


if __name__ == "__main__":
    main()
