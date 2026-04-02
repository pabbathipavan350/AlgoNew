# ============================================================
# TOKENS.PY — Resolve watchlist symbols to Kotak tokens
# FIX 4: Only verified Nifty50 hardcoded — everything else
#         resolved via scrip master to avoid wrong token issues
# ============================================================

import requests, csv, io, logging, json, os, config

logger = logging.getLogger(__name__)

# Verified Nifty50 tokens ONLY
# DO NOT add other stocks here — wrong tokens cause wrong trades (FIVESTAR bug)
VERIFIED_TOKENS = {
    "RELIANCE":"2885","TCS":"11536","HDFCBANK":"1333","INFY":"1594",
    "ICICIBANK":"4963","HINDUNILVR":"1394","ITC":"1660","SBIN":"3045",
    "BHARTIARTL":"10604","AXISBANK":"5900","KOTAKBANK":"1922","LT":"11483",
    "HCLTECH":"7229","ASIANPAINT":"236","MARUTI":"10999","BAJFINANCE":"317",
    "WIPRO":"3787","ULTRACEMCO":"11532","TITAN":"3506","POWERGRID":"14977",
    "NTPC":"11630","ONGC":"2475","SUNPHARMA":"3351","NESTLEIND":"17963",
    "TATAMOTORS":"3432","TATASTEEL":"3499","JSWSTEEL":"11723","ADANIENT":"25",
    "ADANIPORTS":"15083","BAJAJFINSV":"16675","DRREDDY":"881","CIPLA":"694",
    "EICHERMOT":"910","TECHM":"13538","GRASIM":"1232","INDUSINDBK":"5258",
    "HINDALCO":"1363","COALINDIA":"20374","BRITANNIA":"547","TATACONSUM":"3545",
    "APOLLOHOSP":"157","DIVISLAB":"10940","SBILIFE":"21808","HDFCLIFE":"467",
    "M&M":"2031","BAJAJ-AUTO":"16669","SHRIRAMFIN":"21776","TRENT":"3539",
    "HEROMOTOCO":"1348","BPCL":"526",
}

_token_cache      = {}   # symbol → token
_name_cache       = {}   # token → symbol
_prev_close_cache = {}   # symbol → prev close price


def get_prev_close(symbol: str) -> float:
    return _prev_close_cache.get(symbol.upper(), 0.0)


def load_prev_closes(symbols: list, client=None):
    """
    Fetch previous close via quotes(ohlc) API.
    CONFIRMED: response key = exchange_token, prev close = q["ohlc"]["close"]
    Saves cache to prev_close_cache.json for backup.
    """
    global _prev_close_cache
    CACHE_FILE = "prev_close_cache.json"

    if client is not None:
        print(f"  Fetching prev close via quotes(ohlc)...")
        loaded   = 0
        sym_list = list(symbols)

        for i in range(0, len(sym_list), 20):
            batch   = sym_list[i:i+20]
            tokens  = []
            tok_sym = {}
            for sym in batch:
                tok = _token_cache.get(sym)
                if tok:
                    tokens.append({"instrument_token": tok,
                                   "exchange_segment": config.EXCHANGE_SEGMENT_EQ})
                    tok_sym[tok] = sym
            if not tokens:
                continue
            try:
                resp = client.quotes(instrument_tokens=tokens, quote_type="ohlc")
                if isinstance(resp, list):
                    for q in resp:
                        tok  = str(q.get("exchange_token", ""))
                        ohlc = q.get("ohlc", {})
                        pc   = float(ohlc.get("close", 0) or 0)
                        sym  = tok_sym.get(tok, "")
                        if sym and pc > 0:
                            _prev_close_cache[sym] = pc
                            loaded += 1
            except Exception as e:
                logger.debug(f"Quotes batch error: {e}")

        total = len([s for s in symbols if s in _prev_close_cache])
        print(f"  Prev close loaded: {total}/{len(symbols)} stocks ✅")
        if total > 0:
            try:
                with open(CACHE_FILE, "w") as f:
                    json.dump(_prev_close_cache, f)
            except Exception:
                pass
        return

    # Fallback: load from cache file
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                cached = json.load(f)
            for s in symbols:
                if s in cached and cached[s] > 0:
                    _prev_close_cache[s] = cached[s]
            print(f"  Prev close from cache: {len(_prev_close_cache)}/{len(symbols)} stocks")
        except Exception as e:
            print(f"  Cache error: {e}")


def load_tokens(client):
    """
    Build token cache from verified tokens + scrip master.
    Also loads prev close from scrip master if fields available.
    """
    global _token_cache, _name_cache, _prev_close_cache
    _token_cache.update(VERIFIED_TOKENS)
    _name_cache.update({v: k for k, v in VERIFIED_TOKENS.items()})

    try:
        csv_url  = client.scrip_master(exchange_segment=config.EXCHANGE_SEGMENT_EQ)
        response = requests.get(csv_url, timeout=30)
        if response.status_code == 200:
            reader   = csv.DictReader(io.StringIO(response.text))
            pc_loaded = 0
            for row in reader:
                sym = row.get("pSymbolName", "").upper().strip()
                trd = row.get("pTrdSymbol",  "").upper().strip()
                tok = row.get("pSymbol",     "").strip()
                if "-EQ" in trd and sym and tok:
                    if sym not in _token_cache:
                        _token_cache[sym] = tok
                        _name_cache[tok]  = sym
                    # Try all possible prev close field names
                    for field in ["dPrevClose","dClosePrice","dPrvCls","dClose","lClose","pClose"]:
                        val = row.get(field, "")
                        if val and val not in ("0", "0.0", ""):
                            try:
                                pc = float(val)
                                if pc > 0 and sym not in _prev_close_cache:
                                    _prev_close_cache[sym] = pc
                                    pc_loaded += 1
                                    break
                            except Exception:
                                pass
            print(f"  Token cache: {len(_token_cache)} symbols loaded ✅")
            if pc_loaded > 0:
                print(f"  Prev close from scrip master: {pc_loaded} stocks")
    except Exception as e:
        print(f"  Scrip master note: {e} — using verified tokens only ({len(_token_cache)})")


def resolve_watchlist(symbols: list) -> dict:
    resolved  = {}
    not_found = []
    for sym in symbols:
        sym   = sym.strip().upper()
        token = _token_cache.get(sym)
        if token:
            resolved[sym] = token
        else:
            not_found.append(sym)
    if not_found:
        print(f"  ⚠️  Not found: {not_found} — check spelling in watchlist.txt")
    print(f"  Resolved {len(resolved)}/{len(symbols)} stocks")
    return resolved


def get_symbol(token: str) -> str:
    return _name_cache.get(token, token)
