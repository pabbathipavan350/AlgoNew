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
# STRIKE PAIRING (always exact mirror, 200pts ITM):
#   ATM = round(nifty / 50) * 50
#   CE strike = ATM - 200  (200pts ITM for calls)
#   PE strike = ATM + 200  (200pts ITM for puts)
#   Entry only valid when CE is 0-5pts above its VWAP AND
#   PE (mirror) is below its VWAP, or vice versa.
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
        self.was_above   = False
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
        """Set VWAP from Kotak WS 'ap' field. Called on every tick."""
        if vwap > 0:
            self.vwap = round(vwap, 2)

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

    def status(self):
        side = "▲" if self.is_above_vwap() else "▼"
        return f"{self.name}={self.ltp:.1f}(V={self.vwap:.1f}){side}"


# ==============================================================
# StrategyEngine
# ==============================================================

class StrategyEngine:
    """
    Strategy engine tracking exactly 1 CE + 1 PE mirror pair.

    Mirror pair (always 200pts ITM):
      CE strike = ATM - 200
      PE strike = ATM + 200

    Entry rules:
      CE entry: CE is 0-5pts above its VWAP AND PE is below its VWAP
      PE entry: PE is 0-5pts above its VWAP AND CE is below its VWAP
    """

    def __init__(self):
        self.ce_strikes  = {}   # token -> OptionVWAP
        self.pe_strikes  = {}   # token -> OptionVWAP
        self.atm_strike  = 0

        # Active mirror pair tokens
        self.ce_token    = None
        self.pe_token    = None

        self.reset_trade()
        self.last_signal_time = None
        self.cooldown_mins    = 10

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
        print(f"  [Engine] ATM={self.atm_strike} | "
              f"CE pair: {self.atm_strike - 200} (token={self.ce_token}) | "
              f"PE pair: {self.atm_strike + 200} (token={self.pe_token})")

    def _identify_pair(self):
        """Find the exact 200pt ITM mirror pair tokens."""
        ce_target = self.atm_strike - 200
        pe_target = self.atm_strike + 200
        self.ce_token = None
        self.pe_token = None
        for token, vobj in self.ce_strikes.items():
            if vobj.strike == ce_target:
                self.ce_token = token
                break
        for token, vobj in self.pe_strikes.items():
            if vobj.strike == pe_target:
                self.pe_token = token
                break

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
        self.in_trade       = False
        self.direction      = None
        self.active_token   = None
        self.active_strike  = None
        self.entry_price    = 0.0
        self.entry_vwap     = 0.0
        self.entry_dist     = 0.0
        self.sl_price       = 0.0
        self.best_price     = 0.0
        self.breakeven_done = False
        self.trail_active   = False
        self.trail_sl       = 0.0
        self.target_points  = 0.0
        self.target_price   = 0.0

    def _get_mirror_pair(self):
        """
        Return (ce_vwap_obj, pe_vwap_obj) for the exact 200pt ITM mirror pair.
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
        CE entry: CE in 0-5pts above VWAP, PE below VWAP.
        PE entry: PE in 0-5pts above VWAP, CE below VWAP.
        Early session (<9:40): also requires was_above=True (confirmed crossover).
        """
        if self.in_trade:
            return None, None, None

        t = now.time()
        if t < datetime.time(9, 15) or t >= datetime.time(15, 25):
            return None, None, None

        if self.last_signal_time:
            elapsed = (now - self.last_signal_time).total_seconds() / 60
            if elapsed < self.cooldown_mins:
                return None, None, None

        ce_obj, pe_obj = self._get_mirror_pair()
        if not ce_obj or not pe_obj:
            return None, None, None

        early_session = t < datetime.time(9, 40)

        # CE entry: CE 0-5pts above its VWAP, PE below its VWAP
        if ce_obj.is_near_vwap(config.ENTRY_ZONE_PTS) and pe_obj.is_below_vwap():
            if not early_session or ce_obj.was_above:
                return 'CE', self.ce_token, ce_obj

        # PE entry: PE 0-5pts above its VWAP, CE below its VWAP
        if pe_obj.is_near_vwap(config.ENTRY_ZONE_PTS) and ce_obj.is_below_vwap():
            if not early_session or pe_obj.was_above:
                return 'PE', self.pe_token, pe_obj

        return None, None, None

    def on_entry(self, direction, token, entry_price, vwap_obj, now, target_points=None):
        live_vwap  = vwap_obj.get_vwap()
        dist_above = max(entry_price - live_vwap, 0.0)

        self.sl_price       = round(live_vwap - config.VWAP_SL_BUFFER, 2)
        self.in_trade       = True
        self.direction      = direction
        self.active_token   = token
        self.active_strike  = vwap_obj.strike
        self.entry_price    = entry_price
        self.entry_vwap     = live_vwap
        self.entry_dist     = dist_above
        self.best_price     = entry_price
        self.breakeven_done = False
        self.trail_active   = False
        self.trail_sl       = self.sl_price
        self.target_points  = round(float(target_points or 0.0), 2)
        self.target_price   = round(entry_price + self.target_points, 2) if self.target_points > 0 else 0.0
        self.last_signal_time = now
        vwap_obj.reset_peak()

        ts      = now.strftime('%H:%M:%S')
        itm_pts = (self.atm_strike - vwap_obj.strike if direction == 'CE'
                   else vwap_obj.strike - self.atm_strike)
        target_txt = (f" | TARGET={self.target_price:.2f} (+{self.target_points:.1f})"
                      if self.target_points > 0 else "")
        session = 'EARLY' if now.time() < datetime.time(9, 40) else 'Normal'
        print(f"\n  [{ts}][Entry] {direction} strike={vwap_obj.strike} "
              f"@ {entry_price:.2f} | VWAP={live_vwap:.2f} | "
              f"dist={dist_above:.1f}pts | SL={self.sl_price:.2f} "
              f"(VWAP-{config.VWAP_SL_BUFFER}){target_txt} | "
              f"ITM={itm_pts}pts | {session}")

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

        # Dynamic SL: tracks live VWAP - buffer (ratchet — only moves up)
        if not self.breakeven_done and not self.trail_active:
            live_vwap  = vwap_obj.get_vwap()
            dynamic_sl = round(live_vwap - config.VWAP_SL_BUFFER, 2)
            if dynamic_sl > self.sl_price:
                self.sl_price = dynamic_sl
                self.trail_sl = dynamic_sl

        # Breakeven lock
        if gain_pts >= config.BREAKEVEN_TRIGGER and not self.breakeven_done:
            self.breakeven_done = True
            be_sl = round(self.entry_price + config.BREAKEVEN_LOCK_PTS, 2)
            if be_sl > self.sl_price:
                self.sl_price = be_sl
                self.trail_sl = be_sl
            print(f"  [{now.strftime('%H:%M:%S')}][Breakeven] "
                  f"SL → {be_sl:.2f} (+{gain_pts:.1f}pts)")

        # Trail SL
        if gain_pts >= config.TRAIL_TRIGGER_PTS:
            if not self.trail_active:
                self.trail_active = True
                print(f"  [{now.strftime('%H:%M:%S')}][Trail] "
                      f"Active at +{gain_pts:.1f}pts | "
                      f"SL={round(opt - config.TRAIL_MIN_PROFIT, 2):.2f}")
            new_trail = round(opt - config.TRAIL_MIN_PROFIT, 2)
            if new_trail > self.sl_price:
                self.sl_price = new_trail
                self.trail_sl = new_trail

        # Target hit
        if self.target_points > 0 and gain_pts >= self.target_points:
            return True, (f"Target hit | [{now.strftime('%H:%M:%S')}] | "
                          f"entry={self.entry_price:.2f} exit={opt:.2f} "
                          f"P&L={gain_pts:+.2f}pts | target={self.target_points:.1f}pts")

        # Square off
        if t >= datetime.time(15, 25):
            return True, (f"Square-off 3:25 PM | [{now.strftime('%H:%M:%S')}] | "
                          f"entry={self.entry_price:.2f} exit={opt:.2f} "
                          f"P&L={gain_pts:+.2f}pts")

        # Opposite VWAP cross — flip (only before breakeven)
        ce_obj, pe_obj = self._get_mirror_pair()
        if ce_obj and pe_obj and not self.breakeven_done:
            if vwap_obj.is_below_vwap():
                opp_above = (pe_obj.is_above_vwap() if self.direction == 'CE'
                             else ce_obj.is_above_vwap())
                if opp_above:
                    return True, (f"Opposite VWAP cross — flip | "
                                   f"[{now.strftime('%H:%M:%S')}] | "
                                   f"entry={self.entry_price:.2f} exit={opt:.2f} "
                                   f"P&L={gain_pts:+.2f}pts")

        # SL hit
        if opt <= self.sl_price:
            reason = ("Trail SL"     if self.trail_active   else
                      "Breakeven SL" if self.breakeven_done else
                      "Initial SL")
            return True, (f"{reason} hit | [{now.strftime('%H:%M:%S')}] | "
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
        phase = ('TRAIL' if self.trail_active else
                 'BE'    if self.breakeven_done else 'INIT')
        return (f"{ce_str} | {pe_str} | "
                f"{self.direction}{self.active_strike} "
                f"entry={self.entry_price:.2f} "
                f"P&L={gain:+.2f} SL={self.sl_price:.2f} {phase}")
