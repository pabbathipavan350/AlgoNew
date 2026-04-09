# ============================================================
# FUTURES_ENGINE.PY — Nifty Futures VWAP Signal Detector
# ============================================================
# Tracks the ap (VWAP) field from the Kotak WebSocket on the
# Nifty current-month futures token.
#
# Two entry signals:
#
# 1. FRESH CROSS
#    CE: futures crosses from below VWAP to above VWAP (below→above)
#    PE: futures crosses from above VWAP to below VWAP (above→below)
#    Fires exactly once per cross.
#
# 2. PULLBACK
#    CE: futures was above VWAP, pulled back, and is now in the zone
#        0 < ltp - vwap <= CE_PULLBACK_PTS (within 5pts above VWAP)
#    PE: futures was below VWAP, pulled up, and is now in the zone
#        0 < vwap - ltp <= PE_PULLBACK_PTS (within 5pts below VWAP)
#    One-shot per pullback visit — resets only when price leaves the zone.
#
# The ap field IS the session VWAP (Σ price×qty / Σ qty from 9:15).
# No manual calculation needed.
# ============================================================

import logging
import config

logger = logging.getLogger(__name__)


class FuturesVWAPEngine:
    """
    Tracks a single futures token and detects:
      - Fresh VWAP crosses (both directions)
      - Pullback entries near VWAP (both directions)

    Call on_tick() for every incoming WS tick on the futures token.
    Call check_signal() to see if a signal is pending.
    """

    def __init__(self):
        self.ltp        = 0.0    # last traded price of futures
        self.vwap       = 0.0    # ap field = session VWAP
        self.was_above  = None   # True/False/None (None until initialised)
        self.tick_count = 0
        self.signal     = None   # 'CE' | 'PE' | None — one-shot, cleared after read
        self.signal_type = None  # 'cross' | 'pullback' — informational

        # Pullback one-shot flags — prevent firing multiple times in the same zone visit
        self._ce_pullback_fired = False   # True once CE pullback fired; resets when out of zone
        self._pe_pullback_fired = False   # True once PE pullback fired; resets when out of zone

    def on_tick(self, tick: dict):
        """
        Feed a raw Kotak WS tick dict here.
        Expected fields: ltp (or 'lp'), ap (= session VWAP)
        """
        ltp  = float(tick.get("ltp") or tick.get("lp") or 0)
        vwap = float(tick.get("ap") or 0)

        if ltp <= 0 or vwap <= 0:
            return

        self.ltp        = ltp
        self.vwap       = vwap
        self.tick_count += 1

        # Need minimum ticks before trusting any signal
        if self.tick_count < config.VWAP_MIN_TICKS:
            self.was_above = (ltp > vwap)
            return

        # Initialise position on first valid tick
        if self.was_above is None:
            self.was_above = (ltp > vwap)
            logger.info(f"[FuturesEngine] Initialised: LTP={ltp:.2f} VWAP={vwap:.2f} "
                        f"position={'above' if self.was_above else 'below'}")
            return

        currently_above = (ltp > vwap)
        dist_above = ltp - vwap   # positive = above, negative = below

        # ── Fresh cross detection ─────────────────────────────
        if not self.was_above and currently_above:
            # Crossed UP → Buy CE
            if not self.signal:   # don't overwrite an unread signal
                self.signal      = "CE"
                self.signal_type = "cross"
                logger.info(f"[FuturesEngine] CROSS UP   LTP={ltp:.2f} VWAP={vwap:.2f} → CE cross")
            # Reset CE pullback flag — fresh cross supersedes pullback tracking
            self._ce_pullback_fired = False

        elif self.was_above and not currently_above:
            # Crossed DOWN → Buy PE
            if not self.signal:
                self.signal      = "PE"
                self.signal_type = "cross"
                logger.info(f"[FuturesEngine] CROSS DOWN LTP={ltp:.2f} VWAP={vwap:.2f} → PE cross")
            self._pe_pullback_fired = False

        # ── Pullback detection ────────────────────────────────
        # CE pullback: was above VWAP, still above, within CE_PULLBACK_PTS of VWAP
        if currently_above and 0 < dist_above <= config.CE_PULLBACK_PTS:
            if not self._ce_pullback_fired and not self.signal:
                self.signal      = "CE"
                self.signal_type = "pullback"
                self._ce_pullback_fired = True
                logger.info(f"[FuturesEngine] PULLBACK CE  LTP={ltp:.2f} VWAP={vwap:.2f} "
                            f"dist={dist_above:.2f}pts → CE pullback")
        else:
            # Price left CE pullback zone — reset so next visit can fire again
            if not currently_above or dist_above > config.CE_PULLBACK_PTS:
                self._ce_pullback_fired = False

        # PE pullback: was below VWAP, still below, within PE_PULLBACK_PTS of VWAP
        dist_below = vwap - ltp   # positive when ltp < vwap
        if not currently_above and 0 < dist_below <= config.PE_PULLBACK_PTS:
            if not self._pe_pullback_fired and not self.signal:
                self.signal      = "PE"
                self.signal_type = "pullback"
                self._pe_pullback_fired = True
                logger.info(f"[FuturesEngine] PULLBACK PE  LTP={ltp:.2f} VWAP={vwap:.2f} "
                            f"dist={dist_below:.2f}pts → PE pullback")
        else:
            if currently_above or dist_below > config.PE_PULLBACK_PTS:
                self._pe_pullback_fired = False

        self.was_above = currently_above

    def check_signal(self) -> tuple:
        """
        Returns (signal, signal_type) where:
            signal      : 'CE' | 'PE' | None
            signal_type : 'cross' | 'pullback' | None

        Clears the signal after reading — each signal fires exactly once.
        """
        sig  = self.signal
        typ  = self.signal_type
        self.signal      = None
        self.signal_type = None
        return sig, typ

    def get_state(self) -> dict:
        return {
            "ltp"       : self.ltp,
            "vwap"      : self.vwap,
            "was_above" : self.was_above,
            "ticks"     : self.tick_count,
        }

    @property
    def is_ready(self) -> bool:
        """True once we have enough ticks to trust signals."""
        return self.tick_count >= config.VWAP_MIN_TICKS and self.was_above is not None
