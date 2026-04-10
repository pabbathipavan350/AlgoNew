# ============================================================
# FUTURES_ENGINE.PY — Nifty Futures VWAP Signal Detector v3
# ============================================================
# Changes from v2:
#
# 1. PROXIMITY FILTER on entries:
#    Fresh cross  : only fire if futures is within ±FRESH_CROSS_MAX_DIST of VWAP
#    Pullback     : only fire if futures is within PULLBACK_MIN/MAX_DIST of VWAP
#
# 2. OPTION VWAP tracking per token:
#    Tracks 'ap' field on any subscribed option token.
#    Used by main.py to confirm reversal vs pullback when
#    futures crosses VWAP while a trade is open.
#
# 3. Signal state remains one-shot (cleared after check_signal()).
# ============================================================

import logging
import config

logger = logging.getLogger(__name__)


class OptionVWAPTracker:
    """
    Tracks the 'ap' (session VWAP) field for a single option token.
    Used to confirm whether a mid-trade futures cross is a real
    reversal or just a pullback.
    """
    def __init__(self, token: str):
        self.token    = token
        self.ltp      = 0.0
        self.vwap     = 0.0
        self.ticks    = 0

    def on_tick(self, tick: dict):
        ltp  = float(tick.get("ltp") or tick.get("ltP") or tick.get("lp") or 0)
        vwap = float(tick.get("ap") or 0)
        if ltp > 0:
            self.ltp   = ltp
            self.ticks += 1
        if vwap > 0:
            self.vwap = vwap

    @property
    def is_above_vwap(self) -> bool | None:
        """None if not enough data, else True/False."""
        if self.vwap <= 0 or self.ltp <= 0:
            return None
        return self.ltp > self.vwap

    @property
    def is_ready(self) -> bool:
        return self.vwap > 0 and self.ltp > 0


