# ============================================================
# VWAP_ENGINE.PY — TradingView-Accurate VWAP Engine
# ============================================================
# ROOT CAUSE FIX (2026-03-30):
#
#   PROBLEM 1 — ltq is almost always 0 or 1 from Kotak WS:
#     Kotak's websocket sends ltq (last-traded-qty) which is the
#     quantity of just the LAST trade — not volume since last tick.
#     When ltq=1 is used as candle volume, VWAP gets 0 weight
#     information and drifts away from TradingView.
#
#   PROBLEM 2 — seed() injects ltp*1 into accumulators:
#     seed() sets cum_tpv=ltp*1 / cum_vol=1 before any real ticks.
#     If the first real WS tick arrives in the same minute, only
#     vol=1 worth of seed is accumulated, poisoning VWAP.
#
#   PROBLEM 3 — candle_vol accumulates ltq (per-trade qty):
#     Volume for VWAP must be total traded volume across the candle,
#     not the sum of individual ltq values (each is 1-25 lots).
#
#   FIX STRATEGY:
#     A) Volume source priority:
#        1. REST delta volume  = current_vol - prev_vol (most accurate)
#        2. WS ltq accumulation (if REST not yet available)
#        3. Tick count proxy (last resort, approximate)
#        Mode is shown in status() as R/W/T.
#
#     B) seed() no longer writes to cum_tpv/cum_vol — it only seeds ltp.
#
#     C) sync_exchange_vwap() now delegates to sync_quote() which
#        computes proper volume deltas.
# ============================================================

import datetime
import config


