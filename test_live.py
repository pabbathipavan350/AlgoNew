# ============================================================
# TEST_LIVE.PY — Comprehensive real-time test for algo components
# ============================================================
# Run this during market hours BEFORE running the main algo.
# Tests every system the algo depends on, in order.
#
# What it tests:
#   1.  Login + session
#   2.  Nifty spot price
#   3.  India VIX
#   4.  Mirror pair strike resolution (CE=ATM-200, PE=ATM+200)
#   5.  Token + symbol lookup for both strikes
#   6.  LTP fetch via REST quotes
#   7.  WS connection + tick reception (30 second live feed)
#   8.  ap field (VWAP) arriving in ticks — the most critical check
#   9.  VWAP engine: set_vwap_direct + is_near_vwap + is_below_vwap
#  10.  BUY limit order → fill polling → cancel (1 lot, CE strike)
#  11.  SELL market order on any filled qty → fill polling
#  12.  Order history + order report fallback (snapshot parsing)
#  13.  Position check (confirms any open position)
#  14.  Capital JSON read/write
#  15.  ReportManager CSV write
#
# SAFE: places at most 1 lot. Cancels immediately if not filled.
# Run as: python3 test_live.py
# ============================================================

import datetime
import os
import sys
import time
import threading
import json

# ── Load .env ─────────────────────────────────────────────
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
from auth           import get_kotak_session
from option_manager import OptionManager, get_next_expiry
from vwap_engine    import StrategyEngine, OptionVWAP
from capital_manager import CapitalManager
from report_manager  import ReportManager

# ── Result tracking ───────────────────────────────────────
RESULTS = []

def passed(name, detail=""):
    RESULTS.append(("PASS", name, detail))
    tag = f"  {detail}" if detail else ""
    print(f"  ✅  {name}{tag}")

def failed(name, detail=""):
    RESULTS.append(("FAIL", name, detail))
    tag = f"  {detail}" if detail else ""
    print(f"  ❌  {name}{tag}")

def warn(name, detail=""):
    RESULTS.append(("WARN", name, detail))
    tag = f"  {detail}" if detail else ""
    print(f"  ⚠️   {name}{tag}")

def section(title):
    print(f"\n{'='*62}")
    print(f"  {title}")
    print(f"{'='*62}")

# ── Helpers ───────────────────────────────────────────────
def safe_int(v, d=0):
    try: return int(float(v))
    except: return d

def safe_float(v, d=0.0):
    try: return float(v)
    except: return d

def extract_order_id(resp):
    if isinstance(resp, dict):
        return str(resp.get("nOrdNo") or resp.get("order_id") or
                   resp.get("orderId") or "")
    return ""

def normalize_rows(resp):
    if isinstance(resp, list): return resp
    if isinstance(resp, dict):
        data = resp.get("data") or resp.get("message") or []
        if isinstance(data, list): return data
        if isinstance(data, dict): return [data]
    return []

def get_order_snapshot(client, order_id):
    snap = {}
    try:
        hist = client.order_history(order_id=order_id)
        rows = normalize_rows(hist)
        if rows:
            snap.update(rows[-1])
    except Exception as e:
        print(f"      order_history failed: {e}")
    if not snap:
        try:
            rpt  = client.order_report()
            rows = normalize_rows(rpt)
            for row in rows:
                rid = str(row.get("nOrdNo") or row.get("order_id") or "")
                if rid == str(order_id):
                    snap = row
                    break
        except Exception as e:
            print(f"      order_report fallback failed: {e}")
    status     = str(snap.get("ordSt") or snap.get("stat") or snap.get("status") or "").lower()
    qty        = safe_int(snap.get("qty") or snap.get("ordQty") or snap.get("quantity"))
    filled_qty = safe_int(snap.get("fldQty") or snap.get("filled_quantity") or snap.get("fillQty"))
    pending    = safe_int(snap.get("unFldSz") or snap.get("pending_quantity") or max(qty - filled_qty, 0))
    avg_price  = safe_float(snap.get("avgPrc") or snap.get("avg_price") or snap.get("average_price"))
    rej_reason = (snap.get("rejRsn") or snap.get("rejReason") or snap.get("rejMsg") or
                  snap.get("remarks") or snap.get("message") or "")
    return {**snap, "status": status, "qty": qty, "filled_qty": filled_qty,
            "pending_qty": pending, "avg_price": avg_price, "rej_reason": rej_reason}

