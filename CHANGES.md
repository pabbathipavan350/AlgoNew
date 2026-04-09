# ALGO CHANGES LOG — v2
# Share this file in any new chat to restore full context.
# Format: Session | Change | Reason | Files affected

---

## v2 — Complete strategy rewrite (futures VWAP signal)

### Why the rewrite
Old algo (v1) used **option VWAP** (`ap` field on CE/PE tokens) as the signal.
Problem: option VWAP is noisy. Wide bid-ask spreads, thin volume, and delta
sensitivity all cause the `ap` field on options to cross above/below VWAP
constantly — generating too many entries, most of them false.

### Core signal change
**Old:** Option price crosses its own VWAP → enter
**New:** Nifty **futures** price crosses futures VWAP → enter

Futures VWAP is clean because:
- Futures trade with index-level liquidity (millions of contracts)
- Spreads are 0.05–0.10 pts — negligible vs options (2–5 pts)
- One futures token represents the full index movement

### Entry logic
- Futures cross **above** VWAP → buy ITM **CE**
- Futures cross **below** VWAP → buy ITM **PE**
- Enter immediately on tick cross — no wait, no re-confirm
- Re-entry allowed on every valid cross (no cooldown)

### Exit logic
- **SL:** option falls 15 pts from entry price → exit
- **Target:** option gains 45 pts from entry price → exit
- **EOD:** hard exit at 3:25 PM regardless

No profit ladder. No breakeven logic. No stepped trail.
Simple fixed SL and target.

### Strike selection
- Delta ≥ 0.85 (deep ITM, high delta = option moves nearly 1:1 with futures)
- OI ≥ 12,00,000 (liquidity floor)
- Walk toward ATM in 50pt steps if OI fails (up to 8 steps)
- Current month expiry futures token resolved at startup from scrip master

### Capital and position sizing
Fixed — Rs 5,00,000 capital, 5 lots always

### New files
- `futures_engine.py` — FuturesVWAPEngine (tracks ap field, detects crosses)
- `main.py` — rewritten orchestrator around futures signal
- `option_manager.py` — rewritten strike picker + orders + cost calc
- `config.py` — rewritten for new strategy parameters

### Kept from v1 (unchanged)
- `auth.py`, `session_manager.py`, `capital_manager.py`,
  `report_manager.py`, `telegram_notifier.py`

---

## Session 1 — Bug fix: WebSocket crash on startup

### `AttributeError: 'NeoAPI' object has no attribute 'connect'`
**Change:** `main.py` line 578: `target=self.client.connect` →
`target=self.client.ws_connect`
**Reason:** Kotak Neo API method to start the WebSocket is `ws_connect()`,
not `connect()`. This was crashing immediately after initialisation before
any ticks could be received.
**Files:** `main.py`

---

## Session 2 — Pullback entries added

### Two entry types instead of one
**Old:** Only fresh VWAP crosses triggered entries.
**New:** Two entry types:

1. **Fresh cross** — futures crosses VWAP (below→above for CE, above→below
   for PE). Fires exactly once per cross. Enter immediately.

2. **Pullback** — futures was above VWAP (CE) or below VWAP (PE), returns
   toward VWAP, and is now within the pullback zone:
   - CE pullback zone: `0 < ltp - vwap <= CE_PULLBACK_PTS (5.0)`
   - PE pullback zone: `0 < vwap - ltp <= PE_PULLBACK_PTS (5.0)`
   - One-shot per zone visit — resets when price leaves the zone.
   - Prevents firing multiple times during a single prolonged touch.

**New config keys:**
```
CE_PULLBACK_PTS = 5.0   # Enter CE when futures 0–5pts above VWAP
PE_PULLBACK_PTS = 5.0   # Enter PE when futures 0–5pts below VWAP
```

**Engine changes (`futures_engine.py`):**
- Added `_ce_pullback_fired` and `_pe_pullback_fired` one-shot flags
- `check_signal()` now returns `(signal, signal_type)` tuple instead of
  just a string. `signal_type` is `'cross'` or `'pullback'`.
- Pullback zone reset logic: flag resets when price leaves the zone so the
  next return visit can fire again.

**Main changes (`main.py`):**
- `check_signal()` call updated to unpack tuple: `sig, sig_type = ...`
- `_on_signal()` signature updated to accept `sig_type`
- `self.entry_type` added to trade state — records `'cross'` or `'pullback'`
  per trade for reporting
- Entry confirmed print shows signal type

**Files:** `futures_engine.py` (rewritten), `config.py`, `main.py`

---

## Session 3 — Directional re-entry filtering

### Same-direction re-entry blocked; opposite-direction allowed
**Old:** All new signals were dropped while any trade was open (`if not self.in_trade`).
**New:** Directional filtering:

- **Same direction blocked:** CE signal arrives while CE is open → ignored.
  PE signal arrives while PE is open → ignored.
  Reason: prevents averaging into a losing trade or doubling up on a pullback
  that is actually a reversal.

- **Opposite direction allowed:** CE is open and PE signal fires → CE is
  closed at current option LTP first, then PE is entered.
  Reason: a genuine VWAP reversal (cross or pullback in opposite direction)
  is a valid signal even while holding a position.

**Logic in `_on_futures_tick`:**
```python
sig, sig_type = self.futures_engine.check_signal()
if sig:
    same_direction = self.in_trade and self.direction == sig
    if same_direction:
        # drop — already holding this direction
    else:
        self._on_signal(sig, sig_type, ltp, vwap, t)
```

**Logic in `_on_signal`:**
```python
if self.in_trade and self.direction != direction:
    # Close existing opposite position before entering new one
    self._exit_trade(self.option_ltp or self.entry_price, "Flip")
```

**Files:** `main.py`

---

## CURRENT CONFIG (after Session 3)
```
TOTAL_CAPITAL       = 500000
LOTS                = 5
LOT_SIZE            = 65
SL_PTS              = 15.0
TARGET_PTS          = 45.0
VWAP_MIN_TICKS      = 3
CE_PULLBACK_PTS     = 5.0
PE_PULLBACK_PTS     = 5.0
MIN_DELTA           = 0.85
MIN_OI              = 1200000
MAX_OI_WALK_STEPS   = 8
BUY_LIMIT_BUFFER    = 2.0
MAX_DAILY_LOSS_RS   = -15000
ENTRY_START         = "09:15"
SQUARE_OFF_TIME     = "15:25"
EXPIRY_DAY_CUTOFF   = "14:30"
```

## ARCHITECTURE SUMMARY (after Session 3)
- **Signal source:** Kotak WS `ap` field on current-month Nifty futures token
- **Fresh cross CE:** futures ltp crosses above vwap (was_above: False→True)
- **Fresh cross PE:** futures ltp crosses below vwap (was_above: True→False)
- **Pullback CE:** futures 0–5pts above vwap (was above, pulled back) — one-shot
- **Pullback PE:** futures 0–5pts below vwap (was below, pulled up) — one-shot
- **Same direction:** blocked while that direction is open
- **Opposite direction:** closes current position first, then enters new
- **Strike:** delta≥0.85 + OI≥12L, walk toward ATM if OI fails (up to 8 steps)
- **Orders:** limit buy (LTP+2), market exit
- **Exit:** fixed SL −15 pts or target +45 pts on option price
- **EOD:** square off at 3:25 PM, report generated

---

## Session 4 — WebSocket connect() fix

### `AttributeError: 'NeoAPI' object has no attribute 'ws_connect'`
**Root cause:** The Kotak Neo SDK `NeoAPI` class does not have `ws_connect()`
*or* attribute-style callback assignment (`client.on_message = ...`).
The correct API is `client.connect(on_message=..., on_error=..., on_close=..., on_open=...)`.

**Changes in `main.py`:**
- `_setup_websocket()` now stores callbacks in `self._ws_callbacks` dict
  instead of assigning them as attributes on `self.client`
- New `_start_ws()` method spawns a daemon thread that calls
  `self.client.connect(**self._ws_callbacks)` — the correct SDK call
- `run()` now calls `self._start_ws()` instead of
  `threading.Thread(target=self.client.ws_connect, ...)`
- `_on_reconnect()` calls `self._start_ws()` instead of nothing

**Files:** `main.py`

---

## Session 5 — WebSocket final fix (Kotak Neo v2 correct pattern)

### Problem
Previous session used `client.connect(...)` — this method does not exist.
Before that, `ws_connect()` was tried — also does not exist.

### Root cause identified from working algo (Final_Fixed_Four)
Kotak Neo **v2** SDK pattern (confirmed from working production code):

1. Assign callbacks as **attributes** on the client:
   ```python
   client.on_message = self._on_message
   client.on_error   = self._on_ws_error
   client.on_close   = self._on_ws_close
   client.on_open    = self._on_ws_open
   ```
2. Call `client.subscribe(instrument_tokens=[...])` — this **starts the WS internally**.
3. **Never call** `connect()` or `ws_connect()` — they do not exist in v2.

### Changes in `main.py`
- `_setup_websocket()` restored to attribute-assignment pattern (correct)
- `_start_ws()` helper method removed entirely
- `run()` now calls `self._subscribe_futures()` after init — this triggers WS start
- `_on_reconnect()` calls `_subscribe_futures()` to restart WS after re-login

**Files:** `main.py`
