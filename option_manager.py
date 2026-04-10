# ============================================================
# OPTION_MANAGER.PY — Strike Pre-loader + Order Placement v3
# ============================================================
# KEY CHANGE vs v2:
#   All strike tokens are resolved and OI-checked at STARTUP,
#   not when a signal fires. When a signal fires, pick_strike()
#   reads from the pre-loaded cache — zero HTTP delay.
#
# Pre-load flow (called once after auth):
#   1. Download scrip master → cache in memory
#   2. Fetch current Nifty spot (from futures LTP or index)
#   3. Round ATM, compute candidate strikes at various ITM depths
#   4. Resolve token for each candidate (from scrip master)
#   5. Fetch OI for all resolved tokens via quotes API
#   6. Store: {direction: [{strike, token, delta, oi}]} sorted by delta
#
# pick_strike() at signal time:
#   1. Read current spot
#   2. Find best pre-cached entry that passes delta>=0.85 and OI>=12L
#   3. If nothing in cache passes, fall back to live scan
# ============================================================

import math
import logging
import time
import datetime
import config

logger = logging.getLogger(__name__)


# ── Scrip master cache ────────────────────────────────────
_scrip_cache: list = []

def _get_scrip_master(client) -> list:
    global _scrip_cache
    if _scrip_cache:
        return _scrip_cache
    try:
        import requests, csv, io
        url  = client.scrip_master(exchange_segment=config.FO_SEGMENT)
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            _scrip_cache = list(csv.DictReader(io.StringIO(resp.text)))
            logger.info(f"Scrip master cached: {len(_scrip_cache)} rows")
        else:
            logger.error(f"Scrip master HTTP {resp.status_code}")
    except Exception as e:
        logger.error(f"Scrip master download error: {e}")
    return _scrip_cache


# ── Black-Scholes delta ───────────────────────────────────

def _bs_delta(S, K, T, r, sigma, option_type):
    if T <= 0 or sigma <= 0:
        return 1.0 if (option_type == 'CE' and S > K) else 0.0
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        from statistics import NormalDist
        nd = NormalDist()
        return nd.cdf(d1) if option_type == 'CE' else nd.cdf(-d1)
    except Exception:
        return 0.0

def _days_to_expiry(expiry_date: datetime.date) -> float:
    days = (expiry_date - datetime.date.today()).days
    return max(days, 0) / 365.0

def round_to_strike(price: float, step: int = 50) -> int:
    return int(round(price / step) * step)


# ── Quote helper ─────────────────────────────────────────

def _unwrap_quotes_resp(resp) -> list:
    """
    Kotak Neo v2 quotes() wraps the list inside resp['data'] or
    resp['message']. Handle all known shapes.
    """
    if resp is None:
        return []
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        # Try every known wrapper key
        for key in ("data", "message", "result", "quotes", "success"):
            val = resp.get(key)
            if isinstance(val, list) and val:
                return val
            if isinstance(val, dict):
                return [val]
    return []


def _raw_quote(client, token: str, quote_type: str) -> dict:
    """
    Call client.quotes() for a single token and return the first
    record as a plain dict, or {} on failure.
    Tries both nse_fo and nse_cm segments if first fails.
    """
    for segment in (config.FO_SEGMENT, config.CM_SEGMENT):
        try:
            resp = client.quotes(
                instrument_tokens=[{"instrument_token": token,
                                     "exchange_segment": segment}],
                quote_type=quote_type,
            )
            data = _unwrap_quotes_resp(resp)
            if data:
                return data[0]
        except Exception as e:
            logger.debug(f"_raw_quote {quote_type} seg={segment} tok={token}: {e}")
    return {}


# ── OI fetch ─────────────────────────────────────────────
# Kotak Neo v2 returns OI only in the "depth" or full quote response.
# "ohlc" quote_type omits OI in many cases.
# We try multiple quote_types and all known OI field names.