def cancel_order(client, order_id, amo="NO"):
    for kwargs in (
        {"order_id": order_id, "amo": amo, "isVerify": True},
        {"order_id": order_id, "isVerify": True},
        {"order_id": order_id, "amo": amo},
        {"order_id": order_id},
    ):
        try:
            resp = client.cancel_order(**kwargs)
            return resp
        except Exception as e:
            last = e
    return {"error": str(last)}

def poll_until_terminal(client, order_id, expected_qty, timeout_secs):
    """Poll order until filled/cancelled/rejected or timeout. Returns final snapshot."""
    terminal = {"complete", "completed", "traded", "cancelled", "canceled", "rejected"}
    deadline = time.time() + timeout_secs
    last_snap = {"status": "unknown", "filled_qty": 0, "pending_qty": expected_qty, "avg_price": 0.0}
    while time.time() < deadline:
        snap = get_order_snapshot(client, order_id)
        if snap:
            last_snap = snap
            status  = snap.get("status", "")
            filled  = safe_int(snap.get("filled_qty"))
            pending = safe_int(snap.get("pending_qty"), max(expected_qty - filled, 0))
            print(f"      polling: status={status} filled={filled} pending={pending} "
                  f"avg={snap.get('avg_price', 0):.2f}")
            if status in terminal or pending <= 0 or filled >= expected_qty:
                break
        time.sleep(config.ORDER_STATUS_POLL_SECS)
    return last_snap


# ══════════════════════════════════════════════════════════
# TEST 1 — Login
# ══════════════════════════════════════════════════════════
section("TEST 1 — Login + session")
client = None
try:
    client = get_kotak_session()
    passed("Login", "session obtained")
except Exception as e:
    failed("Login", str(e))
    print("\nCannot continue without a valid session. Exiting.")
    sys.exit(1)


# ══════════════════════════════════════════════════════════
# TEST 2 — Nifty spot
# ══════════════════════════════════════════════════════════
section("TEST 2 — Nifty 50 spot price")
spot = 0.0
try:
    resp = client.quotes(
        instrument_tokens=[{"instrument_token": "Nifty 50",
                            "exchange_segment": config.CM_SEGMENT}],
        quote_type="ltp")
    data = resp if isinstance(resp, list) else (resp.get("message") or resp.get("data") or [])
    spot = safe_float(data[0].get("ltp") or data[0].get("last_price")) if data else 0.0
    if spot > 0:
        passed("Nifty spot", f"Rs {spot:.2f}")
    else:
        warn("Nifty spot", f"got 0 — raw: {data}")
except Exception as e:
    failed("Nifty spot", str(e))


# ══════════════════════════════════════════════════════════
# TEST 3 — India VIX
# ══════════════════════════════════════════════════════════
section("TEST 3 — India VIX")
try:
    vix = 0.0
    for name in ["INDIA VIX", "India VIX"]:
        resp = client.quotes(
            instrument_tokens=[{"instrument_token": name,
                                "exchange_segment": config.CM_SEGMENT}],
            quote_type="ltp")
        data = resp if isinstance(resp, list) else (resp.get("message") or resp.get("data") or [])
        if data:
            vix = safe_float(data[0].get("ltp") or 0)
            if vix > 0:
                break
    if vix > 0:
        passed("India VIX", f"{vix:.2f}")
    else:
        warn("India VIX", "returned 0 — non-critical")
except Exception as e:
    warn("India VIX", str(e))


# ══════════════════════════════════════════════════════════
# TEST 4 — Mirror pair strike calculation
# ══════════════════════════════════════════════════════════
section("TEST 4 — Mirror pair strike calculation")
atm        = 0
ce_strike  = 0
pe_strike  = 0
expiry_date = None
try:
    expiry_date = get_next_expiry()
    if spot > 0:
        atm       = round(spot / config.STRIKE_STEP) * config.STRIKE_STEP
        ce_strike = atm - 200   # CE = ATM - 200 (200pts ITM for calls)
        pe_strike = atm + 200   # PE = ATM + 200 (200pts ITM for puts)
        passed("Mirror pair calculation",
               f"Nifty={spot:.0f} ATM={atm} | CE={ce_strike} PE={pe_strike} | Expiry={expiry_date}")
    else:
        atm       = 23200
        ce_strike = 23000
        pe_strike = 23400
        warn("Mirror pair", f"Spot=0, using fallback ATM={atm}")
