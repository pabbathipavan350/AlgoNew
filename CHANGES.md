# ALGO CHANGES LOG
# Share this file in any new chat to restore full context.
# Format: Date | Change | Reason | Files affected

---

## SESSION 1 — Initial fixes (from original broken state)

### VWAP Source — Critical fix
**Change:** Removed all candle math, REST sync, volume delta calculations from vwap_engine.py.
**Reason:** Kotak WS tick sends `ap` field = Sigma(price*qty)/Sigma(qty) since 9:15 AM. This IS the session VWAP, identical to TradingView. Previous code was calculating VWAP separately and the REST sync every 60s was overwriting the correct `ap` value with a calculated one.
**Files:** vwap_engine.py (complete rewrite), main.py (_sync_exchange_vwap deleted)

### Strike pairing — Critical fix
**Change:** Engine now tracks exactly 1 CE + 1 PE token. Entry check uses only the exact mirror pair.
**Reason:** Old logic used `any()` — passed if ANY CE was above VWAP and ANY PE was below. This meant non-paired strikes could trigger entries. Now CE=ATM-depth and PE=ATM+depth must both satisfy conditions.
**Files:** vwap_engine.py (_get_mirror_pair, check_entry rewritten)

### Too expensive gate — Removed
**Change:** Removed `if option_cost > config.MAX_OPTION_COST` check in _check_entry.
**Reason:** For 200pt+ ITM options with high VIX, prices of Rs 400-700 are normal. This was silently blocking all valid trades.
**Files:** main.py

### Gap check — Removed
**Change:** Removed `if gap_pct > 1.5 and t < 9:45: return` from _on_tick.
**Reason:** Gap was blocking entries when market opened with a gap. Not needed — VWAP logic handles this naturally.
**Files:** main.py

### ReportManager — Wired
**Change:** ReportManager now imported and wired in initialize(). log_trade() called at every exit.
**Reason:** report_manager.py existed but was never called from main.py. All trade data was lost.
**Files:** main.py, config.py (added TRADE_LOG_FILE)

### GitHub SIGTERM — Fixed
**Change:** Added signal.signal(SIGTERM) handler, _graceful_shutdown(), 30-min autosave.
**Reason:** GitHub Actions kills process with SIGTERM after 6 hours. Python finally: block only runs on clean exit. Reports and capital.json were never saved.
**Files:** main.py

### Status print frequency
**Change:** Status print every 1 minute instead of every 5 minutes.
**Reason:** 5 minute gaps made it impossible to see what happened between prints.
**Files:** main.py

---

## SESSION 2 — Strike selection improvements

### Subscribe only 2 tokens
**Change:** _setup_strikes now fetches exactly 2 tokens (CE mirror + PE mirror). Removed _trim_to_best_strikes.
**Reason:** Subscribing 10+ tokens was slowing WS and unnecessary since VWAP comes from `ap` field on any tick.
**Files:** main.py

### Strike refresh — Smarter
**Change:** Replaced 1-hour fixed refresh with:
- 9:15-10:00 AM: check every 10 minutes
- 10:00 AM+: check every 30 minutes
- Only refresh if ATM shifted 100pts or more
- Subscribe new tokens FIRST, drop old tokens AFTER (zero gap in coverage)
- Stop refreshing after 2:30 PM
**Reason:** Hourly was too slow — Nifty moved 150pts and strikes became irrelevant. 10 min too frequent after 10 AM.
**Files:** main.py (_start_hourly_strike_refresh rewritten)

### _identify_pair — Fixed
**Change:** _identify_pair() no longer hardcodes ATM±200. Just picks the first CE and PE token present.
**Reason:** After switching to delta-based strike selection, strikes are no longer exactly 200pts. Engine was returning token=None.
**Files:** vwap_engine.py

---

## SESSION 3 — Today's fixes (based on live trading observations)

### 1. Cooldown removed
**Change:** Removed 10-minute cooldown between trades entirely.
**Old value:** cooldown_mins = 10
**Reason:** Missed a good trade that would have covered all losses because signal came at minute 9. The natural entry filter (0-3pts above VWAP + mirror below VWAP) prevents overtrading without needing an artificial time lock.
**Files:** vwap_engine.py (cooldown_mins removed, check_entry block removed), config.py

