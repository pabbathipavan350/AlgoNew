# ============================================================
# OPTION_MANAGER.PY — Strike Selection + Order Placement
# ============================================================
# Strike selection:
#   1. Find ATM from current Nifty spot
#   2. Go deep ITM (CE: ATM - depth, PE: ATM + depth)
#      where depth is chosen to satisfy delta >= 0.85
#   3. Check OI >= 12,00,000. If not, walk toward ATM
#      in 50pt steps (up to 8 steps) until OI passes.
#   4. Use current month expiry futures contract expiry date.
#
# Order placement:
#   - Limit buy at LTP + BUY_LIMIT_BUFFER (2 pts)
#   - Poll for fill every 1s, timeout 15s
#   - Exit: market order, retry up to 3 times
#
# Cost calculation:
#   - Brokerage Rs 20 flat per order
#   - STT, exchange txn, SEBI, GST, stamp duty per config
# ============================================================

import math
import logging
import time
import datetime
import config

logger = logging.getLogger(__name__)


# ── Black-Scholes delta (for strike depth estimation) ─────

def _bs_delta(S, K, T, r, sigma, option_type):
    """Black-Scholes delta. Returns float 0-1."""
    if T <= 0 or sigma <= 0:
        return 1.0 if (option_type == 'CE' and S > K) else 0.0
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        from statistics import NormalDist
        nd = NormalDist()
        if option_type == 'CE':
            return nd.cdf(d1)
        else:
            return nd.cdf(-d1)
    except Exception:
        return 0.0


def _days_to_expiry(expiry_date: datetime.date) -> float:
    """Calendar days to expiry as a fraction of a year."""
    today = datetime.date.today()
    days  = (expiry_date - today).days
    return max(days, 0) / 365.0


# ── ATM rounding ─────────────────────────────────────────

def round_to_strike(price: float, step: int = 50) -> int:
    return int(round(price / step) * step)


# ── Fetch OI for a single token ──────────────────────────

def fetch_oi(client, token: str) -> int:
    """Fetch open interest for a token. Returns 0 on failure."""
    try:
        resp = client.quotes(
            instrument_tokens=[{"instrument_token": token,
                                 "exchange_segment": config.FO_SEGMENT}],
            quote_type="ohlc"
        )
        if isinstance(resp, list) and resp:
            q = resp[0]
            oi = int(q.get("oi") or q.get("open_interest") or 0)
            return oi
    except Exception as e:
        logger.debug(f"fetch_oi error token={token}: {e}")
    return 0


# ── Resolve option token from scrip master ───────────────

def find_option_token(client, symbol_prefix: str,
                      expiry_str: str, strike: int,
                      option_type: str) -> str | None:
    """
    Look up the Kotak token for a specific option contract.
    symbol_prefix : 'NIFTY'
    expiry_str    : 'DDMMMYY' e.g. '29MAY25'
    strike        : 23000
    option_type   : 'CE' or 'PE'
    Returns token string or None.
    """
    try:
        import requests, csv, io
        url  = client.scrip_master(exchange_segment=config.FO_SEGMENT)
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200:
            logger.error(f"Scrip master fetch failed: {resp.status_code}")
            return None

        reader = csv.DictReader(io.StringIO(resp.text))
        target_trd = f"{symbol_prefix}{expiry_str}{strike}{option_type}"

        for row in reader:
            trd = (row.get("pTrdSymbol") or "").strip().upper()
            if trd == target_trd.upper():
                tok = (row.get("pSymbol") or "").strip()
                if tok:
                    logger.info(f"Resolved {target_trd} → token {tok}")
                    return tok

        # Sometimes the symbol format differs — try partial match
        for row in reader:
            trd = (row.get("pTrdSymbol") or "").strip().upper()
            if (symbol_prefix in trd and
                    str(strike) in trd and
                    option_type in trd and
                    expiry_str[:5].upper() in trd):
                tok = (row.get("pSymbol") or "").strip()
                if tok:
                    logger.info(f"Partial match {target_trd} → {trd} token {tok}")
                    return tok

        logger.warning(f"Token not found for {target_trd}")
        return None

    except Exception as e:
        logger.error(f"find_option_token error: {e}")
        return None


