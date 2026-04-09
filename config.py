# ============================================================
# CONFIG.PY — Nifty Futures VWAP Options Algo
# ============================================================
# Signal  : Nifty current-month futures VWAP cross (tick level)
# Buy CE  : futures crosses above VWAP → buy ITM CE
# Buy PE  : futures crosses below VWAP → buy ITM PE
# SL      : option entry price − 15 pts
# Target  : option entry price + 45 pts
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
INITIAL_CAPITAL     = TOTAL_CAPITAL   # for CapitalManager compatibility

# ── Strategy — core numbers ───────────────────────────────
SL_PTS              = 15.0    # Option falls 15 pts from entry → exit
TARGET_PTS          = 45.0    # Option gains 45 pts from entry → exit

# ── VWAP source ───────────────────────────────────────────
# Kotak WS ap field = session VWAP (Σ price×qty / Σ qty since 9:15)
# We use this directly — no manual calculation.
# Signal is on FUTURES ap field, NOT option ap.
VWAP_MIN_TICKS      = 3       # Futures token must receive ≥ 3 ticks before
                               # a cross is considered valid (avoids ghost ticks)

# ── Entry conditions ──────────────────────────────────────
# Two types of entries:
#
# 1. Fresh cross  — futures crosses VWAP from below→above (CE) or above→below (PE)
#                   Enter immediately on the cross tick.
#
# 2. Pullback     — futures was above VWAP (CE) or below VWAP (PE),
#                   pulls back toward VWAP, and is now within the pullback zone.
#                   CE pullback zone : 0 < futures_ltp - vwap <= CE_PULLBACK_PTS
#                   PE pullback zone : 0 < vwap - futures_ltp <= PE_PULLBACK_PTS
#                   Entry fires once per pullback visit to the zone (one-shot flag).
#
CE_PULLBACK_PTS     = 5.0     # CE entry when futures is within 5 pts ABOVE VWAP
PE_PULLBACK_PTS     = 5.0     # PE entry when futures is within 5 pts BELOW VWAP

# ── Strike selection ──────────────────────────────────────
MIN_DELTA           = 0.85    # Minimum delta for ITM strike
MIN_OI              = 1200000 # Minimum open interest (12 lakh contracts)
STRIKE_STEP         = 50      # Walk toward ATM in 50 pt steps if OI fails
MAX_OI_WALK_STEPS   = 8       # Maximum steps toward ATM before giving up
IV_PCT              = 12.0    # Implied volatility estimate for delta calc
RISK_FREE_RATE      = 0.065   # Risk-free rate for delta calc

# ── Order execution ───────────────────────────────────────
BUY_LIMIT_BUFFER    = 2.0     # Add 2 pts above LTP for limit buy orders
ORDER_STATUS_POLL_SECS   = 1.0
ORDER_FILL_TIMEOUT_SECS  = 15
EXIT_FILL_TIMEOUT_SECS   = 12
EXIT_RETRY_ATTEMPTS      = 3
ENABLE_AMO_OUTSIDE_HOURS = True

# ── Session timing ────────────────────────────────────────
MARKET_OPEN         = "09:15"
ENTRY_START         = "09:15"
SQUARE_OFF_TIME     = "15:25"
EXPIRY_DAY_CUTOFF   = "14:30"  # No new entries after this on expiry day

# ── Risk guard ────────────────────────────────────────────
MAX_DAILY_LOSS_RS   = -15000  # Stop all entries if day loss exceeds this
                               # (15 pts × 75 × 5 lots = Rs 5,625 per trade
                               #  ~2.5 losing trades before halt)

# ── Costs (for P&L calculation) ───────────────────────────
# Per side (buy or sell), per lot:
#   Brokerage       : Rs 20 flat per order (not per lot)
#   STT             : 0.02% of premium on BUY side (options)
#                     0.1%  of intrinsic on SELL side (ITM options exercise)
#                     We conservatively use 0.05% of turnover both sides
#   Exchange txn    : 0.053% of premium (NSE F&O)
#   SEBI charges    : Rs 10 per crore of turnover = 0.0001%
#   GST             : 18% on (brokerage + exchange + SEBI)
#   Stamp duty      : 0.003% on buy side
# ── Simplified flat estimate used in reporting ─────────────
BROKERAGE_PER_ORDER = 20.0    # Rs 20 flat per order (both entry and exit)
STT_PCT             = 0.0005  # 0.05% of premium turnover (conservative)
EXCHANGE_TXN_PCT    = 0.00053 # 0.053% of premium turnover
SEBI_PCT            = 0.000001 # Rs 10/crore = 0.000001
GST_PCT             = 0.18    # 18% on brokerage+exchange+SEBI
STAMP_DUTY_PCT      = 0.00003 # 0.003% on buy turnover

# ── Segments & tokens ─────────────────────────────────────
FO_SEGMENT          = "nse_fo"
CM_SEGMENT          = "nse_cm"
NIFTY_INDEX_TOKEN   = "26000"  # Nifty 50 index (cash) — for ATM reference
# Futures token is resolved dynamically at startup (current month expiry)

# ── Files ─────────────────────────────────────────────────
CAPITAL_FILE        = "capital.json"
CAPITAL_BACKUP_DAYS = 30
TRADE_LOG_FILE      = "reports/trade_log.csv"