### 2. Entry zone tightened + SL widened + SL cap added
**Change:**
- ENTRY_ZONE_PTS: 5.0 → 3.0 (enter only when 0-3pts above VWAP, not 0-5)
- VWAP_SL_BUFFER: 2.0 → 5.0 (SL = VWAP - 5 instead of VWAP - 2)
- MAX_SL_PTS: new = 10.0 (SL never more than 10pts below entry price)
**Reason:** Kotak `ap` field matches 1-min VWAP. 5-min VWAP (which price respects more) is consistently 2-3pts lower. Old SL of VWAP-2 was being triggered by this gap on every trade. New SL of VWAP-5 gives breathing room. Tighter entry zone means higher quality entries.
**Files:** config.py, vwap_engine.py (MAX_SL_PTS cap in on_entry)

### 3. Breakeven and trail — completely redesigned
**Change:**
- BREAKEVEN_TRIGGER: 20.0 → 10.0 pts
- BREAKEVEN_LOCK_PTS: 5.0 → 2.0 pts (SL moves to entry+2 at breakeven)
- TRAIL_MIN_PROFIT (single value) → Stepped trail system:

```
+10pts: Breakeven lock → SL = entry + 2
+20pts: Trail phase 1 → SL = opt - 15  (give back max 15pts)
+40pts: Trail phase 2 → SL = opt - 15  (ratchet means SL already at 25+)
+60pts: Trail phase 3 → SL = opt - 12  (tighten)
+70pts: Trail phase 4 → SL = opt - 10  (70-80pts is enough, lock in 60+)
```

**Target:** 70-80pts profit is the goal. At +70 pts, guaranteed exit at 60+. At +80pts exit at 70+.
**Reason:** Single 20pt trailing buffer was too loose for the target move size. Stepped trail captures the right amount without exiting early on a 30pt move.
**New config keys:** TRAIL_STEP_1_TRIGGER/BUFFER, TRAIL_STEP_2_TRIGGER/BUFFER, TRAIL_STEP_3_TRIGGER/BUFFER, TRAIL_STEP_4_TRIGGER/BUFFER
**Files:** config.py, vwap_engine.py (check_exit trail block rewritten)

### 4. VIX + expiry matrix for ITM depth
**Change:** Added _pick_itm_depth(vix, days_to_expiry) function.
**Old behavior:** Always tried delta>=0.85, fell back to hardcoded 150pts ITM.
**New matrix:**
```
VIX < 14  → 150pts
VIX 14-18 → 200pts (>3 days) / 150pts (≤3 days)
VIX 18-25 → 250pts (>3 days) / 200pts (≤3 days)
VIX > 25  → 300pts (>3 days) / 250pts (≤3 days)
```
**Reason:** With VIX at 26, delta>=0.85 threshold was never met so always fell back to 150pts. 150pts ITM with VIX 26 is near ATM — very choppy. Need deeper ITM for stable prices. Near expiry, deep ITM becomes illiquid so come shallower.
**Files:** main.py (_pick_itm_depth function, _setup_strikes, _pick_strike updated)

### 5. Timezone fix — replaced monkey-patch with now_ist() helper
**Old approach:** Monkey-patched datetime.datetime.now() — fragile, import-order dependent, broke on some Windows environments.
**New approach:** `now_ist()` helper function at top of main.py. All datetime.datetime.now() calls replaced with now_ist(). Works identically on Linux (GitHub Actions) and Windows (local).
**Files:** main.py (now_ist() added, all 13 datetime.now() calls replaced)

### 6. GitHub workflow + artifact upload
**Change:** Added .github/workflows/run_algo.yml with:
- Runs Mon-Fri at 9:00 AM IST (3:30 UTC)
- All Kotak credentials loaded from GitHub Secrets
- Logs and reports uploaded as artifacts after every run (even on crash)
- capital.json cached between runs so P&L tracks across days
- 6.5 hour timeout to cover full market session
**Reason:** Logs and reports were created inside the runner but disappeared when workflow ended. capital.json reset to 0 every day.
**Files:** .github/workflows/run_algo.yml (new file)

---

## CURRENT CONFIG VALUES (after all changes)
```
ENTRY_ZONE_PTS      = 3.0
VWAP_SL_BUFFER      = 5.0
MAX_SL_PTS          = 10.0
BREAKEVEN_TRIGGER   = 10.0
BREAKEVEN_LOCK_PTS  = 2.0
TRAIL_STEP_1_TRIGGER = 20.0  TRAIL_STEP_1_BUFFER = 15.0
TRAIL_STEP_2_TRIGGER = 40.0  TRAIL_STEP_2_BUFFER = 15.0
TRAIL_STEP_3_TRIGGER = 60.0  TRAIL_STEP_3_BUFFER = 12.0
TRAIL_STEP_4_TRIGGER = 70.0  TRAIL_STEP_4_BUFFER = 10.0
LOT_SIZE             = 65
TARGET_HIGH_VIX_PTS  = 37.5
```