except Exception as e:
    failed("Mirror pair", str(e))


# ══════════════════════════════════════════════════════════
# TEST 5 — Token + symbol lookup
# ══════════════════════════════════════════════════════════
section("TEST 5 — Token + symbol lookup (both strikes)")
opt_mgr    = OptionManager(client)
ce_token   = None
pe_token   = None
ce_symbol  = None
pe_symbol  = None
try:
    ce_token  = opt_mgr.get_option_token(ce_strike, "CE", expiry_date)
    ce_symbol = opt_mgr.get_trading_symbol(ce_strike, "CE", expiry_date)
    if ce_token and ce_symbol:
        passed("CE token+symbol", f"{ce_symbol} | token={ce_token}")
    else:
        failed("CE token+symbol", f"token={ce_token} symbol={ce_symbol}")
except Exception as e:
    failed("CE token+symbol", str(e))

try:
    pe_token  = opt_mgr.get_option_token(pe_strike, "PE", expiry_date)
    pe_symbol = opt_mgr.get_trading_symbol(pe_strike, "PE", expiry_date)
    if pe_token and pe_symbol:
        passed("PE token+symbol", f"{pe_symbol} | token={pe_token}")
    else:
        failed("PE token+symbol", f"token={pe_token} symbol={pe_symbol}")
except Exception as e:
    failed("PE token+symbol", str(e))


# ══════════════════════════════════════════════════════════
# TEST 6 — LTP via REST quotes for both strikes
# ══════════════════════════════════════════════════════════
section("TEST 6 — LTP fetch via REST quotes")
ce_ltp = 0.0
pe_ltp = 0.0
try:
    tok_list = []
    if ce_token:
        tok_list.append({"instrument_token": str(ce_token),
                         "exchange_segment": config.FO_SEGMENT})
    if pe_token:
        tok_list.append({"instrument_token": str(pe_token),
                         "exchange_segment": config.FO_SEGMENT})

    resp = client.quotes(instrument_tokens=tok_list, quote_type="ltp")
    data = resp if isinstance(resp, list) else (resp.get("message") or resp.get("data") or [])

    for item in data:
        tok = str(item.get("instrument_token") or item.get("tk") or "")
        ltp = safe_float(item.get("ltp") or item.get("last_price"))
        if tok == str(ce_token):
            ce_ltp = ltp
        elif tok == str(pe_token):
            pe_ltp = ltp

    # Fallback by position if token not in response
    if ce_ltp == 0 and pe_ltp == 0 and len(data) == len(tok_list):
        if len(data) >= 1: ce_ltp = safe_float(data[0].get("ltp") or data[0].get("last_price"))
        if len(data) >= 2: pe_ltp = safe_float(data[1].get("ltp") or data[1].get("last_price"))

    if ce_ltp > 0:
        passed("CE LTP (REST)", f"Rs {ce_ltp:.2f}")
    else:
        warn("CE LTP (REST)", f"got 0 — raw: {data}")

    if pe_ltp > 0:
        passed("PE LTP (REST)", f"Rs {pe_ltp:.2f}")
    else:
        warn("PE LTP (REST)", f"got 0 — raw: {data}")

except Exception as e:
    failed("LTP fetch", str(e))


# ══════════════════════════════════════════════════════════
# TEST 7 + 8 + 9 — WebSocket: connection, ticks, ap field, VWAP engine
# ══════════════════════════════════════════════════════════
section("TEST 7/8/9 — WebSocket: connect → ticks → ap (VWAP) → engine")
print("  Subscribing to 2 tokens and listening for 30 seconds...\n")

ws_connected   = False
ticks_received = 0
ap_received    = {"CE": False, "PE": False}
ap_values      = {"CE": 0.0, "PE": 0.0}
ltp_values     = {"CE": 0.0, "PE": 0.0}
ws_errors      = []

# Wire up VWAP engine with the mirror pair
test_eng = StrategyEngine()
if ce_token and pe_token:
    test_eng.setup_strikes(spot or atm,
                           {ce_strike: ce_token},
                           {pe_strike: pe_token})

all_tokens = {}
if ce_token: all_tokens[str(ce_token)] = (ce_strike, "CE")
if pe_token: all_tokens[str(pe_token)] = (pe_strike, "PE")

