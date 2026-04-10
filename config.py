# ============================================================
# CONFIG.PY — Nifty Futures VWAP Options Algo v3
# ============================================================
# Signal  : Nifty current-month futures VWAP cross (tick level)
# Buy CE  : futures crosses above VWAP → buy ITM CE
# Buy PE  : futures crosses below VWAP → buy ITM PE
# SL/TGT  : VIX-based (high VIX: SL=15 TGT=50 | low VIX: SL=10 TGT=35)
# Trail   : always active — SL ratchets up once +20 pts in profit
# Capital : Rs 5,00,000 · 5 lots fixed
# ============================================================

import os

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

# ── Mode ──────────────────────────────────────────────────
PAPER_TRADE         = True    # Set False only when ready for live

# ── Kotak Neo Credentials (from .env) ─────────────────────
KOTAK_CONSUMER_KEY    = os.getenv("KOTAK_CONSUMER_KEY",    "")
KOTAK_CONSUMER_SECRET = os.getenv("KOTAK_CONSUMER_SECRET", "")
KOTAK_MOBILE_NUMBER   = os.getenv("KOTAK_MOBILE_NUMBER",   "")
KOTAK_UCC             = os.getenv("KOTAK_UCC",             "")
KOTAK_MPIN            = os.getenv("KOTAK_MPIN",            "")
KOTAK_ENVIRONMENT     = os.getenv("KOTAK_ENVIRONMENT",     "prod")

# ── Capital & Position Sizing ─────────────────────────────
TOTAL_CAPITAL       = 500000   # Rs 5,00,000 deployed
LOTS                = 5        # Fixed 5 lots always
LOT_SIZE            = 65       # Nifty lot size
INITIAL_CAPITAL     = TOTAL_CAPITAL

# ── VIX threshold ─────────────────────────────────────────
VIX_HIGH_THRESHOLD  = 15.0    # VIX > 15 = high volatility

# ── SL and Target (VIX-based) ─────────────────────────────
SL_PTS_HIGH_VIX     = 15.0    # High VIX SL
TARGET_PTS_HIGH_VIX = 50.0    # High VIX target
SL_PTS_LOW_VIX      = 10.0    # Low VIX SL
TARGET_PTS_LOW_VIX  = 35.0    # Low VIX target

# ── Trailing SL ───────────────────────────────────────────
# Step 1: +20 pts → SL moves to entry (breakeven)
# Step 2: +30 pts → SL moves to entry + 10 (locked)
# SL only ever moves UP — never down.
TRAIL_BREAKEVEN_TRIGGER = 20.0   # +20 pts -> SL = entry price (breakeven)
TRAIL_LOCK_TRIGGER      = 30.0   # +30 pts -> SL = entry + TRAIL_LOCK_PTS
TRAIL_LOCK_PTS          = 10.0   # locked profit at step 2

# ── VWAP source ───────────────────────────────────────────
VWAP_MIN_TICKS          = 3      # min futures ticks before trusting signals

# ── Entry proximity filters ───────────────────────────────
# Fresh cross: enter only if futures is within ±10 pts of VWAP
FRESH_CROSS_MAX_DIST    = 10.0

# Pullback: enter when futures is within 10–15 pts of VWAP
PULLBACK_MAX_DIST       = 15.0   # max distance from VWAP
PULLBACK_MIN_DIST       = 0.0    # min distance (0 = right at VWAP)

# ── Confirmation filter (mid-trade cross check) ───────────
# When already in a trade and futures crosses VWAP again:
#   option LTP < option VWAP → real reversal → flip/exit
#   option LTP > option VWAP → just a pullback → hold current trade
ENABLE_OPTION_VWAP_CONFIRM  = True

# ── Direction fatigue rule ────────────────────────────────
# After DIRECTION_FATIGUE_COUNT profitable exits in same direction:
#   → skip next signal in that direction
#   → re-allow only after DIRECTION_COOLDOWN_MINS minutes
DIRECTION_FATIGUE_COUNT     = 2      # 2 wins in same direction → cool down
DIRECTION_COOLDOWN_MINS     = 15     # wait 15 mins before re-entering same dir
FATIGUE_MIN_PROFIT_PTS      = 10.0   # "profitable" = exit with >= 10 pts gain

# ── Daily guards ──────────────────────────────────────────
MAX_CONSEC_SL           = 3      # stop after 3 consecutive SL hits
MAX_DAILY_LOSS_RS       = -15000 # stop if net day loss > Rs 15,000

# ── Strike pre-loading ────────────────────────────────────
# At startup, we resolve tokens for several ITM depths for both CE and PE.
# When a signal fires, we pick from this cache — zero HTTP delay.
PRELOAD_ITM_DEPTHS      = [100, 150, 200, 250, 300]  # pts ITM to pre-cache

# ── Strike selection (runtime fallback) ───────────────────
MIN_DELTA           = 0.80
MIN_OI              = 1200000
STRIKE_STEP         = 50
MAX_OI_WALK_STEPS   = 8
IV_PCT              = 12.0
RISK_FREE_RATE      = 0.065

# ── Order execution ───────────────────────────────────────
BUY_LIMIT_BUFFER         = 2.0
ORDER_STATUS_POLL_SECS   = 1.0
ORDER_FILL_TIMEOUT_SECS  = 15
EXIT_FILL_TIMEOUT_SECS   = 12
EXIT_RETRY_ATTEMPTS      = 3
ENABLE_AMO_OUTSIDE_HOURS = True

# ── Session timing ────────────────────────────────────────
MARKET_OPEN         = "09:15"
ENTRY_START         = "09:15"
SQUARE_OFF_TIME     = "15:25"
EXPIRY_DAY_CUTOFF   = "14:30"

# ── Costs ─────────────────────────────────────────────────
BROKERAGE_PER_ORDER = 20.0
STT_PCT             = 0.0005
EXCHANGE_TXN_PCT    = 0.00053
SEBI_PCT            = 0.000001
GST_PCT             = 0.18
STAMP_DUTY_PCT      = 0.00003

# ── Segments & tokens ─────────────────────────────────────
FO_SEGMENT          = "nse_fo"
CM_SEGMENT          = "nse_cm"
NIFTY_INDEX_TOKEN   = "26000"

# ── Files ─────────────────────────────────────────────────
CAPITAL_FILE        = "capital.json"
CAPITAL_BACKUP_DAYS = 30
TRADE_LOG_FILE      = "reports/trade_log.csv"