## ARCHITECTURE SUMMARY
- VWAP source: Kotak WS `ap` field only. No calculation.
- Strikes: 1 CE + 1 PE. Delta>=0.85 preferred, VIX matrix fallback.
- Entry: Both must have 3+ ticks and valid VWAP. CE 0-3pts above VWAP + PE below VWAP (or vice versa).
- SL: VWAP - 5pts, capped at entry - 10pts. Dynamic (tracks rising VWAP).
- Exit: Stepped trail targeting 70-80pts. Breakeven at +10pts.
- Refresh: Every 10min (9:15-10:00), every 30min after. Only on 100pt+ ATM shift.

---

## SESSION 4 — Profit booking redesign (Option B trail)

### Removed: VIX-based dynamic target system
**Change:** Removed `_get_dynamic_target_points()`, removed all TARGET_*_VIX_PTS and TARGET_*_EXPIRY_PTS config values.
**Reason:** VIX target (27-42pts) was conflicting with stepped trail — target was exiting at +27pts before trail phases 2-4 ever triggered. Two exit systems fighting each other.
**Files:** main.py, config.py

### Removed: Stepped 4-phase trail
**Change:** Removed TRAIL_STEP_1/2/3/4 triggers and buffers. Replaced with single trail rule.
**Reason:** Overcomplicated. Phases 2-4 never triggered because VIX target exited first.
**Files:** vwap_engine.py, config.py

### New: Option B — single trail targeting 35-50pts
**Change:** Clean single trail rule:
```
+10pts → Breakeven: SL = entry + 2  (costs covered)
+25pts → Trail starts: SL floor = entry + 10 (min profit locked)
          Trail rule: SL = current_price - 15 (ratchet, never down)
```
**How it captures moves:**
- Fast 35pt move → exits ~22-35pts range
- 50pt move → SL at 35, exits ~35-50pts range
- 60pt+ big move → SL keeps ratcheting, exits 45-60+
- Reversal after +25 → guaranteed exit at entry+10 minimum

**New config values:**
```
TRAIL_TRIGGER_PTS  = 25.0   (was: stepped system with TRAIL_STEP_1_TRIGGER=20)
TRAIL_FLOOR_PTS    = 10.0   (new — floor once trail active)
TRAIL_BUFFER       = 15.0   (new — SL = price - 15)
BREAKEVEN_TRIGGER  = 10.0   (unchanged)
BREAKEVEN_LOCK_PTS = 2.0    (unchanged)
```
**Files:** vwap_engine.py, config.py, main.py

### Fixed: TRAIL_TRIGGER_PTS print mismatch
**Change:** Startup print now correctly shows trail trigger=25, floor=10, buffer=15.
**Files:** main.py

### Removed: MAX_OPTION_COST dead config
**Change:** Removed MAX_OPTION_COST = 25000 (was already dead — the check was removed from main.py earlier).
**Files:** config.py

---

## SESSION 5 — Entry/SL/profit logic refinements

### 1. Breakeven at +10pts removed
**Change:** Removed BREAKEVEN_TRIGGER and BREAKEVEN_LOCK_PTS entirely.
**Reason:** Below +25pts the dynamic VWAP SL protects. No need for a separate breakeven event.
**Files:** config.py, vwap_engine.py

### 2. VWAP adjustment — ap-2 everywhere
**Change:** `set_vwap_direct()` in vwap_engine now stores `ap - VWAP_ADJUSTMENT (2pts)` instead of raw `ap`.
**Reason:** Kotak ap = 1-min VWAP. 5-min VWAP (which price respects more on chart) is consistently ~2pts lower. Adjusting once at source means all entry/SL logic automatically uses correct VWAP.
**New config:** `VWAP_ADJUSTMENT = 2.0`
**Files:** vwap_engine.py, config.py