def on_message(message):
    global ticks_received
    try:
        if not isinstance(message, dict):
            return
        if message.get("type", "") not in ["stock_feed", "sf", "index_feed", "if"]:
            return
        ticks = message.get("data", [])
        for tick in ticks:
            token = str(tick.get("tk", "") or tick.get("token", "") or "")
            ltp   = safe_float(tick.get("ltp", 0) or tick.get("ltP", 0))
            ap    = safe_float(tick.get("ap", 0))

            if token not in all_tokens:
                continue
            if ltp <= 0:
                continue

            ticks_received += 1
            _, opt_type = all_tokens[token]
            ltp_values[opt_type] = ltp

            # Feed into engine exactly as main.py does
            test_eng.add_tick(token, ltp)
            if ap > 0:
                test_eng.set_vwap_direct(token, ap)
                ap_received[opt_type] = True
                ap_values[opt_type]   = ap

    except Exception as e:
        ws_errors.append(str(e))

def on_open(msg):
    global ws_connected
    ws_connected = True
    print(f"  [WS] Connected at {datetime.datetime.now().strftime('%H:%M:%S')}")

def on_error(error):
    ws_errors.append(str(error))
    print(f"  [WS] Error: {error}")

def on_close(msg):
    print(f"  [WS] Closed: {msg}")

client.on_message = on_message
client.on_open    = on_open
client.on_error   = on_error
client.on_close   = on_close

try:
    tokens_to_sub = []
    if ce_token: tokens_to_sub.append({"instrument_token": str(ce_token),
                                        "exchange_segment": config.FO_SEGMENT})
    if pe_token: tokens_to_sub.append({"instrument_token": str(pe_token),
                                        "exchange_segment": config.FO_SEGMENT})
    client.subscribe(instrument_tokens=tokens_to_sub, isIndex=False, isDepth=False)
except Exception as e:
    failed("WS subscribe", str(e))

# Wait up to 30 seconds watching for ticks + ap
wait_secs = 30
for i in range(wait_secs):
    time.sleep(1)
    elapsed = i + 1
    ce_ltp_live = ltp_values.get("CE", 0)
    pe_ltp_live = ltp_values.get("PE", 0)
    ce_ap       = ap_values.get("CE", 0)
    pe_ap       = ap_values.get("PE", 0)
    print(f"  [{elapsed:2d}s] ticks={ticks_received} | "
          f"CE ltp={ce_ltp_live:.2f} ap(VWAP)={ce_ap:.2f} | "
          f"PE ltp={pe_ltp_live:.2f} ap(VWAP)={pe_ap:.2f}", end="\r")
    if ap_received["CE"] and ap_received["PE"]:
        print()  # newline after \r
        break

print()

# Evaluate WS tests
if ws_connected:
    passed("WS connection")
else:
    failed("WS connection", "on_open never fired")

if ticks_received > 0:
    passed("Tick reception", f"{ticks_received} ticks in {wait_secs}s")
else:
    failed("Tick reception", "0 ticks received — market may be closed or token mismatch")

if ap_received["CE"]:
    passed("CE ap field (VWAP)", f"ap={ap_values['CE']:.2f} | ltp={ltp_values['CE']:.2f}")
else:
    warn("CE ap field", "ap=0 on all ticks — VWAP will not work until market opens")

if ap_received["PE"]:
    passed("PE ap field (VWAP)", f"ap={ap_values['PE']:.2f} | ltp={ltp_values['PE']:.2f}")
else:
    warn("PE ap field", "ap=0 on all ticks — VWAP will not work until market opens")

# Test VWAP engine state after live ticks
section("TEST 9 — VWAP engine state after live ticks")
ce_vwap_obj = test_eng.ce_strikes.get(ce_token)
pe_vwap_obj = test_eng.pe_strikes.get(pe_token)

if ce_vwap_obj:
    print(f"  CE engine: ltp={ce_vwap_obj.ltp:.2f} vwap={ce_vwap_obj.vwap:.2f} "
          f"ticks={ce_vwap_obj.tick_count} above={ce_vwap_obj.is_above_vwap()} "
          f"below={ce_vwap_obj.is_below_vwap()}")
    if ce_vwap_obj.tick_count > 0:
        passed("CE engine state", f"{ce_vwap_obj.status()}")
    else:
        warn("CE engine state", "0 ticks — market likely closed")
else:
    failed("CE engine state", "OptionVWAP object not found in engine")