def find_futures_token(client, expiry_str: str) -> str | None:
    """
    Find current-month Nifty futures token from scrip master.
    Tries multiple symbol formats because Kotak's scrip master format
    can vary (NIFTY30APR26FUT vs NIFTYFUT vs other variants).

    expiry_str : 'DDMMMYY' e.g. '30APR26'
    """
    try:
        import requests, csv, io

        url  = client.scrip_master(exchange_segment=config.FO_SEGMENT)
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200:
            logger.error(f"Scrip master HTTP {resp.status_code}")
            return None

        rows = list(csv.DictReader(io.StringIO(resp.text)))

        # ── Candidate formats to try ──────────────────────
        # Format 1: NIFTY30APR26FUT  (most common)
        # Format 2: NIFTY-I           (continuous front-month alias)
        # Format 3: NIFTYFUT          (no date)
        # Format 4: NFO:NIFTY30APR26FUT
        month3  = expiry_str[2:5]          # e.g. APR
        year2   = expiry_str[5:]           # e.g. 26
        day2    = expiry_str[:2]           # e.g. 30
        # Alt date format without leading zero day
        day_no0 = str(int(day2))           # e.g. 30 → '30' (no change needed usually)

        candidates = [
            f"NIFTY{expiry_str}FUT",              # NIFTY28APR26FUT (DDMMMYY)
            f"NIFTY{day_no0}{month3}{year2}FUT",  # NIFTY28APR26FUT no leading zero
            f"NIFTY{year2}{month3}FUT",           # NIFTY26APRFUT ← Kotak actual format
            f"NIFTY{month3}{year2}FUT",           # NIFTYAPR26FUT
            "NIFTY-I",
            "NIFTYFUT",
        ]

        # ── Print all FUT rows for debugging ─────────────
        fut_rows = [
            r for r in rows
            if "NIFTY" in (r.get("pTrdSymbol") or "").upper()
            and "FUT" in (r.get("pTrdSymbol") or "").upper()
        ]
        print(f"[FuturesToken] Scrip master has {len(fut_rows)} NIFTY FUT rows:")
        for r in fut_rows[:15]:   # show up to 15 so we can see the format
            print(f"  pTrdSymbol={r.get('pTrdSymbol','')!r:30s}  "
                  f"pSymbol={r.get('pSymbol','')!r}")

        # ── Try exact match on each candidate ─────────────
        for candidate in candidates:
            for row in rows:
                trd = (row.get("pTrdSymbol") or "").strip().upper()
                if trd == candidate.upper():
                    tok = (row.get("pSymbol") or "").strip()
                    if tok:
                        print(f"[FuturesToken] ✅ Matched '{candidate}' → token={tok}")
                        logger.info(f"Futures token matched: {trd} → {tok}")
                        return tok

        # ── Fallback: partial match — any row with NIFTY + FUT
        #    that also contains the month+year ──────────────
        month_year = f"{month3}{year2}"   # e.g. APR26
        for row in rows:
            trd = (row.get("pTrdSymbol") or "").strip().upper()
            if ("NIFTY" in trd and "FUT" in trd and month_year in trd):
                tok = (row.get("pSymbol") or "").strip()
                if tok:
                    print(f"[FuturesToken] ✅ Partial match '{trd}' → token={tok}")
                    logger.info(f"Futures token partial match: {trd} → {tok}")
                    return tok

        print(f"[FuturesToken] ❌ No match found for expiry={expiry_str}")
        print(f"[FuturesToken]    Tried: {candidates}")
        logger.warning(f"Futures token not found for expiry {expiry_str}")
        return None

    except Exception as e:
        logger.error(f"find_futures_token error: {e}", exc_info=True)
        return None