class FuturesVWAPEngine:
    """
    Tracks a single futures token.
    Detects fresh VWAP crosses and pullback entries with proximity filter.

    Call on_tick() for every incoming WS tick on the futures token.
    Call check_signal() to get the pending signal (one-shot, clears after read).
    """

    def __init__(self):
        self.ltp         = 0.0
        self.vwap        = 0.0
        self.was_above   = None   # True/False/None
        self.tick_count  = 0
        self.signal      = None   # 'CE' | 'PE' | None
        self.signal_type = None   # 'cross' | 'pullback' | None

        # Pullback one-shot flags
        self._ce_pullback_fired = False
        self._pe_pullback_fired = False

        # Option VWAP trackers — keyed by token string
        # Populated by main.py when option is subscribed
        self.option_trackers: dict[str, OptionVWAPTracker] = {}

    def on_tick(self, tick: dict):
        """Feed a Kotak Neo v2 WS tick for the futures token."""
        ltp  = float(tick.get("ltp") or tick.get("ltP") or tick.get("lp") or 0)
        vwap = float(tick.get("ap") or 0)

        if ltp <= 0 or vwap <= 0:
            return

        self.ltp        = ltp
        self.vwap       = vwap
        self.tick_count += 1

        if self.tick_count < config.VWAP_MIN_TICKS:
            self.was_above = (ltp > vwap)
            return

        if self.was_above is None:
            self.was_above = (ltp > vwap)
            logger.info(f"[FuturesEngine] Init: LTP={ltp:.2f} VWAP={vwap:.2f} "
                        f"pos={'above' if self.was_above else 'below'}")
            return

        currently_above = (ltp > vwap)
        dist_above      = ltp - vwap   # positive=above, negative=below

        # ── Fresh cross detection ─────────────────────────
        if not self.was_above and currently_above:
            # Crossed UP → CE signal (if within proximity)
            dist = abs(dist_above)
            if dist <= config.FRESH_CROSS_MAX_DIST:
                if not self.signal:
                    self.signal      = "CE"
                    self.signal_type = "cross"
                    logger.info(f"[FuturesEngine] CROSS UP   LTP={ltp:.2f} "
                                f"VWAP={vwap:.2f} dist={dist:.1f}pts → CE cross")
            else:
                logger.info(f"[FuturesEngine] CROSS UP skipped — dist={dist:.1f}pts "
                            f"> {config.FRESH_CROSS_MAX_DIST}pts")
            self._ce_pullback_fired = False

        elif self.was_above and not currently_above:
            # Crossed DOWN → PE signal (if within proximity)
            dist = abs(dist_above)
            if dist <= config.FRESH_CROSS_MAX_DIST:
                if not self.signal:
                    self.signal      = "PE"
                    self.signal_type = "cross"
                    logger.info(f"[FuturesEngine] CROSS DOWN LTP={ltp:.2f} "
                                f"VWAP={vwap:.2f} dist={dist:.1f}pts → PE cross")
            else:
                logger.info(f"[FuturesEngine] CROSS DOWN skipped — dist={dist:.1f}pts "
                            f"> {config.FRESH_CROSS_MAX_DIST}pts")
            self._pe_pullback_fired = False

        # ── Pullback detection ─────────────────────────────
        # CE pullback: above VWAP, within [PULLBACK_MIN, PULLBACK_MAX]
        if (currently_above and
                config.PULLBACK_MIN_DIST < dist_above <= config.PULLBACK_MAX_DIST):
            if not self._ce_pullback_fired and not self.signal:
                self.signal             = "CE"
                self.signal_type        = "pullback"
                self._ce_pullback_fired = True
                logger.info(f"[FuturesEngine] PULLBACK CE  LTP={ltp:.2f} "
                            f"VWAP={vwap:.2f} dist={dist_above:.1f}pts")
        else:
            # Out of CE pullback zone — reset for next visit
            if not currently_above or dist_above > config.PULLBACK_MAX_DIST:
                self._ce_pullback_fired = False

        # PE pullback: below VWAP, within [PULLBACK_MIN, PULLBACK_MAX]
        dist_below = vwap - ltp
        if (not currently_above and
                config.PULLBACK_MIN_DIST < dist_below <= config.PULLBACK_MAX_DIST):
            if not self._pe_pullback_fired and not self.signal:
                self.signal             = "PE"
                self.signal_type        = "pullback"
                self._pe_pullback_fired = True
                logger.info(f"[FuturesEngine] PULLBACK PE  LTP={ltp:.2f} "
                            f"VWAP={vwap:.2f} dist={dist_below:.1f}pts")
        else:
            if currently_above or dist_below > config.PULLBACK_MAX_DIST:
                self._pe_pullback_fired = False

        self.was_above = currently_above

    def on_option_tick(self, token: str, tick: dict):
        """Feed a tick for any subscribed option token (for VWAP confirmation)."""
        tracker = self.option_trackers.get(token)
        if tracker:
            tracker.on_tick(tick)

    def register_option_token(self, token: str):
        """Register an option token to track its VWAP."""
        if token not in self.option_trackers:
            self.option_trackers[token] = OptionVWAPTracker(token)

    def unregister_option_token(self, token: str):
        """Remove tracker when option is unsubscribed."""
        self.option_trackers.pop(token, None)

    def get_option_vwap_position(self, token: str) -> bool | None:
        """
        Returns True if option LTP > option VWAP (above = pullback = hold).
        Returns False if option LTP < option VWAP (below = reversal = flip).
        Returns None if not enough data.
        """
        tracker = self.option_trackers.get(token)
        if tracker:
            return tracker.is_above_vwap
        return None

    def check_signal(self) -> tuple:
        """
        Returns (signal, signal_type).
        signal      : 'CE' | 'PE' | None
        signal_type : 'cross' | 'pullback' | None
        Clears after read — one-shot.
        """
        sig, typ        = self.signal, self.signal_type
        self.signal     = None
        self.signal_type = None
        return sig, typ

    def get_state(self) -> dict:
        return {
            "ltp"      : self.ltp,
            "vwap"     : self.vwap,
            "was_above": self.was_above,
            "ticks"    : self.tick_count,
        }

    @property
    def is_ready(self) -> bool:
        return self.tick_count >= config.VWAP_MIN_TICKS and self.was_above is not None