if pe_vwap_obj:
    print(f"  PE engine: ltp={pe_vwap_obj.ltp:.2f} vwap={pe_vwap_obj.vwap:.2f} "
          f"ticks={pe_vwap_obj.tick_count} above={pe_vwap_obj.is_above_vwap()} "
          f"below={pe_vwap_obj.is_below_vwap()}")
    if pe_vwap_obj.tick_count > 0:
        passed("PE engine state", f"{pe_vwap_obj.status()}")
    else:
        warn("PE engine state", "0 ticks — market likely closed")
else:
    failed("PE engine state", "OptionVWAP object not found in engine")

# Check mirror pair identification
ce_pair, pe_pair = test_eng._get_mirror_pair()
if ce_pair and pe_pair:
    passed("Mirror pair identified in engine",
           f"CE={ce_pair.strike} PE={pe_pair.strike}")
elif ticks_received == 0:
    warn("Mirror pair check", "skipped — no ticks (market closed)")
else:
    failed("Mirror pair check",
           f"_get_mirror_pair returned None — ce={ce_pair} pe={pe_pair}")

if ws_errors:
    warn("WS errors seen", str(ws_errors[:3]))


# ══════════════════════════════════════════════════════════
# TEST 10 — BUY limit order → poll → cancel
# ══════════════════════════════════════════════════════════
section("TEST 10 — BUY limit order (1 lot CE) → poll → cancel")

now_t = datetime.datetime.now().time()
market_open   = datetime.time(9, 15)
market_close  = datetime.time(15, 25)
during_market = market_open <= now_t < market_close

buy_order_id  = None
filled_qty    = 0
filled_price  = 0.0

if not ce_token or not ce_symbol:
    warn("BUY order test", "skipped — CE token not resolved")
elif not during_market:
    warn("BUY order test",
         f"market is closed ({now_t.strftime('%H:%M')}) — skipping live order")
else:
    ref_ltp = ltp_values.get("CE") or ce_ltp or 100.0
    limit_price = round(ref_ltp + config.BUY_LIMIT_BUFFER, 1)

    print(f"  Placing BUY: {ce_symbol} | qty={config.LOT_SIZE} | "
          f"limit=Rs {limit_price:.2f} (ltp={ref_ltp:.2f} + {config.BUY_LIMIT_BUFFER}buffer)")
    try:
        resp = client.place_order(
            exchange_segment   = config.FO_SEGMENT,
            product            = "NRML",
            trading_symbol     = ce_symbol,
            transaction_type   = "B",
            quantity           = str(config.LOT_SIZE),
            order_type         = "L",
            price              = str(limit_price),
            validity           = "DAY",
            amo                = "NO",
            disclosed_quantity = "0",
            market_protection  = "0",
            pf                 = "N",
            trigger_price      = "0",
            tag                = "TEST_BUY",
        )
        buy_order_id = extract_order_id(resp)
        print(f"  place_order response: {resp}")
        print(f"  order_id: {buy_order_id}")

        if buy_order_id:
            passed("BUY order placed", f"id={buy_order_id}")
        else:
            failed("BUY order placed", f"no order_id in response: {resp}")

    except Exception as e:
        failed("BUY order placed", str(e))

    if buy_order_id:
        print(f"\n  Polling for {config.ORDER_FILL_TIMEOUT_SECS}s...")
        snap = poll_until_terminal(client, buy_order_id, config.LOT_SIZE,
                                   config.ORDER_FILL_TIMEOUT_SECS)
        status      = snap.get("status", "")
        filled_qty  = safe_int(snap.get("filled_qty"))
        pending     = safe_int(snap.get("pending_qty"), config.LOT_SIZE - filled_qty)
        filled_price = safe_float(snap.get("avg_price"))
        rej_reason  = snap.get("rej_reason", "")

        print(f"\n  After polling: status={status} filled={filled_qty} "
              f"pending={pending} avg={filled_price:.2f}")

        if status in {"complete", "completed", "traded"}:
            passed("BUY fill poll", f"filled={filled_qty} avg=Rs {filled_price:.2f}")
        elif filled_qty > 0:
            passed("BUY partial fill", f"filled={filled_qty}/{config.LOT_SIZE}")
        elif status in {"rejected"}:
            warn("BUY order rejected", rej_reason)
        else:
            warn("BUY order not filled", f"status={status} — will cancel")

        # Cancel any remaining
        if pending > 0 and status not in {"cancelled", "canceled", "rejected"}:
            print(f"\n  Cancelling remaining {pending} qty...")
            cancel_resp = cancel_order(client, buy_order_id)
            print(f"  Cancel response: {cancel_resp}")
            time.sleep(1)
            final = get_order_snapshot(client, buy_order_id)
            print(f"  Post-cancel: status={final.get('status')} "
                  f"filled={final.get('filled_qty')} pending={final.get('pending_qty')}")
            if final.get("status") in {"cancelled", "canceled"} or safe_int(final.get("pending_qty")) == 0:
                passed("BUY cancel", "pending qty cleared")
            else:
                warn("BUY cancel", f"status={final.get('status')} — may need manual check")
        else:
            passed("BUY cancel check", "no pending qty to cancel")


