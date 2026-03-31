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
MAX_OPTION_COST     = 25000    # Max Rs to spend on 1 lot (price x LOT_SIZE)

# ── Order execution ───────────────────────────────────────
# MKT orders used — no buffer needed (MKT confirmed in Kotak Neo API docs)
ENABLE_AMO_OUTSIDE_HOURS = True # Use AMO automatically if running outside market hours

# ── Risk controls ─────────────────────────────────────────
MAX_DAILY_LOSS_RS    = -3000   # Stop all trading if net P&L drops below this (Rs)
MAX_TRADES_PER_DAY   = 5       # Stop new entries after this many trades
EXPIRY_DAY_CUTOFF    = "14:30" # No new entries after this time on expiry day
NO_TICK_CIRCUIT_SECS = 300     # If no tick for 5 mins during mkt hours → assume circuit/halt

# ── Order monitoring ──────────────────────────────────────
ORDER_STATUS_POLL_SECS      = 1.0   # Poll interval after placing order
ORDER_FILL_TIMEOUT_SECS     = 15    # Wait this long for entry fill before cancelling remainder
                                    # MKT orders should fill in <2s normally — 15s gives buffer
                                    # for slow exchange on high-volatility days (VIX > 20)
EXIT_FILL_TIMEOUT_SECS      = 12    # Exit gets more time — partial exit is worse than waiting
                                    # MKT exit must fill — if it doesn't in 12s something is wrong
EXIT_RETRY_ATTEMPTS         = 3     # Re-place exit order if any quantity is still open

# ── Dynamic target logic ──────────────────────────────────
TARGET_LOW_VIX_PTS     = 27.5   # Low VIX target band: 25-30
TARGET_MEDIUM_VIX_PTS  = 32.5   # Medium VIX target band: 30-35
TARGET_HIGH_VIX_PTS    = 37.5   # High VIX target band: 35-40
TARGET_NEAR_EXPIRY_PTS = 32.5   # Near expiry target band: 30-35
TARGET_EXPIRY_DAY_PTS  = 42.5   # Expiry day target band: 40-45
VIX_MEDIUM_THRESHOLD   = 14.0
VIX_HIGH_THRESHOLD     = 18.0

# ── Strategy ──────────────────────────────────────────────
ENTRY_ZONE_PTS      = 3.0      # Max pts above VWAP for entry
EARLY_SESSION_SL    = 5.0      # SL pts for 9:15-9:40
NORMAL_SL_BASE      = 10.0     # Base SL pts for 9:40+
BREAKEVEN_TRIGGER   = 20.0     # Move SL to entry+BREAKEVEN_LOCK_PTS when +20 pts profit
BREAKEVEN_LOCK_PTS  = 5.0      # SL moves to entry+5 pts (covers round-trip costs of ~5.2pts)
TRAIL_TRIGGER_PTS   = 30.0     # Activate trailing SL at +30 pts
TRAIL_MIN_PROFIT    = 20.0     # Keep at least 20 pts profit once trail active

# ── Session ───────────────────────────────────────────────
MARKET_OPEN         = "09:15"
ENTRY_START         = "09:15"
EARLY_SESSION_END   = "09:40"
SQUARE_OFF_TIME     = "15:25"

# ── Costs ─────────────────────────────────────────────────
TOTAL_COST          = 340      # approximate Rs per round-trip (display only)

# ── Strike selection ──────────────────────────────────────
MIN_DELTA           = 0.80
STRIKE_STEP         = 50
IV_PCT              = 12.0
RISK_FREE_RATE      = 0.065
MIN_ITM_DISTANCE    = 200      # Always 200 pts ITM from ATM
MAX_ITM_DISTANCE    = 200      # Always 200 pts ITM from ATM (exact strike)

# ── Cooldown ──────────────────────────────────────────────
REENTRY_COOLDOWN_MIN = 10

# ── WebSocket ─────────────────────────────────────────────
NIFTY_INDEX_TOKEN   = "26000"
FO_SEGMENT          = "nse_fo"
CM_SEGMENT          = "nse_cm"

# ── Files ─────────────────────────────────────────────────
CAPITAL_FILE        = "capital.json"
CAPITAL_BACKUP_DAYS = 30
SLIP_BUFFER         = 2.0
