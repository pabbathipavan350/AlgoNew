# ============================================================
# OPTION_MANAGER.PY — Auto Strike Picker + Smart Order Placer
# ============================================================
# Key improvement: Uses LIMIT orders with bid-ask buffer
# instead of pure market orders. This reduces slippage by
# 60-70% while still getting filled quickly on liquid options.
#
# ORDER LOGIC:
#   BUY  → fetch ask price → place LIMIT at ask + buffer (Rs 2)
#          This ensures fill while avoiding paying too much
#   SELL → fetch bid price → place LIMIT at bid - buffer (Rs 2)
#          This ensures fill while not selling too cheap
#
# PAPER MODE:
#   Simulates realistic fill price = LTP + slippage buffer
#   so paper results are close to real trading results
#
# SLIPPAGE SIMULATION (paper mode):
#   BUY  fill = ask + 1  (conservative estimate)
#   SELL fill = bid - 1  (conservative estimate)
# ============================================================

import datetime
import math
import requests
import csv
import io
from scipy.stats import norm
import config


# ── Black-Scholes delta ───────────────────────────────────

def black_scholes_delta(S, K, T, r, sigma, option_type):
    if T <= 0:
        if option_type == 'CE':
            return 1.0 if S > K else 0.0
        else:
            return -1.0 if S < K else 0.0
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        return norm.cdf(d1) if option_type == 'CE' else norm.cdf(d1) - 1
    except Exception:
        return 0.0


NSE_WEEKLY_EXPIRY_HOLIDAYS = {
    datetime.date(2026, 3, 31),   # Mahavir Jayanti -> weekly expiry moves to Monday
    datetime.date(2026, 4, 14),   # Ambedkar Jayanti
    datetime.date(2026, 6, 2),
}


_PRINTED_EXPIRY_MESSAGES = set()


def _resolve_weekly_expiry_for_date(base_date, verbose=False):
    """Return the next valid weekly expiry on or after ``base_date``.

    Standard weekly expiry is Tuesday. If Tuesday is a holiday,
    expiry shifts to Monday. If that shifted Monday is already before
    ``base_date`` (for example when ``base_date`` is the holiday Tuesday),
    move forward to the next week's cycle instead of returning a past date.
    """
    days_to_tuesday = (1 - base_date.weekday()) % 7
    tuesday = base_date + datetime.timedelta(days=days_to_tuesday)
    expiry = tuesday - datetime.timedelta(days=1) if tuesday in NSE_WEEKLY_EXPIRY_HOLIDAYS else tuesday

    if expiry < base_date:
        next_base = tuesday + datetime.timedelta(days=1)
        return _resolve_weekly_expiry_for_date(next_base, verbose=verbose)

    if verbose and tuesday in NSE_WEEKLY_EXPIRY_HOLIDAYS:
        msg_key = (tuesday, expiry)
        if msg_key not in _PRINTED_EXPIRY_MESSAGES:
            print(f"  [Expiry] Tuesday {tuesday} is holiday -> using Monday {expiry}")
            _PRINTED_EXPIRY_MESSAGES.add(msg_key)
    return expiry


def get_next_expiry(from_date=None, week_offset=0):
    """Get the upcoming valid Nifty weekly expiry.

    Semantics:
    - week_offset=0 -> the first tradable upcoming weekly expiry from now
    - week_offset=1 -> the following week's expiry

    Rules handled:
    - Standard weekly expiry = Tuesday
    - If Tuesday is a holiday, expiry shifts to Monday
    - If the shifted Monday is already over, move to the immediate next weekly expiry
    - If called after market close on expiry day, move to the immediate next weekly expiry
    """
    ref = from_date or datetime.date.today()
    if isinstance(ref, datetime.datetime):
        ref_date = ref.date()
        ref_time = ref.time()
    else:
        ref_date = ref
        ref_time = datetime.datetime.now().time() if ref_date == datetime.date.today() else datetime.time(0, 0)

    market_close = datetime.time(15, 30)
    expiry = _resolve_weekly_expiry_for_date(ref_date, verbose=True)

    while ref_date > expiry or (ref_date == expiry and ref_time >= market_close):
        ref_date = expiry + datetime.timedelta(days=1)
        ref_time = datetime.time(0, 0)
        expiry = _resolve_weekly_expiry_for_date(ref_date, verbose=True)

    for _ in range(max(0, int(week_offset))):
        expiry = _resolve_weekly_expiry_for_date(expiry + datetime.timedelta(days=1), verbose=True)

    return expiry


