# ============================================================
# VWAP_ENGINE.PY — Exchange VWAP Engine
# ============================================================
# VWAP SOURCE:
#   Kotak WS tick sends 'ap' (average price) on every tick.
#   ap = Sigma(price * qty) / Sigma(qty) since 9:15 AM.
#   This IS the session VWAP — identical to TradingView VWAP.
#   No candle math, no REST polling, no volume delta needed.
#   set_vwap_direct() is called on every tick with ap.
#
# STRIKE PAIRING (always exact mirror, VIX-matrix ITM depth):
#   ATM = round(nifty / 50) * 50
#   CE strike = ATM - depth  (ITM for calls)
#   PE strike = ATM + depth  (ITM for puts)
#   Entry only valid when entry strike is 0-4pts above its VWAP
#   AND the opposite mirror strike is below its VWAP.
#
# PROFIT LADDER (4-level stepped SL — ratchet only, never down):
#   Phase 0  (0 to +10pts):  SL = effective_vwap - 4  (dynamic)
#   Phase 1  (+10pts):       SL = entry + 0  (breakeven)
#   Phase 2  (+25pts):       SL = entry + 10
#   Phase 3  (+35pts):       SL = entry + 25
#   Phase 4  (+40pts):       BOOK PROFIT — exit immediately
# ============================================================

import datetime
import config


class OptionVWAP:
    """
    Tracks VWAP for a single option strike.
    VWAP is received directly from Kotak WS 'ap' field on every tick.
    No calculation required — ap = session VWAP since 9:15 AM.
    """

    def __init__(self, name, strike, option_type):
        self.name        = name
        self.strike      = strike
        self.option_type = option_type

        # VWAP — set directly from exchange tick 'ap' field
        self.vwap        = 0.0

        # LTP and tick tracking
        self.ltp         = 0.0
        self.tick_count  = 0

        # Direction tracking
        self.was_above   = False   # True once ltp > vwap seen at least once
        self.was_below   = False

        # Peak price seen (for journal analysis)
        self.peak_ltp    = 0.0

    def add_tick(self, ltp, ts=None):
        """Update LTP. VWAP set separately via set_vwap_direct()."""
        if ltp <= 0:
            return
        self.ltp = ltp
        self.tick_count += 1
        if ltp > self.peak_ltp:
            self.peak_ltp = ltp
        v = self.vwap
        if v > 0:
            if ltp > v:
                self.was_above = True
            elif ltp < v:
                self.was_below = True

    def set_vwap_direct(self, vwap):
        """Set effective VWAP from Kotak WS 'ap' field.
        effective_vwap = ap - VWAP_ADJUSTMENT (2pts).
        5-min VWAP (which price respects on chart) is ~2pts below 1-min VWAP.
        All entry/SL logic uses this adjusted value automatically."""
        if vwap > 0:
            self.vwap = round(vwap - config.VWAP_ADJUSTMENT, 2)

    def seed(self, ltp):
        """Seed LTP from REST before WS starts."""
        if ltp > 0 and self.tick_count == 0:
            self.ltp = ltp

    def reset_peak(self):
        self.peak_ltp = self.ltp

    def get_vwap(self):
        return self.vwap

    def dist_above_vwap(self):
        if self.vwap <= 0:
            return 0.0
        return round(self.ltp - self.vwap, 2)

    def is_above_vwap(self):
        return self.vwap > 0 and self.ltp > self.vwap

    def is_below_vwap(self):
        return self.vwap > 0 and self.ltp < self.vwap

    def is_near_vwap(self, tolerance=5.0):
        """Entry zone: price 0 to tolerance pts ABOVE VWAP. Needs 3+ ticks."""
        if self.vwap <= 0 or self.ltp <= 0 or self.tick_count < 3:
            return False
        dist = self.ltp - self.vwap
        return 0 <= dist <= tolerance

    def is_pullback(self):
        """
        True if this tick is a pullback entry: price has previously been above
        VWAP (was_above=True) and is now back inside the entry zone.
        False if this is a fresh cross (price was below, now just crossed up).
        """
        return self.was_above

    def status(self):
        side = "▲" if self.is_above_vwap() else "▼"
        return f"{self.name}={self.ltp:.1f}(V={self.vwap:.1f}){side}"


# ==============================================================
# StrategyEngine
# ==============================================================