_OI_FIELDS = ("oi", "open_interest", "openInterest", "OI",
              "tot_buy_qty", "totBuyQty")   # last two are fallbacks

# One-time debug flag — dumps full quote response for first token
_OI_DEBUG_DONE = False

def fetch_oi(client, token: str) -> int:
    global _OI_DEBUG_DONE

    # Try quote_types in priority order
    for qt in ("depth", "ohlc", "ltp", ""):
        try:
            resp = client.quotes(
                instrument_tokens=[{"instrument_token": token,
                                     "exchange_segment": config.FO_SEGMENT}],
                quote_type=qt,
            )
            data = _unwrap_quotes_resp(resp)

            # Debug dump: print full response for first token so we can see
            # exactly what fields Kotak returns — only once per session
            if not _OI_DEBUG_DONE and data:
                print(f"\n[OI_DEBUG] quote_type={qt!r} full response for token={token}:")
                print(f"  {data[0]}")
                _OI_DEBUG_DONE = True

            if data:
                rec = data[0]
                for field in _OI_FIELDS:
                    val = rec.get(field)
                    if val and int(float(val)) > 0:
                        return int(float(val))
        except Exception as e:
            logger.debug(f"fetch_oi qt={qt} token={token}: {e}")

    return 0


def fetch_ltp(client, token: str) -> float:
    """Fetch current LTP for a token via quotes API."""
    _LTP_FIELDS = ("ltp", "ltP", "last_price", "lastPrice", "close", "lc")

    for qt in ("ltp", "ohlc", "depth", ""):
        try:
            resp = client.quotes(
                instrument_tokens=[{"instrument_token": token,
                                     "exchange_segment": config.FO_SEGMENT}],
                quote_type=qt,
            )
            data = _unwrap_quotes_resp(resp)
            if data:
                rec = data[0]
                for field in _LTP_FIELDS:
                    val = rec.get(field)
                    if val and float(val) > 0:
                        return float(val)
        except Exception as e:
            logger.debug(f"fetch_ltp qt={qt} token={token}: {e}")

    return 0.0


def fetch_oi_and_ltp(client, token: str) -> tuple[int, float]:
    """
    Fetch LTP via quotes API.
    OI is read from the scrip master cache (dOpenInt field) — the depth
    quote response does NOT include OI on Kotak Neo v2.
    Returns (oi, ltp).
    """
    global _OI_DEBUG_DONE

    _LTP_FIELDS_LOCAL = ("ltp", "ltP", "last_price", "lastPrice", "close", "lc")

    # ── OI from scrip master ──────────────────────────────
    # The depth/ohlc quote response has no OI field on Kotak Neo v2.
    # Read it from the cached scrip master row instead.
    oi = _get_oi_from_scrip_master(token)

    # ── LTP from quotes API ───────────────────────────────
    ltp = 0.0
    for qt in ("ltp", "ohlc", "depth"):
        try:
            resp = client.quotes(
                instrument_tokens=[{"instrument_token": token,
                                     "exchange_segment": config.FO_SEGMENT}],
                quote_type=qt,
            )
            data = _unwrap_quotes_resp(resp)

            # One-time full dump so we can see the exact field names
            if not _OI_DEBUG_DONE and data:
                print(f"\n[OI_DEBUG] quote_type={qt!r} raw response (token={token}):")
                print(f"  {data[0]}")
                _OI_DEBUG_DONE = True

            if data:
                rec = data[0]
                for f in _LTP_FIELDS_LOCAL:
                    v = rec.get(f)
                    if v and float(v) > 0:
                        ltp = float(v)
                        break
                if ltp > 0:
                    break
        except Exception as e:
            logger.debug(f"fetch_oi_and_ltp qt={qt} token={token}: {e}")

    return oi, ltp