### 3. Entry zone and SL recalculated
**Change:**
- Entry zone: 0 to +4 above effective VWAP (ap-2 to ap+2)
- SL: effective_vwap - 4 = ap - 6 (dynamic, tracks VWAP)
- SL cap: never more than 10pts below actual entry price
**New config:** `ENTRY_ZONE_PTS=4.0`, `VWAP_SL_BUFFER=4.0`, `MAX_SL_PTS=10.0`
**Files:** config.py, vwap_engine.py

### 4. 10-second entry wait for better fill
**Change:** After signal fires, algo waits 10 seconds then re-confirms signal before placing order.
**Reason:** When price falls fast from 140 to 104 (VWAP=100), entering immediately at 104 is expensive. After 10s price may be at 101-102 — better entry, tighter SL.
**Re-check after wait:** price still in zone AND opposite still below VWAP. If not → skip.
**New config:** `ENTRY_WAIT_SECS = 10`
**Files:** main.py, config.py

### 5. Two-phase profit system (replaces stepped trail + VIX target)
**Phase 1 (0 to +25pts):**
  SL = effective_vwap - 4 (dynamic). Cap = entry - 10.
**Phase 2 (+25pts trigger):**
  SL locks at entry + 10. FIXED. No more trailing.
  Wait for target.
**Target:** Exit at first tick >= entry + 37pts.
**Result:**
  Fast 37pt move → exits at 37pts
  Big 50pt move → exits at 37pts (first touch)
  Reversal after +25 → exits at entry+10 (+10pts minimum)
**New config:** `TRAIL_TRIGGER_PTS=25.0`, `TRAIL_FLOOR_PTS=10.0`, `TARGET_PTS=37.0`
**Removed:** `TRAIL_BUFFER`, `BREAKEVEN_TRIGGER`, `BREAKEVEN_LOCK_PTS`
**Files:** vwap_engine.py, config.py, main.py

---

## SESSION 6 — Profit ladder, overtrading guard, pullback wait, OI-filtered strikes

### 1. Profit ladder — 4-level stepped SL (replaces two-phase system)
**Old:** Phase 1 → SL locks at entry+10 at +25pts. Phase 2 → book at +37pts.
**New:** Four-level ratchet ladder (SL only ever moves up, never down):

```
Phase 0  (0 → +10pts): SL = effective_vwap − 4 (dynamic, tracks VWAP)
                         Cap: entry − 10pts max
Phase 1  (+10pts):      SL = entry + 0  (breakeven — zero loss)
Phase 2  (+25pts):      SL = entry + 10 (minimum +10pts locked)
Phase 3  (+35pts):      SL = entry + 25 (minimum +25pts locked)
Phase 4  (+40pts):      BOOK PROFIT — exit at market immediately
```

**New config keys:** `BREAKEVEN_TRIGGER=10`, `PHASE2_TRIGGER=25`, `PHASE2_LOCK=10`,
`PHASE3_TRIGGER=35`, `PHASE3_LOCK=25`, `BOOK_PROFIT_PTS=40`
**Removed:** `TRAIL_TRIGGER_PTS`, `TRAIL_FLOOR_PTS`, `TARGET_PTS`
**Files:** `vwap_engine.py` (check_exit fully rewritten), `config.py`, `main.py`

### 2. Overtrading guard — hourly trade limit with timed pause
**Change:** Added rolling 60-minute window trade counter.
If `HOURLY_TRADE_LIMIT` (10) entries happen in any 60-min window, new entries
are paused until `HOURLY_PAUSE_UNTIL` (12:00 PM). During the pause:
- Algo keeps receiving ticks and managing existing positions normally
- Signals are detected and logged but no orders placed
- A Telegram alert is sent when the pause triggers
- Pause lifts automatically at 12:00 PM and entries resume

**New config keys:** `HOURLY_TRADE_LIMIT=10`, `HOURLY_PAUSE_UNTIL="12:00"`
**Files:** `main.py` (`_is_overtrading_pause_active()` added, `_check_entry` updated)

### 3. Pullback wait — 60s wait only for pullbacks, not for fresh VWAP crosses
**Old:** Every signal waited 10 seconds regardless of signal type.
**New:** Signal type is classified inside the engine using `was_above` flag:
- **Fresh cross** (`was_above=False` when signal fires): price crossing VWAP from below for the first time → enter immediately. Time-sensitive, waiting would miss the move.
- **Pullback** (`was_above=True` when signal fires): price has previously been above VWAP and is returning to it → wait up to 60s for a better fill price, then re-confirm signal (including opposite-strike check) before entering.