# ── NSE market holidays (update annually) ────────────────
# Source: NSE India official holiday list
NSE_HOLIDAYS_2026 = {
    datetime.date(2026, 1, 26),   # Republic Day
    datetime.date(2026, 2, 19),   # Chhatrapati Shivaji Maharaj Jayanti
    datetime.date(2026, 3, 20),   # Holi (Dhuleti)
    datetime.date(2026, 4, 2),    # Ram Navami
    datetime.date(2026, 4, 3),    # Good Friday
    datetime.date(2026, 4, 14),   # Dr. Ambedkar Jayanti
    datetime.date(2026, 5, 1),    # Maharashtra Day
    datetime.date(2026, 8, 15),   # Independence Day
    datetime.date(2026, 8, 27),   # Ganesh Chaturthi
    datetime.date(2026, 10, 2),   # Gandhi Jayanti / Dussehra
    datetime.date(2026, 10, 21),  # Diwali (Laxmi Puja)
    datetime.date(2026, 10, 22),  # Diwali (Balipratipada)
    datetime.date(2026, 11, 5),   # Guru Nanak Jayanti
    datetime.date(2026, 12, 25),  # Christmas
}

NSE_HOLIDAYS_2025 = {
    datetime.date(2025, 1, 26),
    datetime.date(2025, 2, 26),
    datetime.date(2025, 3, 14),
    datetime.date(2025, 3, 31),
    datetime.date(2025, 4, 10),
    datetime.date(2025, 4, 14),
    datetime.date(2025, 4, 18),
    datetime.date(2025, 5, 1),
    datetime.date(2025, 8, 15),
    datetime.date(2025, 8, 27),
    datetime.date(2025, 10, 2),
    datetime.date(2025, 10, 20),   # Diwali Laxmi Puja (tentative)
    datetime.date(2025, 10, 21),   # Diwali Balipratipada
    datetime.date(2025, 11, 5),
    datetime.date(2025, 12, 25),
}

def _is_nse_holiday(d: datetime.date) -> bool:
    """True if date is a weekend or NSE market holiday."""
    if d.weekday() >= 5:   # Saturday=5, Sunday=6
        return True
    all_holidays = NSE_HOLIDAYS_2025 | NSE_HOLIDAYS_2026
    return d in all_holidays


# ── Current-month expiry date ─────────────────────────────

def get_current_month_expiry() -> datetime.date:
    """
    Nifty monthly options/futures expiry = last TUESDAY of the month.
    If that Tuesday is an NSE holiday → roll back to Monday.
    If Monday is also a holiday → roll back to Friday.
    If today is past the expiry, use next month's expiry.
    """
    today = datetime.date.today()

    def last_tuesday(year, month):
        # Find last day of month, walk back to Tuesday (weekday 1)
        if month == 12:
            last = datetime.date(year + 1, 1, 1) - datetime.timedelta(days=1)
        else:
            last = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
        while last.weekday() != 1:   # 1 = Tuesday
            last -= datetime.timedelta(days=1)
        return last

    def adjust_for_holiday(d: datetime.date) -> datetime.date:
        """Roll back day by day until we hit a trading day."""
        while _is_nse_holiday(d):
            d -= datetime.timedelta(days=1)
        return d

    def next_month(year, month):
        return (year + 1, 1) if month == 12 else (year, month + 1)

    exp = adjust_for_holiday(last_tuesday(today.year, today.month))

    if today > exp:
        ny, nm = next_month(today.year, today.month)
        exp = adjust_for_holiday(last_tuesday(ny, nm))

    return exp


def expiry_to_kotak_str(expiry_date: datetime.date) -> str:
    """Convert expiry date to Kotak trading symbol format: DDMMMYY e.g. 30APR26"""
    return expiry_date.strftime("%d%b%y").upper()


# ── Main strike picker ────────────────────────────────────