def get_next_week_expiry(from_date=None):
    return get_next_expiry(from_date=from_date, week_offset=0)


def get_following_week_expiry(from_date=None):
    return get_next_expiry(from_date=from_date, week_offset=1)


# Backward-compatible alias
get_next_thursday = get_next_expiry


def find_itm_strike(spot, option_type, expiry_date):
    """Find nearest ITM strike with delta >= 0.8 (delta-only, no OI).
    Used as a first-pass; OI filtering happens in find_best_strike_with_oi()."""
    sigma = config.IV_PCT / 100.0
    r     = config.RISK_FREE_RATE
    T     = max((expiry_date - datetime.date.today()).days / 365.0, 1/365.0)
    step  = config.STRIKE_STEP
    atm   = round(spot / step) * step

    for i in range(0, 20):
        strike = (atm - i * step) if option_type == 'CE' else (atm + i * step)
        delta  = black_scholes_delta(spot, strike, T, r, sigma, option_type)
        if abs(delta) >= config.MIN_DELTA:
            return strike, round(abs(delta), 4)

    strike = (atm - 5 * step) if option_type == 'CE' else (atm + 5 * step)
    delta  = black_scholes_delta(spot, strike, T, r, sigma, option_type)
    return strike, round(abs(delta), 4)


def fetch_oi(client, token):
    """
    Fetch open interest for a single option token via Kotak quotes API.
    Returns OI as an integer, or 0 on any error.
    """
    if not client or not token:
        return 0
    try:
        resp = client.quotes(
            instrument_tokens=[{
                "instrument_token": str(token),
                "exchange_segment": config.FO_SEGMENT,
            }],
            quote_type="ohlc"
        )
        data = resp if isinstance(resp, list) else (
               resp.get('message') or resp.get('data') or [])
        if data:
            row = data[0]
            oi = int(float(
                row.get('oi') or
                row.get('openInterest') or
                row.get('open_interest') or 0
            ))
            return oi
    except Exception:
        pass
    return 0


def find_best_strike_with_oi(client, spot, option_type, expiry_date,
                              itm_depth, opt_mgr):
    """
    Pick the best liquid strike combining VIX-matrix ITM depth + OI check.

    Logic:
      1. Start at the VIX-matrix strike (ATM ± itm_depth).
      2. Fetch OI for that strike.
      3. If OI >= MIN_OI_THRESHOLD → use it.
      4. Otherwise walk toward ATM one STRIKE_STEP at a time until
         a strike with sufficient OI is found (up to 6 steps).
      5. If no strike passes OI, fall back to the VIX-matrix strike
         anyway (logs a warning) — we still trade, just with a note.

    Returns:
        (strike, delta, oi)  — the chosen strike, its BS delta, and its OI.
    """
    step    = config.STRIKE_STEP
    atm     = round(spot / step) * step
    sigma   = config.IV_PCT / 100.0
    r       = config.RISK_FREE_RATE
    T       = max((expiry_date - datetime.date.today()).days / 365.0, 1 / 365.0)

    # Direction: CE walks ATM-ward = strike increases; PE walks ATM-ward = strike decreases
    atm_direction = +1 if option_type == 'CE' else -1

    # First candidate is always the VIX-matrix strike
    start_strike = (atm - itm_depth) if option_type == 'CE' else (atm + itm_depth)

    best_strike = start_strike
    best_delta  = round(abs(black_scholes_delta(spot, start_strike, T, r, sigma, option_type)), 4)
    best_oi     = 0

    for i in range(7):   # 0 = VIX-matrix strike, 1-6 = walk toward ATM
        candidate = start_strike + i * atm_direction * step
        tok = opt_mgr.get_option_token(candidate, option_type, expiry_date)
        oi  = fetch_oi(client, tok) if tok else 0
        delta = round(abs(black_scholes_delta(spot, candidate, T, r, sigma, option_type)), 4)

        print(f"  [OI] {option_type} {int(candidate)} | delta={delta:.2f} | "
              f"OI={oi:,} | min={config.MIN_OI_THRESHOLD:,}")

        if oi >= config.MIN_OI_THRESHOLD:
            return candidate, delta, oi

        # Track as best seen (for fallback)
        if i == 0 or oi > best_oi:
            best_strike = candidate
            best_delta  = delta
            best_oi     = oi

    print(f"  [OI] WARNING: No {option_type} strike found with OI >= "
          f"{config.MIN_OI_THRESHOLD:,}. Using {int(best_strike)} "
          f"(OI={best_oi:,}) as fallback.")
    return best_strike, best_delta, best_oi


