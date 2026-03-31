# ============================================================
# NSE_VWAP_FETCHER.PY  — Fixed 2026-03-30
# ============================================================
# ROOT CAUSE OF 403:
#   NSE uses Akamai bot-protection. urllib.request gets a valid-
#   looking cookie page but the cookies are rejected on the API
#   call because urllib opens a NEW connection for each request
#   (no shared session jar). NSE's bot-filter sees mismatched
#   TLS fingerprint + no persistent cookie -> 403 Forbidden.
#
# FIX:
#   Use requests.Session() so cookies set by the NSE home page
#   visit are automatically sent with every subsequent API call.
#
# VWAP FORMULA (TradingView-accurate):
#   Reset at 9:15 AM. Per 1-min candle: TP = (H+L+C)/3
#   VWAP = Sigma(TP * Volume) / Sigma(Volume)
#   Volume = totalTradedVolume DELTA between syncs (NOT ltq).
# ============================================================

import datetime
import time

try:
    import requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False
    print("  [NSE] 'requests' not installed. Run: pip install requests")

NSE_OC_URL   = "https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
NSE_HOME_URL = "https://www.nseindia.com/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
    "DNT":             "1",
    "Upgrade-Insecure-Requests": "1",
}

_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://www.nseindia.com/",
    "Connection":      "keep-alive",
}


class NseVwapFetcher:
    """
    Fetches NSE option chain for a given symbol and returns per-strike
    LTP + totalTradedVolume for TradingView-accurate VWAP calculation.

    KEY FIX: requests.Session() persists cookies across calls — this
    is what prevents the 403 Forbidden error from NSE's bot filter.
    """

    def __init__(self, cookie_refresh_mins=8):
        if _REQUESTS_OK:
            self._session = requests.Session()
            self._session.headers.update(_HEADERS)
        else:
            self._session = None

        self._last_cookie_time  = None
        self._cookie_ttl        = datetime.timedelta(minutes=cookie_refresh_mins)
        self._consecutive_fails = 0

    def _refresh_session(self):
        """Visit NSE home to seed session cookies (Akamai bypass)."""
        if not _REQUESTS_OK or not self._session:
            return False
        try:
            resp = self._session.get(NSE_HOME_URL, timeout=10, headers=_HEADERS)
            resp.raise_for_status()
            time.sleep(0.5)   # brief pause mimics browser behaviour
            self._last_cookie_time  = datetime.datetime.now()
            self._consecutive_fails = 0
            return True
        except Exception as e:
            print(f"  [NSE] Session refresh failed: {e}")
            self._consecutive_fails += 1
            return False

    def _ensure_session(self):
        now = datetime.datetime.now()
        if (self._last_cookie_time is None
                or now - self._last_cookie_time > self._cookie_ttl):
            self._refresh_session()

    def fetch_option_chain(self, symbol="NIFTY", expiry_filter=None):
        """
        Returns list of dicts: {strike, type, ltp, volume, oi, expiry}
        Returns [] on any error.
        """
        if not _REQUESTS_OK or not self._session:
            return []

        if self._consecutive_fails >= 5:
            print("  [NSE] Too many consecutive failures — skipping fetch")
            return []

        self._ensure_session()

        url = NSE_OC_URL.format(symbol=symbol)
        try:
            resp = self._session.get(url, timeout=10, headers=_API_HEADERS)

            # Auto-retry once on 403
            if resp.status_code == 403:
                print("  [NSE] 403 — refreshing session and retrying")
                if self._refresh_session():
                    time.sleep(1)
                    resp = self._session.get(url, timeout=10, headers=_API_HEADERS)

            if resp.status_code != 200:
                print(f"  [NSE] HTTP {resp.status_code} fetching option chain")
                self._consecutive_fails += 1
                return []

        except Exception as e:
            print(f"  [NSE] Fetch error: {e}")
            self._consecutive_fails += 1
            return []

        try:
            data = resp.json()
        except Exception:
            print("  [NSE] JSON parse error")
            self._consecutive_fails += 1
            return []

        records = data.get("records", {})
        raw     = records.get("data", [])

        if not raw:
            print("  [NSE] Empty option chain response")
            self._consecutive_fails += 1
            return []

        self._consecutive_fails = 0

        if expiry_filter is None:
            expiries      = records.get("expiryDates", [])
            expiry_filter = expiries[0] if expiries else None

        rows = []
        for item in raw:
            expiry = item.get("expiryDate", "")
            if expiry_filter and expiry != expiry_filter:
                continue
            strike = item.get("strikePrice")
            if strike is None:
                continue
            strike = int(strike)

            for opt_type in ("CE", "PE"):
                od = item.get(opt_type)
                if not od:
                    continue
                ltp    = float(od.get("lastPrice")       or 0)
                volume = int(od.get("totalTradedVolume") or 0)
                oi     = int(od.get("openInterest")      or 0)
                if ltp <= 0:
                    continue
                rows.append({
                    "strike": strike,
                    "type":   opt_type,
                    "ltp":    ltp,
                    "volume": volume,
                    "oi":     oi,
                    "expiry": expiry,
                })

        return rows

    def sync_engine(self, engine, ce_tokens: dict, pe_tokens: dict,
                    symbol="NIFTY", expiry_filter=None):
        """Fetch NSE chain and push ltp+volume into StrategyEngine."""
        rows = self.fetch_option_chain(symbol, expiry_filter)
        if not rows:
            return 0

        synced = 0
        for row in rows:
            strike   = row["strike"]
            opt_type = row["type"]
            tok      = (str(ce_tokens.get(strike, "")) if opt_type == "CE"
                        else str(pe_tokens.get(strike, "")))
            if not tok:
                continue
            engine.sync_quote_full(tok, row["ltp"], row["volume"])
            synced += 1

        return synced


# Quick standalone test
if __name__ == "__main__":
    if not _REQUESTS_OK:
        print("Run: pip install requests")
        exit(1)
    fetcher = NseVwapFetcher()
    print("Fetching NIFTY option chain (requests.Session)...")
    rows = fetcher.fetch_option_chain("NIFTY")
    if rows:
        print(f"OK — got {len(rows)} rows")
        strikes = sorted(set(r["strike"] for r in rows))
        mid     = strikes[len(strikes) // 2]
        for r in [r for r in rows if abs(r["strike"] - mid) <= 200][:10]:
            print(f"  {r['type']} {r['strike']:6d}  LTP={r['ltp']:8.2f}  Vol={r['volume']:8d}")
    else:
        print("No data — NSE may be closed or blocking")