# ── OI from scrip master (fast, no HTTP) ─────────────────
# Kotak scrip master has OI in dOpenInt or similar field.
# We build a token→OI dict once from the cached rows.
_scrip_oi_cache: dict[str, int] = {}
_scrip_oi_built = False
_OI_SM_FIELD    = None   # discovered field name

def _get_oi_from_scrip_master(token: str) -> int:
    """Return OI for a token from scrip master cache. 0 if not found."""
    global _scrip_oi_built, _OI_SM_FIELD, _scrip_oi_cache

    if not _scrip_oi_built:
        _build_scrip_oi_cache()

    return _scrip_oi_cache.get(str(token), 0)

def _build_scrip_oi_cache():
    """Build token→OI map from cached scrip master rows. Called once."""
    global _scrip_oi_built, _OI_SM_FIELD, _scrip_oi_cache
    _scrip_oi_built = True

    rows = _scrip_cache   # already downloaded
    if not rows:
        return

    oi_fields = ("dOpenInterest", "dOpenInt", "lOpenInt", "openInt", "dOI", "lOI",
                 "open_interest", "oi", "nOI", "iOpenInterest")

    # Find which field has non-zero values
    for field in oi_fields:
        sample = [r for r in rows[:200] if r.get(field, "0") not in ("0", "0.0", "", None)]
        if len(sample) > 5:
            _OI_SM_FIELD = field
            break

    if _OI_SM_FIELD:
        for row in rows:
            tok = (row.get("pSymbol") or "").strip()
            val = row.get(_OI_SM_FIELD, "0")
            try:
                oi = int(float(val or 0))
                if oi > 0:
                    _scrip_oi_cache[tok] = oi
            except Exception:
                pass
        print(f"[PreLoad] Scrip master OI: field='{_OI_SM_FIELD}' "
              f"{len(_scrip_oi_cache)} tokens with OI > 0")
    else:
        # Print all column names so we can find the right one
        if rows:
            cols = list(rows[0].keys())
            print(f"[PreLoad] Scrip master OI field not found. "
                  f"All columns: {cols}")



# ── Token resolution ─────────────────────────────────────

def find_option_token(client, symbol_prefix: str,
                      expiry_str: str, strike: int,
                      option_type: str) -> str | None:
    """
    Resolve Kotak token for a specific option contract.
    STRICT: pTrdSymbol must START with symbol_prefix (e.g. 'NIFTY')
    to avoid matching FINNIFTY, BANKNIFTY, MIDCPNIFTY etc.
    """
    rows       = _get_scrip_master(client)
    target_trd = f"{symbol_prefix}{expiry_str}{strike}{option_type}"
    prefix_up  = symbol_prefix.upper()

    # ── Exact match ───────────────────────────────────────
    for row in rows:
        trd = (row.get("pTrdSymbol") or "").strip().upper()
        if trd == target_trd.upper():
            # Extra safety: must start with the exact prefix
            if trd.startswith(prefix_up):
                tok = (row.get("pSymbol") or "").strip()
                if tok:
                    return tok

    # ── Partial match — strict prefix + strike + option_type ─
    month3_p = expiry_str[2:5].upper()
    year2_p  = expiry_str[5:].upper()
    strike_s = str(strike)

    for row in rows:
        trd = (row.get("pTrdSymbol") or "").strip().upper()
        # MUST start with exact prefix — prevents FINNIFTY, BANKNIFTY matches
        if not trd.startswith(prefix_up):
            continue
        # Second char after prefix must be a digit or expiry char, NOT another letter
        # e.g. "NIFTY13APR2623500CE" OK, "NIFTY50..." not OK
        suffix = trd[len(prefix_up):]
        if suffix and suffix[0].isalpha():
            continue   # rules out NIFTY50, NIFTYBEES etc.
        if (strike_s in trd and
                option_type in trd and
                (expiry_str[:5].upper() in trd or
                 f"{year2_p}{month3_p}" in trd)):
            tok = (row.get("pSymbol") or "").strip()
            if tok:
                return tok

    return None