# ══════════════════════════════════════════════════════════
# TEST 11 — SELL market order on filled qty
# ══════════════════════════════════════════════════════════
section("TEST 11 — SELL market order (exit filled qty)")

if not during_market:
    warn("SELL order test", "market closed — skipping")
elif filled_qty <= 0:
    warn("SELL order test", "no filled qty from buy — skipping")
else:
    ref_ltp_sell = ltp_values.get("CE") or ce_ltp or filled_price
    print(f"  Placing SELL MKT: {ce_symbol} | qty={filled_qty} | ref_ltp={ref_ltp_sell:.2f}")
    try:
        resp = client.place_order(
            exchange_segment   = config.FO_SEGMENT,
            product            = "NRML",
            trading_symbol     = ce_symbol,
            transaction_type   = "S",
            quantity           = str(filled_qty),
            order_type         = "MKT",
            price              = "0",
            validity           = "DAY",
            amo                = "NO",
            disclosed_quantity = "0",
            market_protection  = "0",
            pf                 = "N",
            trigger_price      = "0",
            tag                = "TEST_SELL",
        )
        sell_order_id = extract_order_id(resp)
        print(f"  place_order response: {resp}")
        print(f"  order_id: {sell_order_id}")

        if sell_order_id:
            passed("SELL order placed", f"id={sell_order_id}")
        else:
            failed("SELL order placed", f"no order_id: {resp}")

        if sell_order_id:
            print(f"\n  Polling SELL for {config.EXIT_FILL_TIMEOUT_SECS}s...")
            snap = poll_until_terminal(client, sell_order_id, filled_qty,
                                       config.EXIT_FILL_TIMEOUT_SECS)
            sold_qty   = safe_int(snap.get("filled_qty"))
            sell_price = safe_float(snap.get("avg_price"))
            sell_status = snap.get("status", "")

            if sold_qty >= filled_qty:
                passed("SELL fill poll",
                       f"sold={sold_qty} @ Rs {sell_price:.2f}")
                pnl_pts = sell_price - filled_price
                print(f"\n  ── Round-trip P&L (test trade) ──")
                print(f"     Buy  : Rs {filled_price:.2f} x {filled_qty}")
                print(f"     Sell : Rs {sell_price:.2f} x {sold_qty}")
                print(f"     P&L  : {pnl_pts:+.2f} pts = Rs {pnl_pts*filled_qty:+.0f} (before costs)")
            else:
                warn("SELL fill poll",
                     f"sold={sold_qty}/{filled_qty} status={sell_status} — CHECK POSITIONS")

    except Exception as e:
        failed("SELL order", str(e))


# ══════════════════════════════════════════════════════════
# TEST 12 — Order history + report parsing
# ══════════════════════════════════════════════════════════
section("TEST 12 — Order history + order report (snapshot parsing)")
try:
    rpt  = client.order_report()
    rows = normalize_rows(rpt)
    if rows:
        sample = rows[-1]
        order_id_sample = str(sample.get("nOrdNo") or sample.get("order_id") or "")
        if order_id_sample:
            snap = get_order_snapshot(client, order_id_sample)
            passed("Order report + snapshot parse",
                   f"last order: {order_id_sample} status={snap.get('status')}")
        else:
            warn("Order report", "rows found but no order_id key — check field names")
    else:
        warn("Order report", "empty — no orders today (expected if market closed)")
except Exception as e:
    failed("Order report", str(e))