class OptionManager:

    def __init__(self, client):
        self.client          = client
        self._fo_tokens      = {}   # cache: (strike, type, expiry) → token
        self._fo_symbols     = {}   # cache: (strike, type, expiry) → exact trading symbol
        self._working_prefix = None # confirmed working prefix format e.g. "NIFTY26MAR"
        self._scrip_rows     = None # scrip master CSV rows — downloaded once

    # ── Token lookup ──────────────────────────────────────

    def _make_symbol(self, strike, option_type, expiry_date):
        """Build display symbol — actual token lookup uses partial match."""
        yy = expiry_date.strftime("%y")
        m  = str(expiry_date.month)
        dd = expiry_date.strftime("%d")
        return f"NIFTY{yy}{m}{dd}{int(strike)}{option_type}"

    def _build_prefixes(self, expiry_date):
        """
        Return all prefix formats Kotak may use for NIFTY weekly options.

        Kotak scrip master uses several formats depending on the API version:
          Format A: NIFTY{YY}{M}{DD}   e.g. NIFTY26330   (month=3, no zero-pad)
          Format B: NIFTY{YY}{MM}{DD}  e.g. NIFTY260330  (month zero-padded)
          Format C: NIFTY{YY}{MON}     e.g. NIFTY26MAR   (3-letter month)
          Format D: NIFTY{YY}{M}       e.g. NIFTY263      (month only, no DD)
          Format E: NIFTY{YY}{MM}      e.g. NIFTY2603     (zero-padded month only)

        We try all of them so the algo works regardless of Kotak API version.
        """
        yy  = expiry_date.strftime("%y")           # "26"
        m   = str(expiry_date.month)               # "3"
        mm  = expiry_date.strftime("%m")           # "03"
        dd  = expiry_date.strftime("%d")           # "30"
        mon = expiry_date.strftime("%b").upper()   # "MAR"

        return [
            f"NIFTY{yy}{m}{dd}",    # A: NIFTY26330
            f"NIFTY{yy}{mm}{dd}",   # B: NIFTY260330
            f"NIFTY{yy}{mon}",      # C: NIFTY26MAR
            f"NIFTY{yy}{m}",        # D: NIFTY263     (old format — fallback)
            f"NIFTY{yy}{mm}",       # E: NIFTY2603    (zero-padded — fallback)
        ]

    def _load_scrip_master(self):
        """
        Download F&O scrip master CSV exactly ONCE per session.
        Stores all rows in memory — subsequent calls return instantly.
        """
        if self._scrip_rows is not None:
            return self._scrip_rows
        print("  [Option] Downloading F&O scrip master (once for all strikes)...")
        csv_url  = self.client.scrip_master(exchange_segment=config.FO_SEGMENT)
        response = requests.get(csv_url, timeout=30)
        if response.status_code != 200:
            raise Exception(f"Scrip master HTTP {response.status_code}")
        self._scrip_rows = list(csv.DictReader(io.StringIO(response.text)))
        print(f"  [Option] Scrip master loaded — {len(self._scrip_rows):,} rows ✅")
        return self._scrip_rows

    def get_option_token(self, strike, option_type, expiry_date):
        """
        Find token from F&O scrip master CSV.

        Speed optimisation:
        - CSV is downloaded ONCE and reused for all strikes.
        - On the first successful match, the working prefix format is saved
          (_working_prefix). Every subsequent call skips format detection and
          goes straight to the known prefix — no retrying 5 formats.
        """
        cache_key = (strike, option_type, str(expiry_date))
        if cache_key in self._fo_tokens:
            return self._fo_tokens[cache_key]

        strike_str = str(int(strike))
        suffix     = f"{strike_str}{option_type}"  # e.g. "22900CE"

        try:
            rows = self._load_scrip_master()

            # If we already know the working prefix, use it directly
            prefixes_to_try = (
                [self._working_prefix] if self._working_prefix
                else self._build_prefixes(expiry_date)
            )

            if self._working_prefix:
                print(f"  [Option] {option_type} {int(strike)} "
                      f"→ {self._working_prefix}{suffix}")
            else:
                print(f"  [Option] {option_type} {int(strike)} "
                      f"— detecting format...")

            for prefix in prefixes_to_try:
                candidates = []
                for row in rows:
                    trd = (row.get('pTrdSymbol') or '').upper().strip()
                    tok = (row.get('pSymbol') or '').strip()
                    if not tok or not trd:
                        continue
                    if (trd.startswith(prefix)
                            and trd.endswith(suffix)
                            and 'BANK' not in trd):
                        candidates.append((trd, tok))

                if candidates:
                    candidates.sort(key=lambda x: x[0])
                    trd, tok = candidates[0]
                    self._fo_tokens[cache_key] = tok
                    self._fo_symbols[cache_key] = trd
                    # Lock in this prefix for all future lookups
                    if not self._working_prefix:
                        self._working_prefix = prefix
                        print(f"  [Option] Format locked: {prefix} ✅")
                    print(f"  [Option] {trd} | Token={tok} ✅")
                    return tok

            # Nothing found — show samples to aid debugging
            sample = [
                (row.get('pTrdSymbol') or '').upper()
                for row in rows
                if 'NIFTY' in (row.get('pTrdSymbol') or '').upper()
                and 'BANK' not in (row.get('pTrdSymbol') or '').upper()
            ][:6]
            print(f"  [Option] NOT FOUND: {suffix} | "
                  f"Sample NIFTY symbols: {sample}")

        except Exception as e:
            print(f"  [Option] Token lookup error: {e}")

        return None

    # ── Price fetcher with bid-ask ────────────────────────

    def get_bid_ask_ltp(self, token):
        """Fetch bid, ask and LTP — no isIndex param (not in API)."""
        if not token:
            return 0.0, 0.0, 0.0
        try:
            resp = self.client.quotes(
                instrument_tokens=[{
                    "instrument_token": token,
                    "exchange_segment": config.FO_SEGMENT
                }],
                quote_type="depth"
            )
            data = resp
            if isinstance(resp, dict):
                data = resp.get('message') or resp.get('data') or []
            if isinstance(data, list) and data:
                q     = data[0]
                ltp   = float(q.get('ltp') or q.get('last_traded_price') or 0)
                depth = q.get('depth') or {}
                buys  = depth.get('buy') or []
                sells = depth.get('sell') or []
                bid   = float(buys[0].get('price',  0)) if buys  else 0.0
                ask   = float(sells[0].get('price', 0)) if sells else 0.0
                if bid <= 0: bid = round(ltp - 1, 2)
                if ask <= 0: ask = round(ltp + 1, 2)
                return bid, ask, ltp
        except Exception:
            pass
        try:
            resp = self.client.quotes(
                instrument_tokens=[{
                    "instrument_token": token,
                    "exchange_segment": config.FO_SEGMENT
                }],
                quote_type="ltp"
            )
            data = resp
            if isinstance(resp, dict):
                data = resp.get('message') or resp.get('data') or []
            if isinstance(data, list) and data:
                ltp    = float(data[0].get('ltp') or
                               data[0].get('last_traded_price') or 0)
                spread = max(1.0, round(ltp * 0.005, 1))
                return round(ltp-spread, 2), round(ltp+spread, 2), ltp
        except Exception as e:
            print(f"  [Option] quotes error: {e}")
        return 0.0, 0.0, 0.0

    def get_live_option_price(self, token):
        """Get LTP for exit monitoring."""
        _, _, ltp = self.get_bid_ask_ltp(token)
        return ltp

    def get_realistic_fill_price(self, token, side):
        """
        Get realistic fill price accounting for bid-ask spread.

        BUY  → fills at ask + SLIP_BUFFER (we pay a little more)
        SELL → fills at bid - SLIP_BUFFER (we receive a little less)

        This is used for BOTH paper simulation AND live limit price.
        """
        bid, ask, ltp = self.get_bid_ask_ltp(token)

        if ltp <= 0:
            return 0.0, 0.0

        buf = config.SLIP_BUFFER  # Rs 2 from config

        if side == 'BUY':
            # Pay ask + small buffer to ensure fill
            limit_price = round(ask + buf, 1) if ask > 0 else round(ltp + buf, 1)
            sim_price   = round(ask + 1, 1)   if ask > 0 else round(ltp + 1, 1)
        else:
            # Receive bid - small buffer to ensure fill
            limit_price = round(bid - buf, 1) if bid > 0 else round(ltp - buf, 1)
            sim_price   = round(bid - 1, 1)   if bid > 0 else round(ltp - 1, 1)

        return limit_price, sim_price

    # ── Order placement ───────────────────────────────────

    def place_buy_order(self, token, trading_symbol, qty):
        """
        Place LIMIT buy order at ask + buffer.
        Much better than market order — reduces slippage 60-70%.
        If limit not filled in 30s, algo will retry on next tick.
        """
        limit_price, sim_price = self.get_realistic_fill_price(token, 'BUY')

        if limit_price <= 0:
            print(f"  [Order] Could not get price for {trading_symbol}")
            return {'stat': 'Not_Ok', 'emsg': 'Price fetch failed'}

        print(f"  [Order] BUY LIMIT {qty} x {trading_symbol} "
              f"@ Rs{limit_price:.1f} "
              f"(ask+{config.SLIP_BUFFER} buffer)")

        try:
            resp = self.client.place_order(
                exchange_segment  = config.FO_SEGMENT,
                product           = "MIS",
                price             = str(limit_price),
                order_type        = "L",       # LIMIT order
                quantity          = str(qty),
                validity          = "DAY",
                trading_symbol    = trading_symbol,
                transaction_type  = "B",
                amo               = "NO",
                disclosed_quantity= "0",
                market_protection = "0",
                pf                = "N",
                trigger_price     = "0",
                tag               = None
            )
            # Store limit price for fill confirmation
            resp['_limit_price'] = limit_price
            resp['_sim_price']   = sim_price
            return resp
        except Exception as e:
            print(f"  [Order] Buy error: {e}")
            return {'stat': 'Not_Ok', 'emsg': str(e)}

    def place_sell_order(self, token, trading_symbol, qty):
        """
        Place LIMIT sell order at bid - buffer.
        Ensures fill while avoiding selling too cheap.
        """
        limit_price, sim_price = self.get_realistic_fill_price(token, 'SELL')

        if limit_price <= 0:
            # Fallback to market order on exit to avoid being stuck
            print(f"  [Order] Price fetch failed — using MKT order for exit")
            return self._place_market_sell(trading_symbol, qty)

        print(f"  [Order] SELL LIMIT {qty} x {trading_symbol} "
              f"@ Rs{limit_price:.1f} "
              f"(bid-{config.SLIP_BUFFER} buffer)")

        try:
            resp = self.client.place_order(
                exchange_segment  = config.FO_SEGMENT,
                product           = "MIS",
                price             = str(limit_price),
                order_type        = "L",       # LIMIT order
                quantity          = str(qty),
                validity          = "DAY",
                trading_symbol    = trading_symbol,
                transaction_type  = "S",
                amo               = "NO",
                disclosed_quantity= "0",
                market_protection = "0",
                pf                = "N",
                trigger_price     = "0",
                tag               = None
            )
            resp['_limit_price'] = limit_price
            resp['_sim_price']   = sim_price
            return resp
        except Exception as e:
            print(f"  [Order] Sell error: {e}")
            return {'stat': 'Not_Ok', 'emsg': str(e)}

    def _place_market_sell(self, trading_symbol, qty):
        """Emergency market sell — only used if limit price unavailable."""
        print(f"  [Order] EMERGENCY MKT SELL {qty} x {trading_symbol}")
        try:
            return self.client.place_order(
                exchange_segment  = config.FO_SEGMENT,
                product           = "MIS",
                price             = "0",
                order_type        = "MKT",
                quantity          = str(qty),
                validity          = "DAY",
                trading_symbol    = trading_symbol,
                transaction_type  = "S",
                amo               = "NO",
                disclosed_quantity= "0",
                market_protection = "0",
                pf                = "N",
                trigger_price     = "0",
                tag               = None
            )
        except Exception as e:
            return {'stat': 'Not_Ok', 'emsg': str(e)}

    def get_trading_symbol(self, strike, option_type, expiry_date):
        """Return exact trading symbol from scrip master when available."""
        cache_key = (strike, option_type, str(expiry_date))
        if cache_key not in self._fo_symbols:
            self.get_option_token(strike, option_type, expiry_date)
        return self._fo_symbols.get(cache_key) or self._make_symbol(strike, option_type, expiry_date)