def find_futures_token(client, expiry_str: str) -> str | None:
    rows = _get_scrip_master(client)
    if not rows:
        return None

    month3  = expiry_str[2:5]
    year2   = expiry_str[5:]
    day2    = expiry_str[:2]
    day_no0 = str(int(day2))

    candidates = [
        f"NIFTY{expiry_str}FUT",
        f"NIFTY{day_no0}{month3}{year2}FUT",
        f"NIFTY{year2}{month3}FUT",
        f"NIFTY{month3}{year2}FUT",
        "NIFTY-I",
        "NIFTYFUT",
    ]

    fut_rows = [r for r in rows
                if "NIFTY" in (r.get("pTrdSymbol") or "").upper()
                and "FUT" in (r.get("pTrdSymbol") or "").upper()]
    print(f"[FuturesToken] {len(fut_rows)} NIFTY FUT rows in scrip master:")
    for r in fut_rows[:15]:
        print(f"  {r.get('pTrdSymbol',''):30s}  token={r.get('pSymbol','')}")

    for candidate in candidates:
        for row in rows:
            trd = (row.get("pTrdSymbol") or "").strip().upper()
            if trd == candidate.upper():
                tok = (row.get("pSymbol") or "").strip()
                if tok:
                    print(f"[FuturesToken] ✅ '{candidate}' → {tok}")
                    return tok

    month_year = f"{month3}{year2}"
    for row in rows:
        trd = (row.get("pTrdSymbol") or "").strip().upper()
        if "NIFTY" in trd and "FUT" in trd and month_year in trd:
            tok = (row.get("pSymbol") or "").strip()
            if tok:
                print(f"[FuturesToken] ✅ Partial '{trd}' → {tok}")
                return tok

    print(f"[FuturesToken] ❌ Not found for {expiry_str}. Tried: {candidates}")
    return None


# ── NSE holidays ─────────────────────────────────────────

NSE_WEEKLY_EXPIRY_HOLIDAYS = {
    datetime.date(2026, 3, 31),
    datetime.date(2026, 4, 14),
    datetime.date(2026, 6,  2),
}

_PRINTED_EXPIRY_MSGS = set()

def _resolve_weekly_expiry_for_date(base_date, verbose=False):
    days_ahead = (1 - base_date.weekday()) % 7
    tuesday    = base_date + datetime.timedelta(days=days_ahead)
    expiry     = (tuesday - datetime.timedelta(days=1)
                  if tuesday in NSE_WEEKLY_EXPIRY_HOLIDAYS else tuesday)
    if expiry < base_date:
        return _resolve_weekly_expiry_for_date(
            tuesday + datetime.timedelta(days=1), verbose=verbose)
    if verbose and tuesday in NSE_WEEKLY_EXPIRY_HOLIDAYS:
        key = (tuesday, expiry)
        if key not in _PRINTED_EXPIRY_MSGS:
            print(f"  [Expiry] Tuesday {tuesday} holiday → Monday {expiry}")
            _PRINTED_EXPIRY_MSGS.add(key)
    return expiry

def get_next_weekly_expiry(from_date=None) -> datetime.date:
    ref = from_date or datetime.date.today()
    if isinstance(ref, datetime.datetime):
        ref_date = ref.date()
        ref_time = ref.time()
    else:
        ref_date = ref
        ref_time = (datetime.datetime.now().time()
                    if ref_date == datetime.date.today()
                    else datetime.time(0, 0))
    market_close = datetime.time(15, 30)
    expiry = _resolve_weekly_expiry_for_date(ref_date, verbose=True)
    while (ref_date > expiry or
           (ref_date == expiry and ref_time >= market_close)):
        ref_date = expiry + datetime.timedelta(days=1)
        ref_time = datetime.time(0, 0)
        expiry   = _resolve_weekly_expiry_for_date(ref_date, verbose=True)
    return expiry