class OptionVWAP:
    """
    Tracks TradingView-accurate VWAP for a single option strike.

    Volume weighting strategy (best -> worst):
      1. REST volume delta  (sync_quote called every 60s)       mode='rest'
      2. WS tick ltq        (unreliable on Kotak — often all 1) mode='ws'
      3. Tick count proxy   (last resort — VWAP approximate)    mode='ticks'
    """

    def __init__(self, name, strike, option_type):
        self.name        = name
        self.strike      = strike
        self.option_type = option_type

        # VWAP accumulators
        self.cum_tpv    = 0.0
        self.cum_vol    = 0.0
        self.vwap       = 0.0

        # Current 1-min candle
        self.candle_open   = 0.0
        self.candle_high   = 0.0
        self.candle_low    = 0.0
        self.candle_close  = 0.0
        self.candle_vol    = 0.0
        self.candle_ticks  = 0
        self.candle_minute = None

        # LTP / tick tracking
        self.ltp        = 0.0
        self.tick_count = 0

        # Direction tracking
        self.was_above  = False
        self.was_below  = False

        # REST volume tracking (for delta computation)
        self.last_rest_volume = 0.0
        self.last_rest_time   = None
        self.rest_synced      = False

        # Exchange reference (display only)
        self.exchange_vwap   = 0.0
        self.exchange_volume = 0.0
        self.last_sync_time  = None
        self.exchange_synced = False

        # Volume reliability mode
        self.vol_mode = 'ticks'   # 'rest' | 'ws' | 'ticks'

    # ----------------------------------------------------------
    # Tick ingestion
    # ----------------------------------------------------------

    def add_tick(self, ltp, ltq=1.0, ts=None):
        """
        Ingest one WS tick.

        ltq: last-traded-quantity from WS.  On Kotak this is often 1
             per trade — NOT the volume increment — so we do NOT use
             it as the primary volume source.  It is used only as
             fallback if REST syncs have not started yet.
        """
        if ltp <= 0:
            return

        self.ltp = ltp
        self.tick_count += 1

        if ts is None:
            ts = datetime.datetime.now()

        minute = ts.replace(second=0, microsecond=0)

        if self.candle_minute is None:
            self._start_candle(minute, ltp, ltq)

        elif minute > self.candle_minute:
            self._close_candle()
            self._start_candle(minute, ltp, ltq)

        else:
            # Same minute — update OHLC
            self.candle_high  = max(self.candle_high, ltp)
            self.candle_low   = min(self.candle_low,  ltp)
            self.candle_close = ltp
            self.candle_ticks += 1

            if self.vol_mode == 'ws':
                self.candle_vol += max(float(ltq), 1.0)
            elif self.vol_mode == 'ticks':
                self.candle_vol = float(self.candle_ticks)
            # 'rest' mode: candle_vol is managed by sync_quote()

        # Direction tracking — use get_vwap() which includes the live
        # open candle, not self.vwap which is only closed candles.
        # This matters early session (9:15-9:20) when few candles closed.
        _v = self.get_vwap()
        if _v > 0:
            if ltp > _v:
                self.was_above = True
            elif ltp < _v:
                self.was_below = True

    def _start_candle(self, minute, ltp, ltq=1.0):
        self.candle_minute = minute
        self.candle_open   = ltp
        self.candle_high   = ltp
        self.candle_low    = ltp
        self.candle_close  = ltp
        self.candle_ticks  = 1
        if self.vol_mode in ('ws', 'ticks'):
            self.candle_vol = max(float(ltq), 1.0)
        else:
            self.candle_vol = 0.0  # REST will fill via sync_quote

    def _close_candle(self):
        if self.candle_high <= 0:
            return
        # Use tick count as minimum volume guarantee
        vol = max(self.candle_vol, float(self.candle_ticks))
        if vol <= 0:
            return
        tp = (self.candle_high + self.candle_low + self.candle_close) / 3.0
        self.cum_tpv += tp * vol
        self.cum_vol += vol
        if self.cum_vol > 0:
            self.vwap = round(self.cum_tpv / self.cum_vol, 2)

    # ----------------------------------------------------------
    # REST volume sync — KEY FIX
    # ----------------------------------------------------------

    def sync_quote(self, ltp, volume, avg_price=0.0, ts=None):
        """
        Called every 60 s with REST quote data.

        Computes delta_vol = volume_now - volume_at_last_sync and
        injects it into the current open candle as a proper volume
        weight — replacing the unreliable ltq accumulation.

        Also updates candle close price so VWAP stays aligned even
        when WS ticks are sparse.
        """
        if ts is None:
            ts = datetime.datetime.now()

        if ltp > 0:
            self.ltp = ltp

        if avg_price > 0:
            self.exchange_vwap   = avg_price
            self.exchange_synced = True

        if volume <= 0:
            return

        if not self.rest_synced or self.last_rest_volume <= 0:
            # First sync — store baseline only. Do NOT assign total session
            # volume to candle_vol: the total includes all candles since 9:15,
            # not just the current open candle. Assigning it would massively
            # over-weight this one candle. Let the tick accumulation stand
            # until the second sync gives us a proper 60s delta.
            self.last_rest_volume = volume
            self.last_rest_time   = ts
            self.rest_synced      = True
            self.vol_mode         = 'rest'
            return

        delta_vol = volume - self.last_rest_volume
        last_sync_time        = self.last_rest_time   # minute of previous sync
        self.last_rest_volume = volume
        self.last_rest_time   = ts
        self.vol_mode         = 'rest'

        if delta_vol <= 0:
            return   # no new trades in this 60s window

        # Candle boundary check: if the previous sync was in a different
        # minute than the current open candle, the delta spans a boundary.
        # We can't split perfectly, so we use a time-proportional split:
        #   prev_candle_share = seconds from last_sync to candle boundary
        #   curr_candle_share = seconds from candle boundary to now
        # Both shares are injected into the respective candles via cum_tpv.
        if (self.candle_minute is not None
                and last_sync_time is not None
                and last_sync_time < self.candle_minute):
            # Delta spans at least one candle boundary.
            # Apportion: time from last_sync → candle_open vs candle_open → now
            total_secs = max((ts - last_sync_time).total_seconds(), 1.0)
            prev_secs  = max((self.candle_minute - last_sync_time).total_seconds(), 0.0)
            curr_secs  = max((ts - self.candle_minute).total_seconds(), 1.0)

            prev_share = delta_vol * (prev_secs / total_secs)
            curr_share = delta_vol * (curr_secs / total_secs)

            # The previous candle is already closed — patch its contribution
            # directly into cum_tpv/cum_vol using its stored close price.
            # We use candle_open as a proxy for the previous candle's TP
            # (best we can do without storing all closed candle data).
            if prev_share > 0 and self.candle_open > 0:
                prev_tp    = self.candle_open   # proxy: last closed candle's close ≈ open of current
                self.cum_tpv += prev_tp * prev_share
                self.cum_vol += prev_share
                if self.cum_vol > 0:
                    self.vwap = round(self.cum_tpv / self.cum_vol, 2)

            # Current candle gets its proportional share
            if curr_share > 0 and ltp > 0:
                self.candle_high  = max(self.candle_high, ltp)
                self.candle_low   = min(self.candle_low,  ltp)
                self.candle_close = ltp
                self.candle_vol  += curr_share
        else:
            # Same minute as last sync — inject full delta into open candle
            if self.candle_minute is not None and ltp > 0:
                self.candle_high  = max(self.candle_high, ltp)
                self.candle_low   = min(self.candle_low,  ltp)
                self.candle_close = ltp
                self.candle_vol  += delta_vol

        self.last_sync_time = ts

    # ----------------------------------------------------------
    # Legacy compatibility
    # ----------------------------------------------------------

    def sync_exchange_vwap(self, avg_price, volume):
        """
        Backward-compatible wrapper used by main._sync_exchange_vwap.
        Delegates to sync_quote so volume deltas are handled properly.
        avg_price from Kotak is NOT a true session VWAP — it is the
        average price for the last quote interval only.  Stored for
        display/reference only; does NOT override our VWAP.
        """
        self.sync_quote(ltp=self.ltp, volume=volume, avg_price=avg_price)

    # ----------------------------------------------------------
    # Seed
    # ----------------------------------------------------------

    def seed(self, ltp):
        """
        Seed with REST LTP before WS starts.
        IMPORTANT: we do NOT write to cum_tpv/cum_vol here.
        The old code did ltp*1 which poisoned VWAP on first candle.
        """
        if ltp > 0 and self.tick_count == 0:
            self.ltp          = ltp
            self.candle_open  = ltp
            self.candle_high  = ltp
            self.candle_low   = ltp
            self.candle_close = ltp
            # DO NOT touch cum_tpv / cum_vol / vwap

    # ----------------------------------------------------------
    # Reset
    # ----------------------------------------------------------

    def set_vwap_direct(self, vwap, volume=0.0):
        """
        Set VWAP directly from exchange data (Kotak WS 'ap' field).
        ap = to/v = Sigma(price*qty)/Sigma(qty) since 9:15 AM.
        This is the exact same calculation TradingView uses.
        Called on every WS tick — no REST polling needed.
        """
        if vwap > 0:
            self.vwap      = round(vwap, 2)
            self.cum_vol   = volume if volume > 0 else self.cum_vol
            # Sync cum_tpv so get_vwap() is consistent
            if self.cum_vol > 0:
                self.cum_tpv = self.vwap * self.cum_vol
            self.vol_mode  = 'exchange'
            self.ltp       = self.ltp  # unchanged

    def reset_vwap(self):
        self.cum_tpv       = 0.0
        self.cum_vol       = 0.0
        self.vwap          = 0.0
        self.tick_count    = 0
        self.was_above     = False
        self.was_below     = False
        self.candle_minute = None
        self.candle_vol    = 0.0
        self.candle_ticks  = 0
        self.exchange_synced  = False
        self.rest_synced      = False
        self.last_rest_volume = 0.0
        self.vol_mode      = 'ticks'

    # ----------------------------------------------------------
    # VWAP value
    # ----------------------------------------------------------

    def get_vwap(self):
        """
        Returns VWAP. When vol_mode=exchange, returns the direct
        exchange value (ap from WS tick = to/v) — exact TV match.
        """
        if self.vol_mode == 'exchange':
            return self.vwap   # set directly from ap field, no candle math

        if self.candle_minute is None:
            return self.vwap

        curr_vol = max(self.candle_vol, float(self.candle_ticks))

        if curr_vol > 0 and self.candle_high > 0:
            curr_tp = (self.candle_high + self.candle_low +
                       self.candle_close) / 3.0
            if self.cum_vol > 0:
                return round((self.cum_tpv + curr_tp * curr_vol) /
                             (self.cum_vol + curr_vol), 2)
            else:
                return round(curr_tp, 2)

        if self.cum_vol > 0:
            return round(self.cum_tpv / self.cum_vol, 2)

        return self.vwap

    # ----------------------------------------------------------
    # Derived helpers
    # ----------------------------------------------------------

    def dist_above_vwap(self):
        v = self.get_vwap()
        if v <= 0:
            return 0.0
        return round(self.ltp - v, 2)

    def is_above_vwap(self):
        v = self.get_vwap()
        return self.ltp > v > 0

    def is_below_vwap(self):
        v = self.get_vwap()
        return self.ltp < v > 0

    def is_near_vwap(self, tolerance=3.0):
        v = self.get_vwap()
        if v <= 0 or self.ltp <= 0 or self.tick_count < 3:
            return False
        dist = self.ltp - v
        return 0 <= dist <= tolerance

    def is_retesting_vwap(self, tolerance=3.0):
        return self.was_above and self.is_near_vwap(tolerance)

    def status(self):
        v    = self.get_vwap()
        side = "▲" if self.is_above_vwap() else "▼"
        mode = {'exchange': 'E', 'rest': 'R', 'ws': 'W', 'ticks': 'T'}.get(self.vol_mode, '?')
        return (f"{self.name}={self.ltp:.1f}"
                f"(V={v:.1f}/{mode}){side}")