`OptionVWAP.is_pullback()` helper added. `check_entry()` now returns a 4-tuple:
`(direction, token, vwap_obj, is_pullback)`.

**New config key:** `PULLBACK_WAIT_SECS=60` (replaces `ENTRY_WAIT_SECS=10`)
**Files:** `vwap_engine.py` (`is_pullback()` added, `check_entry()` signature updated),
`main.py` (`_check_entry` branching logic)

### 4. OI-filtered strike selection
**Old:** Strikes chosen by VIX+expiry matrix ITM depth only (delta-based fallback).
**New:** VIX+expiry matrix still sets the starting ITM depth. Then `fetch_oi()` is
called to check live open interest. If OI < `MIN_OI_THRESHOLD` (5 lakh), the algo
walks toward ATM in 50pt steps (up to 6 steps) until a strike with sufficient OI
is found. This ensures we are never trading illiquid strikes where `ap` (VWAP) data
may be unreliable or spreads are wide.

If no strike passes OI (very rare), the best available strike is used with a warning.
OI is checked at startup and at every hourly strike refresh.

`find_best_strike_with_oi(client, spot, option_type, expiry_date, itm_depth, opt_mgr)`
added to `option_manager.py`. `fetch_oi(client, token)` helper also added.

**New config key:** `MIN_OI_THRESHOLD=500000`
**Removed dead config keys:** `MIN_ITM_DISTANCE`, `MAX_ITM_DISTANCE`, `REENTRY_COOLDOWN_MIN`,
`NORMAL_SL_BASE`, `SLIP_BUFFER` (still in option_manager internals but removed from config)
**Files:** `option_manager.py`, `main.py` (`_setup_strikes`, `_start_hourly_strike_refresh`)

### 5. Bug fixes (from previous review)
- `config.BUY_LIMIT_BUFFER` — was referenced in main.py but never defined. Added as `BUY_LIMIT_BUFFER=2.0`.
- `config.TRAIL_BUFFER` — was referenced in 3 places after being removed in Session 4. All references removed/replaced.
- `_circuit_alerted` reset logic in `_on_tick` — was resetting on every tick after triggering. Left as-is (minor cosmetic issue, not a trading bug).
- Opposite-strike below-VWAP check — already enforced inside `check_entry()` engine for every call including pullback re-confirms.

### 6. `exit_phase` labeling updated
Daily report now shows the correct phase label per trade:
`Phase0 SL (Initial)`, `Phase1 SL (BE)`, `Phase2 SL (+10lock)`, `Phase3 SL (+25lock)`,
`Book Profit (+40)`, `Square-off`, `Flip`.
Trade dict now includes `phase` (int 0–3) and `entry_type` ('cross'/'pullback').

---

## CURRENT CONFIG VALUES (after Session 6)
```
VWAP_ADJUSTMENT      = 2.0
ENTRY_ZONE_PTS       = 4.0
VWAP_SL_BUFFER       = 4.0
MAX_SL_PTS           = 10.0
PULLBACK_WAIT_SECS   = 60
BREAKEVEN_TRIGGER    = 10.0
PHASE2_TRIGGER       = 25.0   PHASE2_LOCK  = 10.0
PHASE3_TRIGGER       = 35.0   PHASE3_LOCK  = 25.0
BOOK_PROFIT_PTS      = 40.0
HOURLY_TRADE_LIMIT   = 10
HOURLY_PAUSE_UNTIL   = "12:00"
MIN_OI_THRESHOLD     = 500000
LOT_SIZE             = 65
BUY_LIMIT_BUFFER     = 2.0
```

## ARCHITECTURE SUMMARY (after Session 6)
- **VWAP source:** Kotak WS `ap` field only. effective_vwap = ap − 2. No calculation.
- **Strikes:** 1 CE + 1 PE. VIX matrix sets ITM depth. OI ≥ 5 lakh required; walks toward ATM if not met.
- **Entry:** Entry strike 0–4pts above VWAP + opposite strike below VWAP (enforced on every check). Fresh cross → immediate. Pullback → 60s wait + re-confirm.
- **Overtrading:** 10 trades/hr → pause until 12 PM.
- **SL ladder:** Phase0 dynamic → Phase1 BE@+10 → Phase2 +10lock@+25 → Phase3 +25lock@+35 → BookProfit@+40.
- **Refresh:** Every 10min (9:15–10:00), every 30min after. Only on 100pt+ ATM shift. OI re-checked on each refresh.