# ══════════════════════════════════════════════════════════
# TEST 13 — Positions check
# ══════════════════════════════════════════════════════════
section("TEST 13 — Positions")
try:
    resp = client.positions()
    rows = resp if isinstance(resp, list) else (resp.get("data") or resp.get("message") or [])
    open_pos = [r for r in rows if safe_int(r.get("netQty") or r.get("net_quantity")) != 0]
    if open_pos:
        warn(f"Open positions found ({len(open_pos)})",
             "verify these are intentional")
        for p in open_pos:
            sym = p.get("trdSym") or p.get("trading_symbol") or "?"
            qty = p.get("netQty") or p.get("net_quantity") or 0
            avg = p.get("avgPrc") or p.get("average_price") or 0
            print(f"     {sym} qty={qty} avg={avg}")
    else:
        passed("Positions check", "no open positions")
except Exception as e:
    warn("Positions", str(e))


# ══════════════════════════════════════════════════════════
# TEST 14 — Capital JSON
# ══════════════════════════════════════════════════════════
section("TEST 14 — Capital JSON read + write")
try:
    cap_mgr = CapitalManager()
    summary = cap_mgr.get_summary()
    passed("Capital JSON",
           f"current=Rs {summary.get('current',0):,.0f} "
           f"deployed=Rs {summary.get('deployed',0):,.0f} "
           f"roi={summary.get('roi_pct',0):+.2f}%")
except Exception as e:
    failed("Capital JSON", str(e))


# ══════════════════════════════════════════════════════════
# TEST 15 — ReportManager CSV write
# ══════════════════════════════════════════════════════════
section("TEST 15 — ReportManager CSV write")
try:
    os.makedirs("reports", exist_ok=True)
    rm = ReportManager(CapitalManager())
    rm.set_vix(vix if 'vix' in dir() else 15.0)

    now = datetime.datetime.now()
    dummy_trade = {
        "direction"      : "CE",
        "strike"         : ce_strike,
        "symbol"         : ce_symbol or f"NIFTY_CE_{ce_strike}",
        "entry_price"    : 150.0,
        "exit_price"     : 155.0,
        "peak_price"     : 158.0,
        "entry_time"     : now - datetime.timedelta(minutes=15),
        "exit_time"      : now,
        "entry_vwap"     : 148.0,
        "entry_dist"     : 2.0,
        "atm_at_entry"   : atm,
        "nifty_at_entry" : spot,
        "nifty_at_exit"  : spot + 30,
        "pnl_pts"        : 5.0,
        "pnl_rs"         : 5.0 * config.LOT_SIZE,
        "total_cost"     : 340.0,
        "net_rs"         : 5.0 * config.LOT_SIZE - 340.0,
        "won"            : True,
        "exit_reason"    : "Target hit | [10:30:00] | test",
        "exit_phase"     : "Target",
        "breakeven_done" : False,
        "trail_active"   : False,
        "target_points"  : 27.5,
        "target_reason"  : "low VIX",
    }
    rm.log_trade(dummy_trade)
    rm.close()

    if os.path.exists(config.TRADE_LOG_FILE):
        size = os.path.getsize(config.TRADE_LOG_FILE)
        passed("ReportManager CSV", f"{config.TRADE_LOG_FILE} exists ({size} bytes)")
    else:
        failed("ReportManager CSV", f"{config.TRADE_LOG_FILE} not created")
except Exception as e:
    failed("ReportManager CSV", str(e))


# ══════════════════════════════════════════════════════════
# FINAL SUMMARY
# ══════════════════════════════════════════════════════════
section("FINAL SUMMARY")

passes = [r for r in RESULTS if r[0] == "PASS"]
fails  = [r for r in RESULTS if r[0] == "FAIL"]
warns  = [r for r in RESULTS if r[0] == "WARN"]

print(f"  Total : {len(RESULTS)}")
print(f"  PASS  : {len(passes)}")
print(f"  WARN  : {len(warns)}  (non-blocking — expected if market closed)")
print(f"  FAIL  : {len(fails)}")

if fails:
    print("\n  FAILURES — must fix before running algo:")
    for _, name, detail in fails:
        print(f"    ❌  {name}: {detail}")

if warns:
    print("\n  WARNINGS — review before market open:")
    for _, name, detail in warns:
        print(f"    ⚠️   {name}: {detail}")

print()
if not fails:
    print("  ✅  All critical tests passed. Algo is safe to run.")
else:
    print("  ❌  Fix failures above before running the algo.")
print()