class OptionManager:

    def __init__(self, client):
        self.client       = client
        self.expiry_date  = get_current_month_expiry()
        self.expiry_str   = expiry_to_kotak_str(self.expiry_date)
        self.dte          = _days_to_expiry(self.expiry_date)
        print(f"[OptionManager] Expiry: {self.expiry_date} ({self.expiry_str}) "
              f"DTE={self.dte*365:.0f}d")

    def pick_strike(self, spot: float, direction: str) -> dict | None:
        """
        Pick the best ITM strike for direction ('CE' or 'PE').
        Returns dict with keys: strike, token, delta, oi, expiry_str
        Returns None if no valid strike found.
        """
        atm   = round_to_strike(spot, config.STRIKE_STEP)
        sigma = config.IV_PCT / 100.0
        r     = config.RISK_FREE_RATE
        T     = self.dte

        # Start deep ITM and walk toward ATM until delta + OI satisfied
        # CE: strike below ATM (lower strike = higher delta for CE)
        # PE: strike above ATM (higher strike = higher delta for PE)

        if direction == "CE":
            # Go 300 pts ITM to start, walk toward ATM
            start_strike = atm - 300
            candidates   = range(start_strike, atm + config.STRIKE_STEP,
                                  config.STRIKE_STEP)
        else:
            # Go 300 pts ITM above ATM, walk toward ATM (downward)
            start_strike = atm + 300
            candidates   = range(start_strike, atm - config.STRIKE_STEP,
                                  -config.STRIKE_STEP)

        best_token  = None
        best_strike = None
        best_delta  = 0.0
        best_oi     = 0

        walk_steps = 0
        for strike in candidates:
            if walk_steps > config.MAX_OI_WALK_STEPS + 6:
                break

            delta = _bs_delta(spot, strike, T, r, sigma, direction)

            if delta < config.MIN_DELTA:
                walk_steps += 1
                continue

            # Delta ok — resolve token and check OI
            token = find_option_token(
                self.client, "NIFTY", self.expiry_str, strike, direction
            )
            if not token:
                walk_steps += 1
                continue

            oi = fetch_oi(self.client, token)
            logger.info(f"[Strike] {direction} {strike}: delta={delta:.2f} OI={oi:,}")

            if oi >= config.MIN_OI:
                # Good strike found
                print(f"[OptionManager] ✅ {direction} strike={strike} "
                      f"delta={delta:.2f} OI={oi:,} token={token}")
                return {
                    "strike"    : strike,
                    "token"     : token,
                    "delta"     : delta,
                    "oi"        : oi,
                    "expiry_str": self.expiry_str,
                    "expiry_date": self.expiry_date,
                }

            walk_steps += 1
            best_token  = token
            best_strike = strike
            best_delta  = delta
            best_oi     = oi

        # Fallback: use best we found even if OI is low
        if best_token:
            print(f"[OptionManager] ⚠️  OI fallback {direction} "
                  f"strike={best_strike} delta={best_delta:.2f} "
                  f"OI={best_oi:,} (below threshold)")
            return {
                "strike"     : best_strike,
                "token"      : best_token,
                "delta"      : best_delta,
                "oi"         : best_oi,
                "expiry_str" : self.expiry_str,
                "expiry_date": self.expiry_date,
            }

        logger.error(f"[OptionManager] No valid strike found for {direction}")
        return None

    # ── Order placement ──────────────────────────────────

    def place_buy_order(self, token: str, strike: int, direction: str,
                        ltp: float) -> dict | None:
        """
        Place a limit buy order at LTP + BUY_LIMIT_BUFFER.
        Returns fill info dict or None on failure.
        PAPER_TRADE mode simulates the fill at limit price.
        """
        qty        = config.LOTS * config.LOT_SIZE
        limit_px   = round(ltp + config.BUY_LIMIT_BUFFER, 2)

        print(f"\n[Order] BUY {direction} {strike} | qty={qty} "
              f"ltp={ltp:.2f} limit={limit_px:.2f}")

        if config.PAPER_TRADE:
            fill_px = limit_px
            print(f"[Order] PAPER FILL @ {fill_px:.2f}")
            return {"fill_price": fill_px, "qty": qty, "order_id": "PAPER"}

        try:
            resp = self.client.place_order(
                exchange_segment = config.FO_SEGMENT,
                product          = "NRML",
                price            = str(limit_px),
                order_type       = "L",
                quantity         = str(qty),
                validity         = "DAY",
                trading_symbol   = self._build_symbol(strike, direction),
                transaction_type = "B",
                amo              = "NO",
                disclosed_quantity = "0",
                market_protection  = "0",
                pf                 = "N",
                trigger_price      = "0",
                tag                = None,
            )
            order_id = self._extract_order_id(resp)
            if not order_id:
                logger.error(f"Buy order failed: {resp}")
                return None

            fill = self._wait_for_fill(order_id, qty,
                                       config.ORDER_FILL_TIMEOUT_SECS)
            return fill

        except Exception as e:
            logger.error(f"place_buy_order exception: {e}")
            return None

    def place_exit_order(self, token: str, strike: int, direction: str,
                         qty: int, reason: str = "") -> float | None:
        """
        Place a market exit (sell) order.
        Returns actual exit price or None.
        Retries up to EXIT_RETRY_ATTEMPTS times.
        """
        print(f"\n[Order] SELL {direction} {strike} qty={qty} reason={reason}")

        if config.PAPER_TRADE:
            # In paper mode, simulate LTP as exit price
            # Caller should pass current LTP — we return it as-is
            print(f"[Order] PAPER EXIT acknowledged")
            return None   # caller uses current LTP from engine

        for attempt in range(config.EXIT_RETRY_ATTEMPTS):
            try:
                resp = self.client.place_order(
                    exchange_segment = config.FO_SEGMENT,
                    product          = "NRML",
                    price            = "0",
                    order_type       = "MKT",
                    quantity         = str(qty),
                    validity         = "DAY",
                    trading_symbol   = self._build_symbol(strike, direction),
                    transaction_type = "S",
                    amo              = "NO",
                    disclosed_quantity = "0",
                    market_protection  = "0",
                    pf                 = "N",
                    trigger_price      = "0",
                    tag                = None,
                )
                order_id = self._extract_order_id(resp)
                if not order_id:
                    logger.error(f"Exit order failed attempt {attempt+1}: {resp}")
                    time.sleep(2)
                    continue

                fill = self._wait_for_fill(order_id, qty,
                                           config.EXIT_FILL_TIMEOUT_SECS)
                if fill:
                    return fill.get("fill_price")

            except Exception as e:
                logger.error(f"place_exit_order attempt {attempt+1}: {e}")
                time.sleep(2)

        logger.error("Exit order failed after all retries")
        return None

    def _build_symbol(self, strike: int, direction: str) -> str:
        return f"NIFTY{self.expiry_str}{strike}{direction}"

    def _extract_order_id(self, resp) -> str | None:
        if not resp:
            return None
        if isinstance(resp, dict):
            return (resp.get("nOrdNo") or resp.get("order_id") or
                    resp.get("orderId") or resp.get("id"))
        return None

    def _wait_for_fill(self, order_id: str, qty: int,
                       timeout: float) -> dict | None:
        """Poll order status until filled or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                resp = self.client.order_report()
                if isinstance(resp, list):
                    for o in resp:
                        if str(o.get("nOrdNo") or "") == str(order_id):
                            status   = (o.get("ordSt") or "").upper()
                            fill_qty = int(o.get("fldQty") or 0)
                            fill_px  = float(o.get("avgPrc") or 0)
                            if status in ("COMPLETE", "FILLED") and fill_qty > 0:
                                return {"fill_price": fill_px, "qty": fill_qty,
                                        "order_id": order_id}
                            if status in ("REJECTED", "CANCELLED"):
                                logger.error(f"Order {order_id} {status}")
                                return None
            except Exception as e:
                logger.debug(f"Poll error: {e}")
            time.sleep(config.ORDER_STATUS_POLL_SECS)

        # Timeout — cancel remainder
        try:
            self.client.cancel_order(order_id=order_id)
            logger.warning(f"Order {order_id} timed out — cancelled remainder")
        except Exception:
            pass
        return None

    # ── Cost calculation ─────────────────────────────────

    @staticmethod
    def calc_trade_cost(entry_price: float, exit_price: float,
                        qty: int) -> float:
        """
        Calculate total transaction cost for one round trip.
        Returns total cost in Rs.
        """
        buy_turnover  = entry_price * qty
        sell_turnover = exit_price  * qty
        total_tv      = buy_turnover + sell_turnover

        brokerage     = config.BROKERAGE_PER_ORDER * 2   # buy + sell
        stt           = total_tv * config.STT_PCT
        exchange_txn  = total_tv * config.EXCHANGE_TXN_PCT
        sebi          = total_tv * config.SEBI_PCT
        gst           = (brokerage + exchange_txn + sebi) * config.GST_PCT
        stamp_duty    = buy_turnover * config.STAMP_DUTY_PCT

        total_cost    = brokerage + stt + exchange_txn + sebi + gst + stamp_duty
        return round(total_cost, 2)