def get_current_month_expiry() -> datetime.date:
    return get_next_weekly_expiry()

def expiry_to_kotak_str(expiry_date: datetime.date) -> str:
    return expiry_date.strftime("%d%b%y").upper()


# ── Pre-loaded strike cache ───────────────────────────────
# Structure:
# {
#   'CE': [{'strike': 23000, 'token': 'XXXX', 'delta': 0.92, 'oi': 1500000}, ...],
#   'PE': [{'strike': 24000, 'token': 'YYYY', 'delta': 0.90, 'oi': 1300000}, ...],
# }
# Sorted best-first (highest delta first for CE, same for PE).

class OptionManager:

    def __init__(self, client):
        self.client       = client
        self.expiry_date  = get_next_weekly_expiry()
        self.expiry_str   = expiry_to_kotak_str(self.expiry_date)
        self.dte          = _days_to_expiry(self.expiry_date)

        # Pre-loaded strike cache — populated by preload_strikes()
        self._strike_cache: dict[str, list] = {"CE": [], "PE": []}

        print(f"[OptionManager] Expiry: {self.expiry_date} ({self.expiry_str}) "
              f"DTE={self.dte*365:.0f}d  [WEEKLY]")

    # ── STARTUP PRE-LOAD ──────────────────────────────────

    def preload_strikes(self, spot: float):
        """
        Called ONCE at startup after getting first futures LTP.
        Resolves tokens and OI for all candidate strikes at multiple
        ITM depths for both CE and PE.
        Populates self._strike_cache so pick_strike() is instant.
        """
        atm   = round_to_strike(spot, config.STRIKE_STEP)
        sigma = config.IV_PCT / 100.0
        r     = config.RISK_FREE_RATE
        T     = self.dte

        print(f"\n[PreLoad] Starting strike pre-load: spot={spot:.0f} ATM={atm} "
              f"expiry={self.expiry_str}")
        print(f"[PreLoad] ITM depths to scan: {config.PRELOAD_ITM_DEPTHS} pts")

        # Build scrip master OI cache once before scanning strikes
        _build_scrip_oi_cache()

        for direction in ["CE", "PE"]:
            entries = []

            if direction == "CE":
                strikes = [atm - d for d in config.PRELOAD_ITM_DEPTHS]
                extra = []
                for s in strikes:
                    extra += [s - 50, s, s + 50]
                strikes = sorted(set(extra))
            else:
                strikes = [atm + d for d in config.PRELOAD_ITM_DEPTHS]
                extra = []
                for s in strikes:
                    extra += [s - 50, s, s + 50]
                strikes = sorted(set(extra), reverse=True)

            print(f"[PreLoad] {direction}: checking {len(strikes)} candidate strikes...")

            for strike in strikes:
                delta = _bs_delta(spot, strike, T, r, sigma, direction)
                if delta < 0.50:
                    continue

                token = find_option_token(
                    self.client, "NIFTY", self.expiry_str, strike, direction
                )
                if not token:
                    logger.debug(f"[PreLoad] {direction} {strike}: no token found")
                    continue

                # OI from scrip master (instant, no HTTP) + LTP from API
                oi, ltp = fetch_oi_and_ltp(self.client, token)

                entry = {
                    "strike"     : strike,
                    "token"      : token,
                    "delta"      : delta,
                    "oi"         : oi,
                    "ltp"        : ltp,
                    "expiry_str" : self.expiry_str,
                    "expiry_date": self.expiry_date,
                }
                entries.append(entry)

                oi_ok = "✅" if oi >= config.MIN_OI else "⚠️ "
                dl_ok = "✅" if delta >= config.MIN_DELTA else "⚠️ "
                print(f"[PreLoad]   {direction} {strike}: "
                      f"delta={delta:.2f}{dl_ok}  OI={oi:>12,}{oi_ok}  LTP={ltp:.1f}")

                time.sleep(0.05)

            entries.sort(key=lambda x: (x["delta"], x["oi"]), reverse=True)
            self._strike_cache[direction] = entries
            print(f"[PreLoad] {direction}: {len(entries)} strikes cached ✅")

        print(f"[PreLoad] ✅ Pre-load complete. "
              f"CE={len(self._strike_cache['CE'])} strikes, "
              f"PE={len(self._strike_cache['PE'])} strikes cached.\n")

    def refresh_oi(self):
        """
        Refresh OI values in the cache (called every ~30 min if needed).
        Token resolution is NOT re-done — only OI values updated.
        """
        for direction in ["CE", "PE"]:
            for entry in self._strike_cache[direction]:
                entry["oi"] = fetch_oi(self.client, entry["token"])
        print("[OptionManager] OI refreshed in cache")

    # ── SIGNAL-TIME PICK (instant — reads from cache) ─────

    def pick_strike(self, spot: float, direction: str) -> dict | None:
        """
        Pick best pre-cached ITM strike for direction at signal time.
        Fast — reads from _strike_cache, no HTTP calls.
        Falls back to live scan if cache has nothing passing filters.
        """
        atm   = round_to_strike(spot, config.STRIKE_STEP)
        sigma = config.IV_PCT / 100.0
        r     = config.RISK_FREE_RATE
        T     = self.dte

        candidates = self._strike_cache.get(direction, [])

        if candidates:
            # Re-compute delta at current spot (spot may have moved)
            for c in candidates:
                c["delta"] = _bs_delta(spot, c["strike"], T, r, sigma, direction)

            # Filter: delta >= 0.85, OI >= 12L, strike is still ITM
            valid = [
                c for c in candidates
                if (c["delta"] >= config.MIN_DELTA and
                    c["oi"] >= config.MIN_OI and
                    (direction == "CE" and c["strike"] < atm or
                     direction == "PE" and c["strike"] > atm))
            ]

            if valid:
                # Pick highest delta (most ITM that passes both filters)
                best = valid[0]
                print(f"[Strike] ✅ Cache hit — {direction} {best['strike']} "
                      f"delta={best['delta']:.2f} OI={best['oi']:,}")
                return best

            # Try relaxed: any passing delta, ignore OI
            relaxed = [
                c for c in candidates
                if c["delta"] >= config.MIN_DELTA and
                (direction == "CE" and c["strike"] < atm or
                 direction == "PE" and c["strike"] > atm)
            ]
            if relaxed:
                best = relaxed[0]
                print(f"[Strike] ⚠️  Cache OI-relaxed — {direction} {best['strike']} "
                      f"delta={best['delta']:.2f} OI={best['oi']:,}")
                return best

        # Live fallback (cache miss or all strikes now OTM)
        print(f"[Strike] Cache miss for {direction} — running live scan...")
        return self._live_scan(spot, direction)

    def _live_scan(self, spot: float, direction: str) -> dict | None:
        """Fallback live scan — same logic as v2 pick_strike."""
        atm   = round_to_strike(spot, config.STRIKE_STEP)
        sigma = config.IV_PCT / 100.0
        r     = config.RISK_FREE_RATE
        T     = self.dte

        if direction == "CE":
            candidates = range(atm - 300, atm + config.STRIKE_STEP, config.STRIKE_STEP)
        else:
            candidates = range(atm + 300, atm - config.STRIKE_STEP, -config.STRIKE_STEP)

        best = None
        steps = 0
        for strike in candidates:
            delta = _bs_delta(spot, strike, T, r, sigma, direction)
            if delta < 0.50:
                continue
            token = find_option_token(self.client, "NIFTY", self.expiry_str,
                                      strike, direction)
            if not token:
                continue
            oi = fetch_oi(self.client, token)
            if oi >= config.MIN_OI and delta >= config.MIN_DELTA:
                return {"strike": strike, "token": token, "delta": delta,
                        "oi": oi, "expiry_str": self.expiry_str,
                        "expiry_date": self.expiry_date}
            if best is None or delta > best["delta"]:
                best = {"strike": strike, "token": token, "delta": delta,
                        "oi": oi, "expiry_str": self.expiry_str,
                        "expiry_date": self.expiry_date}
            steps += 1
            if steps > config.MAX_OI_WALK_STEPS:
                break
        return best

    # ── Order placement ───────────────────────────────────

    def place_buy_order(self, token: str, strike: int, direction: str,
                        ltp: float) -> dict | None:
        qty      = config.LOTS * config.LOT_SIZE
        limit_px = round(ltp + config.BUY_LIMIT_BUFFER, 2)
        print(f"\n[Order] BUY {direction} {strike} | qty={qty} "
              f"ltp={ltp:.2f} limit={limit_px:.2f}")

        if config.PAPER_TRADE:
            print(f"[Order] PAPER FILL @ {limit_px:.2f}")
            return {"fill_price": limit_px, "qty": qty, "order_id": "PAPER"}

        try:
            resp = self.client.place_order(
                exchange_segment="nse_fo", product="NRML",
                price=str(limit_px), order_type="L",
                quantity=str(qty), validity="DAY",
                trading_symbol=self._build_symbol(strike, direction),
                transaction_type="B", amo="NO",
                disclosed_quantity="0", market_protection="0",
                pf="N", trigger_price="0", tag=None,
            )
            order_id = self._extract_order_id(resp)
            if not order_id:
                logger.error(f"Buy order failed: {resp}")
                return None
            return self._wait_for_fill(order_id, qty, config.ORDER_FILL_TIMEOUT_SECS)
        except Exception as e:
            logger.error(f"place_buy_order: {e}")
            return None

    def place_exit_order(self, token: str, strike: int, direction: str,
                         qty: int, reason: str = "") -> float | None:
        print(f"\n[Order] SELL {direction} {strike} qty={qty} reason={reason}")

        if config.PAPER_TRADE:
            return None

        for attempt in range(config.EXIT_RETRY_ATTEMPTS):
            try:
                resp = self.client.place_order(
                    exchange_segment="nse_fo", product="NRML",
                    price="0", order_type="MKT",
                    quantity=str(qty), validity="DAY",
                    trading_symbol=self._build_symbol(strike, direction),
                    transaction_type="S", amo="NO",
                    disclosed_quantity="0", market_protection="0",
                    pf="N", trigger_price="0", tag=None,
                )
                order_id = self._extract_order_id(resp)
                if not order_id:
                    time.sleep(2)
                    continue
                fill = self._wait_for_fill(order_id, qty, config.EXIT_FILL_TIMEOUT_SECS)
                if fill:
                    return fill.get("fill_price")
            except Exception as e:
                logger.error(f"place_exit_order attempt {attempt+1}: {e}")
                time.sleep(2)
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

    def _wait_for_fill(self, order_id: str, qty: int, timeout: float) -> dict | None:
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
                                return None
            except Exception as e:
                logger.debug(f"Poll: {e}")
            time.sleep(config.ORDER_STATUS_POLL_SECS)
        try:
            self.client.cancel_order(order_id=order_id)
        except Exception:
            pass
        return None

    @staticmethod
    def calc_trade_cost(entry_price: float, exit_price: float, qty: int) -> float:
        buy_tv  = entry_price * qty
        sell_tv = exit_price  * qty
        total   = buy_tv + sell_tv
        brok    = config.BROKERAGE_PER_ORDER * 2
        stt     = total * config.STT_PCT
        exc     = total * config.EXCHANGE_TXN_PCT
        sebi    = total * config.SEBI_PCT
        gst     = (brok + exc + sebi) * config.GST_PCT
        stamp   = buy_tv * config.STAMP_DUTY_PCT
        return round(brok + stt + exc + sebi + gst + stamp, 2)
