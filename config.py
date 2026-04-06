# ============================================================
# CONFIG.PY — Nifty Options VWAP Algo Settings
# ============================================================
# Credentials are loaded from .env using stdlib only —
# no python-dotenv required.
# ============================================================

import os

def _load_env(path=".env"):
    """Read key=value pairs from .env using stdlib only (no dotenv needed)."""
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
            if key and key not in os.environ:   # don't overwrite real env vars
                os.environ[key] = val

_load_env()

# ── Mode ──────────────────────────────────────────────────
PAPER_TRADE         = True    # Set False only when ready for live

# ── Kotak Neo Credentials (from .env) ─────────────────────
KOTAK_CONSUMER_KEY    = os.getenv("KOTAK_CONSUMER_KEY",    "")
KOTAK_CONSUMER_SECRET = os.getenv("KOTAK_CONSUMER_SECRET", "")
KOTAK_MOBILE_NUMBER   = os.getenv("KOTAK_MOBILE_NUMBER",   "")
KOTAK_UCC             = os.getenv("KOTAK_UCC",             "")
KOTAK_MPIN            = os.getenv("KOTAK_MPIN",            "")
KOTAK_ENVIRONMENT     = os.getenv("KOTAK_ENVIRONMENT",     "prod")

# ── Capital ───────────────────────────────────────────────
INITIAL_CAPITAL     = 30000
LOT_SIZE            = 65       # Nifty lot size

# ── Order execution ───────────────────────────────────────
ENABLE_AMO_OUTSIDE_HOURS = True # Use AMO automatically if running outside market hours
BUY_LIMIT_BUFFER    = 2.0    # Add this many pts above LTP for limit buy orders

# ── Risk controls ─────────────────────────────────────────
MAX_DAILY_LOSS_RS    = -3000   # Stop all trading if net P&L drops below this (Rs)
MAX_TRADES_PER_DAY   = 5       # Hard stop: no new entries after this many trades
EXPIRY_DAY_CUTOFF    = "14:30" # No new entries after this time on expiry day
NO_TICK_CIRCUIT_SECS = 300     # If no tick for 5 mins during mkt hours → assume circuit/halt

# ── Overtrading guard ─────────────────────────────────────
# If HOURLY_TRADE_LIMIT trades happen within any rolling 60-min window,
# pause new entries until HOURLY_PAUSE_UNTIL. Algo keeps watching signals
# during the pause (logs them) but does not enter.
HOURLY_TRADE_LIMIT   = 10      # Max trades in any rolling 60-min window
HOURLY_PAUSE_UNTIL   = "12:00" # Resume new entries after this time once pause triggered

# ── Order monitoring ──────────────────────────────────────
ORDER_STATUS_POLL_SECS      = 1.0   # Poll interval after placing order
ORDER_FILL_TIMEOUT_SECS     = 15    # Wait this long for entry fill before cancelling remainder
EXIT_FILL_TIMEOUT_SECS      = 12    # Exit gets more time — partial exit is worse than waiting
EXIT_RETRY_ATTEMPTS         = 3     # Re-place exit order if any quantity is still open

# ── Strategy ──────────────────────────────────────────────
# ── VWAP adjustment ───────────────────────────────────────
# Kotak ap = 1-min VWAP. 5-min VWAP (which price respects) is ~2pts lower.
# Effective VWAP = ap - VWAP_ADJUSTMENT (used everywhere — entry, SL, dynamic)
VWAP_ADJUSTMENT     = 2.0    # effective_vwap = ap - 2

# ── Entry zone ─────────────────────────────────────────────
# Price must be 0 to +4 above effective VWAP (ap-2 to ap+2)
ENTRY_ZONE_PTS      = 4.0    # price in [effective_vwap, effective_vwap + 4]

# ── Stop loss ──────────────────────────────────────────────
# SL = effective_vwap - 4 = ap - 6 (dynamic, tracks rising VWAP)
VWAP_SL_BUFFER      = 4.0    # SL = effective_vwap - VWAP_SL_BUFFER (= ap-6)
MAX_SL_PTS          = 10.0   # SL never more than 10pts below actual entry price

# ── Entry wait — pullback mode only ───────────────────────
# When a pullback signal fires (price returning to VWAP from above, not a fresh
# VWAP cross), the algo waits up to PULLBACK_WAIT_SECS for a better fill price,
# then re-confirms the signal before placing the order.
# A fresh VWAP cross (was_above=False at signal time) enters immediately.
PULLBACK_WAIT_SECS  = 60     # Wait up to 60s for pullback entries (better fill)

# ── Profit management — 4-level stepped SL ladder ─────────
#
# Phase 0  (0 to +10pts):   SL = effective_vwap - 4  (dynamic, tracks VWAP).
#                             Cap: never more than MAX_SL_PTS below entry.
#
# Phase 1  (+10pts trigger): SL moves to entry + 0  (breakeven — zero loss).
#
# Phase 2  (+25pts trigger): SL moves to entry + 10 (minimum +10 locked).
#
# Phase 3  (+35pts trigger): SL moves to entry + 25 (minimum +25 locked).
#
# Phase 4  (+40pts trigger): BOOK PROFIT — exit at market immediately.
#
# All SL values are ratcheted: once raised, never lowered.

BREAKEVEN_TRIGGER   = 10.0   # +10pts → SL = entry (zero loss)
PHASE2_TRIGGER      = 25.0   # +25pts → SL = entry + PHASE2_LOCK
PHASE2_LOCK         = 10.0   # SL locked at entry + 10
PHASE3_TRIGGER      = 35.0   # +35pts → SL = entry + PHASE3_LOCK
PHASE3_LOCK         = 25.0   # SL locked at entry + 25
BOOK_PROFIT_PTS     = 40.0   # +40pts → book profit immediately (market exit)

# ── Session ───────────────────────────────────────────────
MARKET_OPEN         = "09:15"
ENTRY_START         = "09:15"
EARLY_SESSION_END   = "09:40"
SQUARE_OFF_TIME     = "15:25"

# ── Costs ─────────────────────────────────────────────────
TOTAL_COST          = 340      # approximate Rs per round-trip (display only)

# ── Strike selection — VIX matrix + OI filter ─────────────
# Depth (ITM distance) set by VIX + days-to-expiry matrix (see main.py).
# The chosen strike must also pass the OI threshold for liquidity.
# If OI is too low, walk toward ATM in STRIKE_STEP increments until OI passes.
MIN_DELTA           = 0.80
STRIKE_STEP         = 50
IV_PCT              = 12.0
RISK_FREE_RATE      = 0.065
MIN_OI_THRESHOLD    = 1200000   # Minimum open interest (5 lakh contracts)

# ── WebSocket ─────────────────────────────────────────────
NIFTY_INDEX_TOKEN   = "26000"
FO_SEGMENT          = "nse_fo"
CM_SEGMENT          = "nse_cm"

# ── Files ─────────────────────────────────────────────────
CAPITAL_FILE        = "capital.json"
CAPITAL_BACKUP_DAYS = 30
SLIP_BUFFER         = 2.0
TRADE_LOG_FILE      = "reports/trade_log.csv"