class StrategyEngine:
    """
    Strategy engine tracking exactly 1 CE + 1 PE mirror pair.

    Entry rules (checked on every tick):
      CE entry: CE is 0-4pts above its VWAP AND PE is below its VWAP
      PE entry: PE is 0-4pts above its VWAP AND CE is below its VWAP
      Early session (<9:40): also requires was_above=True (confirmed crossover)

    Profit ladder (4-level stepped SL, ratchet only):
      Phase 0  (0→+10):   dynamic SL = effective_vwap - 4
      Phase 1  (+10):      SL = entry + 0  (breakeven)
      Phase 2  (+25):      SL = entry + 10
      Phase 3  (+35):      SL = entry + 25
      Phase 4  (+40):      book profit — exit immediately
    """

    def __init__(self):
        self.ce_strikes  = {}   # token -> OptionVWAP
        self.pe_strikes  = {}   # token -> OptionVWAP
        self.atm_strike  = 0

        # Active mirror pair tokens
        self.ce_token    = None
        self.pe_token    = None

        self.reset_trade()
        self.last_signal_time = None   # kept for flip bypass only — no cooldown

    def setup_strikes(self, spot, ce_tokens, pe_tokens, step=50):
        self.atm_strike = round(spot / 50) * 50
        self.ce_strikes = {}
        self.pe_strikes = {}
        self.ce_token   = None
        self.pe_token   = None

        for strike, token in ce_tokens.items():
            self.ce_strikes[token] = OptionVWAP(f"CE_{strike}", strike, 'CE')

        for strike, token in pe_tokens.items():
            self.pe_strikes[token] = OptionVWAP(f"PE_{strike}", strike, 'PE')

        self._identify_pair()
        ce_obj = self.ce_strikes.get(self.ce_token)
        pe_obj = self.pe_strikes.get(self.pe_token)
        ce_strike_display = ce_obj.strike if ce_obj else '?'
        pe_strike_display = pe_obj.strike if pe_obj else '?'
        print(f"  [Engine] ATM={self.atm_strike} | "
              f"CE pair: {ce_strike_display} (token={self.ce_token}) | "
              f"PE pair: {pe_strike_display} (token={self.pe_token})")

    def _identify_pair(self):
        """
        Identify the active CE and PE tokens.
        We always subscribe exactly 1 CE and 1 PE token, so just
        pick whichever is present — no hardcoded distance needed.
        """
        self.ce_token = next(iter(self.ce_strikes), None)
        self.pe_token = next(iter(self.pe_strikes), None)

    def update_atm(self, spot):
        self.atm_strike = round(spot / 50) * 50
        self._identify_pair()

    def add_tick(self, token, ltp, ts=None):
        if token in self.ce_strikes:
            self.ce_strikes[token].add_tick(ltp, ts)
        elif token in self.pe_strikes:
            self.pe_strikes[token].add_tick(ltp, ts)

    def set_vwap_direct(self, token, vwap):
        """Called on every WS tick with 'ap' = session VWAP."""
        if token in self.ce_strikes:
            self.ce_strikes[token].set_vwap_direct(vwap)
        elif token in self.pe_strikes:
            self.pe_strikes[token].set_vwap_direct(vwap)

    def seed_ltp(self, token, ltp):
        if token in self.ce_strikes:
            self.ce_strikes[token].seed(ltp)
        elif token in self.pe_strikes:
            self.pe_strikes[token].seed(ltp)

    def get_ltp(self, token):
        if token in self.ce_strikes:
            return self.ce_strikes[token].ltp
        elif token in self.pe_strikes:
            return self.pe_strikes[token].ltp
        return 0.0

    def reset_trade(self):
        self.in_trade        = False
        self.direction       = None
        self.active_token    = None
        self.active_strike   = None
        self.entry_price     = 0.0
        self.entry_vwap      = 0.0
        self.entry_dist      = 0.0
        self.sl_price        = 0.0
        self.best_price      = 0.0
        # Phase flags — only ever advance forward, never reset mid-trade
        self.phase           = 0    # 0=dynamic, 1=breakeven, 2=+10lock, 3=+25lock
        self.trail_active    = False   # True once any SL lock phase is active (phase>=1)
        self.breakeven_done  = False   # True once phase >= 1

    def _get_mirror_pair(self):
        """
        Return (ce_vwap_obj, pe_vwap_obj) for the active mirror pair.
        Returns (None, None) if pair not ready (no tokens, no ticks, no VWAP).
        """
        if not self.ce_token or not self.pe_token:
            return None, None
        ce_obj = self.ce_strikes.get(self.ce_token)
        pe_obj = self.pe_strikes.get(self.pe_token)
        if not ce_obj or not pe_obj:
            return None, None
        if ce_obj.tick_count < 3 or pe_obj.tick_count < 3:
            return None, None
        if ce_obj.vwap <= 0 or pe_obj.vwap <= 0:
            return None, None
        return ce_obj, pe_obj

    def check_entry(self, now):
        """
        Check for valid entry using exact mirror pair only.

        CE entry: CE in 0-4pts above its VWAP AND PE is below its VWAP.
        PE entry: PE in 0-4pts above its VWAP AND CE is below its VWAP.

        The opposite-strike below-VWAP check runs on every entry attempt,
        including re-confirms after pullback waits.

        Early session (<9:40): also requires was_above=True (confirmed crossover).

        Returns:
            (direction, token, vwap_obj, is_pullback)
            where is_pullback=True means the entry strike had was_above=True
            (price returning to VWAP), False means a fresh cross.
        """
        if self.in_trade:
            return None, None, None, False

        t = now.time()
        if t < datetime.time(9, 15) or t >= datetime.time(15, 25):
            return None, None, None, False

        ce_obj, pe_obj = self._get_mirror_pair()
        if not ce_obj or not pe_obj:
            return None, None, None, False

        early_session = t < datetime.time(9, 40)

        # CE entry: CE 0-4pts above its VWAP, PE must be BELOW its VWAP
        if ce_obj.is_near_vwap(config.ENTRY_ZONE_PTS) and pe_obj.is_below_vwap():
            if not early_session or ce_obj.was_above:
                is_pb = ce_obj.is_pullback()
                return 'CE', self.ce_token, ce_obj, is_pb

        # PE entry: PE 0-4pts above its VWAP, CE must be BELOW its VWAP
        if pe_obj.is_near_vwap(config.ENTRY_ZONE_PTS) and ce_obj.is_below_vwap():
            if not early_session or pe_obj.was_above:
                is_pb = pe_obj.is_pullback()
                return 'PE', self.pe_token, pe_obj, is_pb

        return None, None, None, False

    def on_entry(self, direction, token, entry_price, vwap_obj, now):
        live_vwap  = vwap_obj.get_vwap()
        dist_above = max(entry_price - live_vwap, 0.0)

        raw_sl        = round(live_vwap - config.VWAP_SL_BUFFER, 2)
        min_sl        = round(entry_price - config.MAX_SL_PTS, 2)
        self.sl_price = max(raw_sl, min_sl)   # never more than MAX_SL_PTS below entry

        self.in_trade        = True
        self.direction       = direction
        self.active_token    = token
        self.active_strike   = vwap_obj.strike
        self.entry_price     = entry_price
        self.entry_vwap      = live_vwap
        self.entry_dist      = dist_above
        self.best_price      = entry_price
        self.phase           = 0
        self.trail_active    = False
        self.breakeven_done  = False
        self.last_signal_time = now
        vwap_obj.reset_peak()

        ts      = now.strftime('%H:%M:%S')
        itm_pts = (self.atm_strike - vwap_obj.strike if direction == 'CE'
                   else vwap_obj.strike - self.atm_strike)
        session = 'EARLY' if now.time() < datetime.time(9, 40) else 'Normal'
        print(f"\n  [{ts}][Entry] {direction} strike={vwap_obj.strike} "
              f"@ {entry_price:.2f} | effVWAP={live_vwap:.2f} (ap-{config.VWAP_ADJUSTMENT:.0f}) | "
              f"dist={dist_above:.1f}pts | SL={self.sl_price:.2f} | "
              f"ITM={itm_pts}pts | {session}")
        print(f"  [Ladder] Phase0→Breakeven@+{config.BREAKEVEN_TRIGGER:.0f} | "
              f"+10lock@+{config.PHASE2_TRIGGER:.0f} | "
              f"+25lock@+{config.PHASE3_TRIGGER:.0f} | "
              f"BookProfit@+{config.BOOK_PROFIT_PTS:.0f}pts")

    def check_exit(self, now):
        if not self.in_trade or not self.active_token:
            return False, None

        strikes  = (self.ce_strikes if self.direction == 'CE'
                    else self.pe_strikes)
        vwap_obj = strikes.get(self.active_token)
        if not vwap_obj:
            return False, None

        opt = vwap_obj.ltp
        if opt <= 0:
            return False, None

        t        = now.time()
        gain_pts = opt - self.entry_price
        if opt > self.best_price:
            self.best_price = opt

        ts = now.strftime('%H:%M:%S')

        # ── Phase 4: +40pts → Book profit immediately ──────────────────────
        if gain_pts >= config.BOOK_PROFIT_PTS:
            return True, (f"Book Profit +{config.BOOK_PROFIT_PTS:.0f}pts | "
                          f"[{ts}] | "
                          f"entry={self.entry_price:.2f} exit={opt:.2f} "
                          f"P&L={gain_pts:+.2f}pts")

        # ── Phase 3: +35pts → lock SL at entry + 25 ───────────────────────
        if gain_pts >= config.PHASE3_TRIGGER and self.phase < 3:
            locked_sl = round(self.entry_price + config.PHASE3_LOCK, 2)
            if locked_sl > self.sl_price:
                self.sl_price = locked_sl
            self.phase          = 3
            self.trail_active   = True
            self.breakeven_done = True
            print(f"  [{ts}][Phase3] +{gain_pts:.1f}pts | "
                  f"SL locked at {self.sl_price:.2f} (entry+{config.PHASE3_LOCK:.0f})")

        # ── Phase 2: +25pts → lock SL at entry + 10 ───────────────────────
        elif gain_pts >= config.PHASE2_TRIGGER and self.phase < 2:
            locked_sl = round(self.entry_price + config.PHASE2_LOCK, 2)
            if locked_sl > self.sl_price:
                self.sl_price = locked_sl
            self.phase          = 2
            self.trail_active   = True
            self.breakeven_done = True
            print(f"  [{ts}][Phase2] +{gain_pts:.1f}pts | "
                  f"SL locked at {self.sl_price:.2f} (entry+{config.PHASE2_LOCK:.0f})")

        # ── Phase 1: +10pts → breakeven (SL = entry + 0) ──────────────────
        elif gain_pts >= config.BREAKEVEN_TRIGGER and self.phase < 1:
            locked_sl = round(self.entry_price, 2)   # breakeven = entry price
            if locked_sl > self.sl_price:
                self.sl_price = locked_sl
            self.phase          = 1
            self.trail_active   = True
            self.breakeven_done = True
            print(f"  [{ts}][Phase1] +{gain_pts:.1f}pts | "
                  f"SL = breakeven {self.sl_price:.2f} (entry+0)")

        # ── Phase 0: dynamic SL tracks effective VWAP ─────────────────────
        if self.phase == 0:
            live_vwap  = vwap_obj.get_vwap()   # already = ap - 2
            dynamic_sl = round(live_vwap - config.VWAP_SL_BUFFER, 2)
            cap_sl     = round(self.entry_price - config.MAX_SL_PTS, 2)
            dynamic_sl = max(dynamic_sl, cap_sl)
            if dynamic_sl > self.sl_price:
                self.sl_price = dynamic_sl

        # ── Square off ────────────────────────────────────────────────────
        if t >= datetime.time(15, 25):
            return True, (f"Square-off 3:25 PM | [{ts}] | "
                          f"entry={self.entry_price:.2f} exit={opt:.2f} "
                          f"P&L={gain_pts:+.2f}pts")

        # ── Opposite VWAP cross — flip (only before breakeven) ────────────
        ce_obj, pe_obj = self._get_mirror_pair()
        if ce_obj and pe_obj and not self.breakeven_done:
            if vwap_obj.is_below_vwap():
                opp_above = (pe_obj.is_above_vwap() if self.direction == 'CE'
                             else ce_obj.is_above_vwap())
                if opp_above:
                    return True, (f"Opposite VWAP cross — flip | "
                                   f"[{ts}] | "
                                   f"entry={self.entry_price:.2f} exit={opt:.2f} "
                                   f"P&L={gain_pts:+.2f}pts")

        # ── SL hit ────────────────────────────────────────────────────────
        if opt <= self.sl_price:
            phase_label = (
                "Phase3 SL"    if self.phase == 3 else
                "Phase2 SL"    if self.phase == 2 else
                "Breakeven SL" if self.phase == 1 else
                "Initial SL"
            )
            return True, (f"{phase_label} hit | [{ts}] | "
                          f"entry={self.entry_price:.2f} exit={opt:.2f} "
                          f"P&L={gain_pts:+.2f}pts")

        return False, None

    def on_exit(self):
        self.reset_trade()

    def get_status(self):
        ce_obj = self.ce_strikes.get(self.ce_token) if self.ce_token else None
        pe_obj = self.pe_strikes.get(self.pe_token) if self.pe_token else None
        ce_str = ce_obj.status() if ce_obj and ce_obj.tick_count > 0 else "CE:no-ticks"
        pe_str = pe_obj.status() if pe_obj and pe_obj.tick_count > 0 else "PE:no-ticks"

        if not self.in_trade:
            return f"{ce_str} | {pe_str} | Watching"

        strikes  = (self.ce_strikes if self.direction == 'CE'
                    else self.pe_strikes)
        vwap_obj = strikes.get(self.active_token)
        opt  = vwap_obj.ltp if vwap_obj else 0
        gain = opt - self.entry_price
        phase_label = f"Ph{self.phase}"
        return (f"{ce_str} | {pe_str} | "
                f"{self.direction}{self.active_strike} "
                f"entry={self.entry_price:.2f} "
                f"P&L={gain:+.2f} SL={self.sl_price:.2f} {phase_label}")