# ==============================================================
# StrategyEngine
# ==============================================================

class StrategyEngine:
    """Multi-strike strategy engine with TradingView VWAP."""

    def __init__(self):
        self.ce_strikes  = {}
        self.pe_strikes  = {}
        self.atm_strike  = 0
        self.reset_trade()
        self.last_signal_time = None
        self.cooldown_mins    = 10

    def setup_strikes(self, spot, ce_tokens, pe_tokens, step=50):
        self.atm_strike = round(spot / 50) * 50
        self.ce_strikes = {}
        self.pe_strikes = {}
        for strike, token in ce_tokens.items():
            self.ce_strikes[token] = OptionVWAP(f"CE_{strike}", strike, 'CE')
        for strike, token in pe_tokens.items():
            self.pe_strikes[token] = OptionVWAP(f"PE_{strike}", strike, 'PE')
        print(f"  [Engine] ATM={self.atm_strike} | "
              f"CE: {sorted(ce_tokens.keys())} | "
              f"PE: {sorted(pe_tokens.keys())}")

    def update_atm(self, spot):
        self.atm_strike = round(spot / 50) * 50

    def add_tick(self, token, ltp, ltq=1.0, ts=None):
        if token in self.ce_strikes:
            self.ce_strikes[token].add_tick(ltp, ltq, ts)
        elif token in self.pe_strikes:
            self.pe_strikes[token].add_tick(ltp, ltq, ts)

    def sync_vwap(self, token, avg_price, volume):
        """
        Called from main._sync_exchange_vwap every 60s.
        Now delegates to sync_exchange_vwap which computes volume deltas.
        """
        if token in self.ce_strikes:
            self.ce_strikes[token].sync_exchange_vwap(avg_price, volume)
        elif token in self.pe_strikes:
            self.pe_strikes[token].sync_exchange_vwap(avg_price, volume)

    def sync_quote_full(self, token, ltp, volume, avg_price=0.0):
        """
        Enhanced sync — use this when you have ltp + volume + avg_price
        together (e.g. from NSE option chain or Kotak quotes).
        """
        if token in self.ce_strikes:
            self.ce_strikes[token].sync_quote(ltp, volume, avg_price)
        elif token in self.pe_strikes:
            self.pe_strikes[token].sync_quote(ltp, volume, avg_price)

    def set_vwap_direct(self, token, vwap, volume=0.0):
        """Push exchange VWAP directly into the correct strike."""
        if token in self.ce_strikes:
            self.ce_strikes[token].set_vwap_direct(vwap, volume)
        elif token in self.pe_strikes:
            self.pe_strikes[token].set_vwap_direct(vwap, volume)

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

    def _ce_signal_valid(self):
        ce_above = any(v.is_above_vwap()
                       for v in self.ce_strikes.values() if v.tick_count >= 3)
        pe_below = any(v.is_below_vwap()
                       for v in self.pe_strikes.values() if v.tick_count >= 3)
        return ce_above and pe_below

    def _pe_signal_valid(self):
        pe_above = any(v.is_above_vwap()
                       for v in self.pe_strikes.values() if v.tick_count >= 3)
        ce_below = any(v.is_below_vwap()
                       for v in self.ce_strikes.values() if v.tick_count >= 3)
        return pe_above and ce_below

    def _find_best_entry(self, direction, early_session, tolerance=3.0):
        candidates = []
        strikes = self.ce_strikes if direction == 'CE' else self.pe_strikes
        for token, vwap_obj in strikes.items():
            if vwap_obj.tick_count < 3:
                continue
            if direction == 'CE':
                itm_dist = self.atm_strike - vwap_obj.strike
            else:
                itm_dist = vwap_obj.strike - self.atm_strike
            if itm_dist < config.MIN_ITM_DISTANCE or itm_dist > config.MAX_ITM_DISTANCE:
                continue
            if not vwap_obj.is_near_vwap(tolerance):
                continue
            if early_session and not vwap_obj.was_above:
                continue
            candidates.append((itm_dist, token, vwap_obj))
        if not candidates:
            return None, None
        candidates.sort(key=lambda x: x[0])
        _, token, vwap_obj = candidates[0]
        return token, vwap_obj

    def check_entry(self, now):
        if self.in_trade:
            return None, None, None
        t = now.time()
        if t < datetime.time(9, 15) or t >= datetime.time(15, 25):
            return None, None, None
        ce_ready = any(v.tick_count >= 3 for v in self.ce_strikes.values())
        pe_ready = any(v.tick_count >= 3 for v in self.pe_strikes.values())
        if not ce_ready or not pe_ready:
            return None, None, None
        if self.last_signal_time:
            elapsed = (now - self.last_signal_time).seconds / 60
            if elapsed < self.cooldown_mins:
                return None, None, None
        early_session = t < datetime.time(9, 40)
        if self._ce_signal_valid():
            token, vwap_obj = self._find_best_entry('CE', early_session)
            if token and vwap_obj:
                return 'CE', token, vwap_obj
        if self._pe_signal_valid():
            token, vwap_obj = self._find_best_entry('PE', early_session)
            if token and vwap_obj:
                return 'PE', token, vwap_obj
        return None, None, None

    def on_entry(self, direction, token, entry_price, vwap_obj, now, target_points=None):
        t             = now.time()
        early_session = t < datetime.time(9, 40)
        dist_above    = max(entry_price - vwap_obj.get_vwap(), 0.0)
        # SL = always 5pts below VWAP at entry time (VWAP-relative)
        # Tighter and smarter — if price drops below VWAP it is wrong direction
        sl_pts        = 5.0
        self.in_trade       = True
        self.direction      = direction
        self.active_token   = token
        self.active_strike  = vwap_obj.strike
        self.entry_price    = entry_price
        self.entry_vwap     = vwap_obj.get_vwap()
        self.entry_dist     = dist_above
        self.sl_price       = round(vwap_obj.get_vwap() - sl_pts, 2)
        self.best_price     = entry_price
        self.breakeven_done = False
        self.trail_active   = False
        self.trail_sl       = self.sl_price
        self.target_points  = round(float(target_points or 0.0), 2)
        self.target_price   = round(entry_price + self.target_points, 2) if self.target_points > 0 else 0.0
        self.last_signal_time = now
        ts     = now.strftime('%H:%M:%S')
        itm_pts = (self.atm_strike - vwap_obj.strike if direction == 'CE'
                   else vwap_obj.strike - self.atm_strike)
        target_txt = (f" | TARGET={self.target_price:.2f} (+{self.target_points:.1f})"
                      if self.target_points > 0 else "")
        print(f"\n  [{ts}][Entry] {direction} strike={vwap_obj.strike} "
              f"@ {entry_price:.2f} | TV-VWAP={vwap_obj.get_vwap():.2f} | "
              f"dist={dist_above:.1f} pts | SL={self.sl_price:.2f}"
              f"{target_txt} | ITM={itm_pts}pts | vol_mode={vwap_obj.vol_mode} | "
              f"{'EARLY' if early_session else 'Normal'} VWAP-5pt SL")

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
        if gain_pts >= config.BREAKEVEN_TRIGGER and not self.breakeven_done:
            self.breakeven_done = True
            be_sl = round(self.entry_price + config.BREAKEVEN_LOCK_PTS, 2)
            if be_sl > self.sl_price:
                self.sl_price = be_sl
                self.trail_sl = be_sl
            print(f"  [{now.strftime('%H:%M:%S')}][Breakeven] SL → {be_sl:.2f} (+{gain_pts:.1f}pts)")
        if gain_pts >= config.TRAIL_TRIGGER_PTS:
            if not self.trail_active:
                self.trail_active = True
                print(f"  [{now.strftime('%H:%M:%S')}][Trail] Active at +{gain_pts:.1f}pts")
            new_trail = round(opt - config.TRAIL_MIN_PROFIT, 2)
            if new_trail > self.sl_price:
                self.sl_price = new_trail
                self.trail_sl = new_trail
        if self.target_points > 0 and gain_pts >= self.target_points:
            return True, (f"Target hit | [{now.strftime('%H:%M:%S')}] | "
                         f"entry={self.entry_price:.2f} exit={opt:.2f} P&L={gain_pts:+.2f}pts | "
                         f"target={self.target_points:.1f}pts")
        if t >= datetime.time(15, 25):
            return True, (f"Square-off 3:25 PM | [{now.strftime('%H:%M:%S')}] | "
                         f"entry={self.entry_price:.2f} exit={opt:.2f} P&L={gain_pts:+.2f}pts")

        # Opposite VWAP cross — exit before SL and flip direction
        #
        # Trigger condition (both must be true):
        #   1. Current trade price is NOW BELOW its own VWAP
        #      (trade thesis is broken — momentum reversed)
        #   2. Opposite side has crossed ABOVE its VWAP
        #      (confirms market moved the other way)
        #
        # Only trigger before breakeven — if trade is already profitable
        # or at breakeven, let normal SL/trail manage the exit.
        current_below_vwap = vwap_obj.is_below_vwap()

        if self.direction == 'CE':
            opp_crossed = any(
                v.is_above_vwap() and v.tick_count >= 3
                for v in self.pe_strikes.values()
                if abs(v.strike - self.active_strike) <= 300
            )
        else:
            opp_crossed = any(
                v.is_above_vwap() and v.tick_count >= 3
                for v in self.ce_strikes.values()
                if abs(v.strike - self.active_strike) <= 300
            )

        if current_below_vwap and opp_crossed and not self.breakeven_done:
            return True, (f"Opposite VWAP cross — flip | [{now.strftime('%H:%M:%S')}] | "
                         f"entry={self.entry_price:.2f} exit={opt:.2f} P&L={gain_pts:+.2f}pts")

        if opt <= self.sl_price:
            reason = ("Trail SL"     if self.trail_active   else
                      "Breakeven SL" if self.breakeven_done else
                      "Initial SL")
            return True, (f"{reason} hit | [{now.strftime('%H:%M:%S')}] | "
                         f"entry={self.entry_price:.2f} exit={opt:.2f} P&L={gain_pts:+.2f}pts")
        return False, None

    def on_exit(self):
        self.reset_trade()

    def get_status(self):
        ce_status = " | ".join(v.status()
                               for v in sorted(self.ce_strikes.values(),
                                               key=lambda x: x.strike)
                               if v.tick_count > 0)
        pe_status = " | ".join(v.status()
                               for v in sorted(self.pe_strikes.values(),
                                               key=lambda x: x.strike)
                               if v.tick_count > 0)
        if not self.in_trade:
            return f"CE:[{ce_status}] PE:[{pe_status}] | Watching"
        strikes  = (self.ce_strikes if self.direction == 'CE'
                    else self.pe_strikes)
        vwap_obj = strikes.get(self.active_token)
        opt  = vwap_obj.ltp if vwap_obj else 0
        gain = opt - self.entry_price
        phase = ('TRAIL' if self.trail_active else
                 'BE'    if self.breakeven_done else 'INIT')
        return (f"CE:[{ce_status}] PE:[{pe_status}] | "
                f"{self.direction}{self.active_strike} "
                f"entry={self.entry_price:.2f} "
                f"P&L={gain:+.2f} SL={self.sl_price:.2f} {phase}")
