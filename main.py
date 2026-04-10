# ============================================================
# MAIN.PY — Nifty Futures VWAP Options Algo v3
# ============================================================
# NEW in v3 vs v2:
#
# 1. STARTUP PRE-LOAD
#    All strike tokens + OI fetched at startup.
#    pick_strike() at signal time reads from cache — no delay.
#
# 2. VIX-BASED SL + TARGET
#    Fetched at startup via index quote.
#    High VIX (>15): SL=15, Target=50
#    Low  VIX (≤15): SL=10, Target=35
#
# 3. TRAILING SL
#    Activates at +20 pts profit.
#    SL = current_ltp - TRAIL_BUFFER (ratchets up, never down).
#
# 4. ENTRY PROXIMITY FILTER
#    Fresh cross: only if futures within ±10 pts of VWAP.
#    Pullback   : only if within 0–15 pts of VWAP.
#    (Implemented in FuturesVWAPEngine.)
#
# 5. OPTION VWAP CONFIRMATION (mid-trade cross)
#    When in a trade and futures crosses VWAP again:
#      option LTP > option VWAP → just a pullback → hold
#      option LTP < option VWAP → real reversal → flip/exit
#
# 6. DIRECTION FATIGUE RULE
#    After 2 profitable exits in same direction:
#      → skip next signal in that direction for 15 mins
#
# 7. DAILY GUARDS
#    Max 6 trades/day
#    3 consecutive SL hits → stop for day
#    2 full targets hit    → stop for day
# ============================================================

import threading
import signal
import logging
import logging.handlers
import os
import datetime
import time

import config
from auth              import get_kotak_session
from futures_engine    import FuturesVWAPEngine
from option_manager    import OptionManager, find_futures_token, fetch_ltp
from capital_manager   import CapitalManager
from report_manager    import ReportManager
from session_manager   import SessionManager
from telegram_notifier import TelegramNotifier


def now_ist() -> datetime.datetime:
    return datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)


def setup_logging():
    os.makedirs("logs", exist_ok=True)
    log_file = f"logs/algo_{now_ist().strftime('%Y%m%d')}.log"
    fmt      = "%(asctime)s %(levelname)-8s %(name)s | %(message)s"
    root     = logging.getLogger()
    root.setLevel(logging.DEBUG)
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=20*1024*1024, backupCount=5, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(fmt))
    root.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(fmt))
    root.addHandler(ch)
    return logging.getLogger("main")


class FuturesVWAPAlgo:

    def __init__(self):
        self.logger         = setup_logging()
        self.client         = None
        self.session_mgr    = None
        self.opt_mgr        = None
        self.cap_mgr        = None
        self.report_mgr     = None
        self.telegram       = TelegramNotifier()
        self.futures_engine = FuturesVWAPEngine()

        # Tokens
        self.futures_token  = None
        self.option_token   = None

        # VIX state (set at startup)
        self.current_vix    = 0.0
        self.high_vix       = True    # default to high until VIX fetched
        self.sl_pts         = config.SL_PTS_HIGH_VIX
        self.target_pts     = config.TARGET_PTS_HIGH_VIX

        # Position state
        self.in_trade       = False
        self.direction      = None
        self.entry_type     = None
        self.strike         = None
        self.entry_price    = 0.0
        self.entry_time     = None
        self.entry_vwap     = 0.0
        self.sl_price       = 0.0
        self.target_price   = 0.0
        self.peak_price     = 0.0
        self.trail_active   = False
        self.qty            = config.LOTS * config.LOT_SIZE
        self.option_ltp     = 0.0

        # Day counters
        self.day_pnl_rs         = 0.0
        self.trade_count        = 0
        self.consec_sl          = 0   # consecutive SL hits
        self.day_stopped        = False

        # Direction fatigue tracking
        # {direction: {'wins': N, 'last_win_time': datetime}}
        self._dir_wins: dict      = {"CE": {"wins": 0, "last_win_time": None},
                                     "PE": {"wins": 0, "last_win_time": None}}

        # Pre-load flag
        self._preloaded         = False
        self._first_futures_ltp = 0.0

        # Auto-shutdown timer (set properly in initialize())
        self._start_time    = now_ist()
        self._shutdown_time = self._start_time + datetime.timedelta(hours=5, minutes=50)

        # No-tick circuit breaker
        self._last_tick_time  = now_ist()
        self._circuit_alerted = False

        self._running = True
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        signal.signal(signal.SIGINT,  self._handle_sigterm)

    # ── Init ─────────────────────────────────────────────

    def initialize(self):
        print("\n" + "="*60)
        print("  Nifty Futures VWAP Options Algo  v3")
        print(f"  Mode    : {'*** PAPER TRADE ***' if config.PAPER_TRADE else '*** LIVE ***'}")
        print(f"  Capital : Rs {config.TOTAL_CAPITAL:,.0f}  |  {config.LOTS} lots × {config.LOT_SIZE}")
        print(f"  Guards  : {config.MAX_CONSEC_SL} consec SL stops day | "
              f"Loss limit Rs{config.MAX_DAILY_LOSS_RS:,.0f}")
        print("="*60)

        # Record start time — auto-shutdown after 5h 50m from start
        self._start_time    = now_ist()
        self._shutdown_time = self._start_time + datetime.timedelta(hours=5, minutes=50)
        print(f"[Init] Started at   : {self._start_time.strftime('%H:%M:%S')} IST")
        print(f"[Init] Auto-shutdown: {self._shutdown_time.strftime('%H:%M:%S')} IST")

        self.client = get_kotak_session()

        self.session_mgr = SessionManager(self.client, get_kotak_session)
        self.session_mgr.on_reconnect = self._on_reconnect
        self.session_mgr.start()

        self.cap_mgr    = CapitalManager()
        self.opt_mgr    = OptionManager(self.client)
        self.report_mgr = ReportManager(self.cap_mgr)

        # Resolve futures token
        self._resolve_futures_token()

        # Fetch VIX at startup
        self._fetch_vix()

        # Setup WS callbacks
        self._setup_websocket()

        exp = self.opt_mgr.expiry_date
        print(f"\n[Init] Expiry : {exp.strftime('%d %b %Y')} "
              f"(in {(exp - datetime.date.today()).days}d)")
        vix_regime = 'HIGH' if self.high_vix else 'LOW'
        print(f"[Init] VIX    : {self.current_vix:.1f}  ->  {vix_regime} regime  "
              f"SL={self.sl_pts}  Target={self.target_pts}  "
              f"Trail: +{config.TRAIL_BREAKEVEN_TRIGGER}pts->BE  "
              f"+{config.TRAIL_LOCK_TRIGGER}pts->+{config.TRAIL_LOCK_PTS}locked")
        print(f"[Init] Futures token: {self.futures_token}")

        # Pre-load strikes NOW using Nifty index quote — before 9:15
        self._preload_at_startup()

        self.telegram.alert_startup(
            mode   = "PAPER" if config.PAPER_TRADE else "LIVE",
            expiry = str(exp),
            atm    = f"VIX={self.current_vix:.1f}",
        )
        print("[Init] Initialisation complete — starting WebSocket")
        print("[Init] Entries blocked until 09:16:00 IST\n")

    def _preload_at_startup(self):
        """
        Fetch Nifty spot from index quote at startup and run preload_strikes()
        synchronously — before WS starts, so all data is ready before 9:15.
        Falls back to trigger on first futures tick if index quote fails.
        """
        spot = self._fetch_nifty_spot()
        if spot <= 0:
            print("[PreLoad] Could not fetch Nifty spot at startup — "
                  "will preload on first futures tick")
            return
        print(f"[PreLoad] Startup spot: {spot:.0f} (from Nifty index quote)")
        try:
            self.opt_mgr.preload_strikes(spot)
            self._preloaded = True
        except Exception as e:
            self.logger.error(f"[PreLoad] Startup error: {e}", exc_info=True)
            print(f"[PreLoad] Preload failed ({e}) — will retry on first futures tick")

    def _fetch_nifty_spot(self) -> float:
        """Fetch current Nifty 50 index LTP (used for startup pre-load)."""
        try:
            resp = self.client.quotes(
                instrument_tokens=[{"instrument_token": config.NIFTY_INDEX_TOKEN,
                                    "exchange_segment": config.CM_SEGMENT}],
                quote_type="ltp"
            )
            if isinstance(resp, dict):
                data = resp.get("message") or resp.get("data") or []
            elif isinstance(resp, list):
                data = resp
            else:
                data = []
            if data:
                for f in ("ltp", "ltP", "last_price", "lastPrice", "close"):
                    v = data[0].get(f)
                    if v and float(v) > 0:
                        return float(v)
        except Exception as e:
            self.logger.debug(f"_fetch_nifty_spot: {e}")
        return 0.0

    def _fetch_vix(self):
        """Fetch India VIX from Kotak quotes API and set SL/target."""
        INDIA_VIX_TOKEN = "26074"   # India VIX index token on NSE
        try:
            resp = self.client.quotes(
                instrument_tokens=[{"instrument_token": INDIA_VIX_TOKEN,
                                    "exchange_segment": config.CM_SEGMENT}],
                quote_type="ltp"
            )
            if isinstance(resp, dict):
                data = resp.get("message") or resp.get("data") or []
            elif isinstance(resp, list):
                data = resp
            else:
                data = []
            if data:
                vix = float(data[0].get("ltp") or data[0].get("ltP") or 0)
                if vix > 0:
                    self.current_vix = vix
        except Exception as e:
            self.logger.debug(f"VIX fetch error: {e}")

        if self.current_vix <= 0:
            print("[VIX] Could not fetch — defaulting to HIGH VIX regime")
            self.current_vix = 20.0   # safe default

        self.high_vix     = self.current_vix > config.VIX_HIGH_THRESHOLD
        self.sl_pts       = config.SL_PTS_HIGH_VIX     if self.high_vix else config.SL_PTS_LOW_VIX
        self.target_pts   = config.TARGET_PTS_HIGH_VIX  if self.high_vix else config.TARGET_PTS_LOW_VIX
        self.report_mgr.set_vix(self.current_vix)

    def _resolve_futures_token(self):
        expiry_str = self.opt_mgr.expiry_str
        print(f"[Init] Resolving futures token for expiry {expiry_str}...")
        self.futures_token = find_futures_token(self.client, expiry_str)
        if not self.futures_token:
            raise RuntimeError(
                f"Could not resolve Nifty futures token for {expiry_str}."
            )

    # ── WebSocket ─────────────────────────────────────────

    def _setup_websocket(self):
        self.client.on_message = self._on_message
        self.client.on_error   = self._on_ws_error
        self.client.on_close   = self._on_ws_close
        self.client.on_open    = self._on_ws_open

    def _on_ws_open(self, *args):
        print("[WS] Connected")

    def _on_ws_error(self, error):
        self.logger.error(f"[WS] Error: {error}")

    def _on_ws_close(self, *args):
        self.logger.warning("[WS] Closed")

    def _subscribe_futures(self):
        if not self.futures_token:
            return
        try:
            self.client.subscribe(
                instrument_tokens=[{"instrument_token": self.futures_token,
                                    "exchange_segment": config.FO_SEGMENT}],
                isIndex=False, isDepth=False,
            )
            print(f"[WS] Subscribed futures token={self.futures_token}")
        except Exception as e:
            self.logger.error(f"Subscribe futures: {e}")

    def _subscribe_option(self, token: str):
        try:
            self.client.subscribe(
                instrument_tokens=[{"instrument_token": token,
                                    "exchange_segment": config.FO_SEGMENT}],
                isIndex=False, isDepth=False,
            )
            self.futures_engine.register_option_token(token)
        except Exception as e:
            self.logger.error(f"Subscribe option: {e}")

    def _unsubscribe_option(self, token: str):
        try:
            self.client.unsubscribe(
                instrument_tokens=[{"instrument_token": token,
                                    "exchange_segment": config.FO_SEGMENT}],
                isIndex=False, isDepth=False,
            )
            self.futures_engine.unregister_option_token(token)
        except Exception:
            pass

    # ── Tick handler ──────────────────────────────────────

    def _on_message(self, message):
        try:
            if not isinstance(message, dict):
                return
            msg_type = message.get('type', '')
            if msg_type not in ('stock_feed', 'sf', 'index_feed', 'if'):
                return
            ticks = message.get('data', [])
            if not ticks:
                return
            for tick in ticks:
                token = str(tick.get('tk') or tick.get('token') or
                            tick.get('instrument_token') or '')
                ltp   = float(tick.get('ltp') or tick.get('ltP') or 0)
                if ltp <= 0:
                    continue
                self._last_tick_time  = now_ist()
                self._circuit_alerted = False

                if token == str(self.futures_token):
                    self._on_futures_tick(tick)
                elif token == str(self.option_token):
                    self._on_option_tick(tick)
                # Always forward to option VWAP tracker
                self.futures_engine.on_option_tick(token, tick)

        except Exception as e:
            self.logger.error(f"_on_message: {e}", exc_info=True)

    def _on_futures_tick(self, tick: dict):
        self.futures_engine.on_tick(tick)
        state = self.futures_engine.get_state()
        ltp   = state["ltp"]

        # Fallback preload on first futures tick if startup preload failed
        if not self._preloaded and ltp > 0:
            self._trigger_preload(ltp)

        if not self._is_market_hours(now_ist()):
            return

        sig, sig_type = self.futures_engine.check_signal()
        if not sig:
            return

        if self.in_trade and self.direction == sig:
            # Same direction — check if it's a real reversal via option VWAP
            self.logger.debug(f"[Guard] {sig} signal while in {self.direction} — ignored")
            return

        if self.in_trade and self.direction != sig:
            # Opposite direction signal — use option VWAP confirmation
            self._handle_opposite_signal(sig, sig_type, state)
        else:
            # No open trade — evaluate entry
            self._on_signal(sig, sig_type, state["ltp"], state["vwap"], now_ist())

    def _handle_opposite_signal(self, sig: str, sig_type: str, state: dict):
        """
        Opposite direction signal while in a trade.
        Use option VWAP to confirm reversal vs pullback.
        """
        if not config.ENABLE_OPTION_VWAP_CONFIRM or not self.option_token:
            # No confirmation filter — flip immediately
            self._on_signal(sig, sig_type, state["ltp"], state["vwap"], now_ist())
            return

        opt_above_vwap = self.futures_engine.get_option_vwap_position(self.option_token)

        if opt_above_vwap is None:
            # Not enough option VWAP data — flip (safe default)
            self.logger.info("[Confirm] No option VWAP data — treating as reversal")
            self._on_signal(sig, sig_type, state["ltp"], state["vwap"], now_ist())
            return

        if opt_above_vwap:
            # Option above its VWAP → just a pullback → hold current trade
            self.logger.info(
                f"[Confirm] {sig} signal but option LTP above option VWAP "
                f"→ PULLBACK — holding {self.direction} trade"
            )
            print(f"[Confirm] Futures crossed {sig} but option is ABOVE its VWAP "
                  f"→ pullback, not reversal — holding {self.direction}")
        else:
            # Option below its VWAP → real reversal → flip
            self.logger.info(
                f"[Confirm] {sig} signal + option LTP below option VWAP "
                f"→ REVERSAL — flipping to {sig}"
            )
            print(f"[Confirm] Option BELOW its VWAP → confirmed reversal → flip to {sig}")
            self._on_signal(sig, sig_type, state["ltp"], state["vwap"], now_ist())

    def _on_option_tick(self, tick: dict):
        ltp = float(tick.get("ltp") or tick.get("ltP") or tick.get("lp") or 0)
        if ltp <= 0 or not self.in_trade:
            return

        self.option_ltp = ltp
        if ltp > self.peak_price:
            self.peak_price = ltp

        profit_pts = ltp - self.entry_price

        # ── Trailing SL — two-step ratchet ───────────────
        # Step 1: +20 pts → move SL to breakeven (entry price)
        # Step 2: +30 pts → move SL to entry + 10 pts (locked forever)
        if profit_pts >= config.TRAIL_LOCK_TRIGGER:
            lock_sl = round(self.entry_price + config.TRAIL_LOCK_PTS, 2)
            if lock_sl > self.sl_price:
                self.logger.info(f"[Trail] LOCK  SL {self.sl_price:.2f} → {lock_sl:.2f} "
                                 f"(+{profit_pts:.1f}pts ≥ +{config.TRAIL_LOCK_TRIGGER})")
                self.sl_price     = lock_sl
                self.trail_active = True
        elif profit_pts >= config.TRAIL_BREAKEVEN_TRIGGER:
            be_sl = round(self.entry_price, 2)
            if be_sl > self.sl_price:
                self.logger.info(f"[Trail] BE    SL {self.sl_price:.2f} → {be_sl:.2f} "
                                 f"(+{profit_pts:.1f}pts ≥ +{config.TRAIL_BREAKEVEN_TRIGGER})")
                self.sl_price     = be_sl
                self.trail_active = True

        # ── Target check ──────────────────────────────────
        if ltp >= self.target_price:
            self.logger.info(f"[Exit] TARGET ltp={ltp:.2f} tgt={self.target_price:.2f}")
            self._exit_trade(ltp, "Target")
            return

        # ── SL check ─────────────────────────────────────
        if ltp <= self.sl_price:
            reason = "Trail SL" if self.trail_active else "SL"
            self.logger.info(f"[Exit] {reason} ltp={ltp:.2f} sl={self.sl_price:.2f}")
            self._exit_trade(ltp, reason)

    # ── Pre-load trigger ─────────────────────────────────

    def _trigger_preload(self, spot: float):
        """Run preload_strikes() in a background thread so WS keeps ticking."""
        self._preloaded = True
        print(f"\n[PreLoad] First futures tick: spot={spot:.0f} — starting pre-load...")

        def _do_preload():
            try:
                self.opt_mgr.preload_strikes(spot)
            except Exception as e:
                self.logger.error(f"[PreLoad] Error: {e}", exc_info=True)

        t = threading.Thread(target=_do_preload, daemon=True, name="PreLoad")
        t.start()

    # ── Signal handler ────────────────────────────────────

    def _on_signal(self, direction: str, sig_type: str,
                   futures_ltp: float, futures_vwap: float,
                   t: datetime.datetime):

        # ── Day stop guards ───────────────────────────────
        if self.day_stopped:
            self.logger.info(f"[Guard] Day stopped — ignoring {direction} signal")
            return

        if self.consec_sl >= config.MAX_CONSEC_SL:
            if not self.day_stopped:
                self.day_stopped = True
                msg = f"{config.MAX_CONSEC_SL} consecutive SLs — stopped for day"
                print(f"\n[Guard] {msg}")
                self.telegram.alert_risk(msg)
            return

        if self.day_pnl_rs <= config.MAX_DAILY_LOSS_RS:
            if not self.day_stopped:
                self.day_stopped = True
                msg = f"Daily loss limit Rs{config.MAX_DAILY_LOSS_RS:,.0f} hit — stopped"
                print(f"\n[Guard] {msg}")
                self.telegram.alert_risk(msg)
            return

        # ── Timing guards ─────────────────────────────────
        # No entries before 9:16:00 — give market time to stabilise
        entry_open = datetime.time(9, 16, 0)
        if t.time() < entry_open:
            self.logger.info(f"[Guard] Entry blocked — before 09:16 ({t.strftime('%H:%M:%S')})")
            return

        sq_time = datetime.time(*map(int, config.SQUARE_OFF_TIME.split(":")))
        if t.time() >= sq_time:
            return
        if datetime.date.today() == self.opt_mgr.expiry_date:
            cutoff = datetime.time(*map(int, config.EXPIRY_DAY_CUTOFF.split(":")))
            if t.time() >= cutoff:
                return

        # ── Direction fatigue guard ───────────────────────
        if self._is_direction_fatigued(direction, t):
            self.logger.info(f"[Fatigue] {direction} fatigued — skipping")
            print(f"[Fatigue] {direction} skipped — {config.DIRECTION_FATIGUE_COUNT} wins, "
                  f"cooldown {config.DIRECTION_COOLDOWN_MINS} min")
            return

        # ── Close opposite trade if open ──────────────────
        if self.in_trade and self.direction != direction:
            print(f"\n[Flip] Closing {self.direction} → entering {direction}")
            self._exit_trade(self.option_ltp or self.entry_price, "Flip")

        # ── Pre-load not ready yet? ───────────────────────
        if not self._preloaded or not self.opt_mgr._strike_cache.get(direction):
            print(f"[Signal] {direction} signal but pre-load not ready — running live scan")

        print(f"\n{'='*55}")
        print(f"[Signal] {direction} {sig_type.upper()} at {t.strftime('%H:%M:%S')}")
        print(f"         Futures={futures_ltp:.2f}  VWAP={futures_vwap:.2f}  "
              f"dist={futures_ltp-futures_vwap:+.2f}pts")

        # ── Pick strike (from cache) ──────────────────────
        info = self.opt_mgr.pick_strike(futures_ltp, direction)
        if not info:
            self.logger.error(f"No strike for {direction}")
            self.telegram.alert_risk(f"No {direction} strike at {t.strftime('%H:%M')}")
            return

        # ── Get fresh LTP for order pricing ──────────────
        # Use cached LTP first; fall back to live fetch
        option_ltp = info.get("ltp") or 0.0
        if option_ltp <= 0:
            option_ltp = fetch_ltp(self.client, info["token"])
        if option_ltp <= 0:
            self.logger.error("Option LTP unavailable")
            return

        print(f"[Strike] {direction} {info['strike']}  "
              f"delta={info['delta']:.2f}  OI={info['oi']:,}  LTP={option_ltp:.2f}")

        # ── Place buy order ───────────────────────────────
        fill = self.opt_mgr.place_buy_order(
            token=info["token"], strike=info["strike"],
            direction=direction, ltp=option_ltp,
        )
        if not fill:
            self.logger.error("Buy order not filled")
            return

        fill_px = fill["fill_price"]

        # ── Set trade state ───────────────────────────────
        self.in_trade     = True
        self.direction    = direction
        self.entry_type   = sig_type
        self.strike       = info["strike"]
        self.option_token = info["token"]
        self.entry_price  = fill_px
        self.entry_time   = t
        self.entry_vwap   = futures_vwap
        self.sl_price     = round(fill_px - self.sl_pts, 2)
        self.target_price = round(fill_px + self.target_pts, 2)
        self.peak_price   = fill_px
        self.trail_active = False
        self.option_ltp   = fill_px

        self.trade_count += 1
        self._subscribe_option(info["token"])

        print(f"\n✅ ENTRY #{self.trade_count}")
        print(f"   Direction  : {direction}  ({sig_type.upper()})")
        print(f"   Strike     : {info['strike']}  exp={info['expiry_str']}")
        print(f"   Entry      : Rs {fill_px:.2f}")
        print(f"   SL         : Rs {self.sl_price:.2f}  (−{self.sl_pts} pts)")
        print(f"   Target     : Rs {self.target_price:.2f}  (+{self.target_pts} pts)")
        print(f"   Trail      : +{config.TRAIL_BREAKEVEN_TRIGGER}pts→BE, +{config.TRAIL_LOCK_TRIGGER}pts→+{config.TRAIL_LOCK_PTS}pts locked")
        print(f"   Futures    : {futures_ltp:.2f}  VWAP={futures_vwap:.2f}")
        print(f"   VIX regime : {'HIGH' if self.high_vix else 'LOW'} ({self.current_vix:.1f})")

        self.telegram.alert_entry(
            direction=direction, strike=info["strike"],
            entry_price=fill_px, vwap=futures_vwap,
            sl=self.sl_price, target=self.target_price, qty=self.qty,
        )

    # ── Exit handler ─────────────────────────────────────

    def _exit_trade(self, exit_ltp: float, reason: str):
        if not self.in_trade:
            return

        self.in_trade = False
        exit_time     = now_ist()

        actual_exit = self.opt_mgr.place_exit_order(
            token=self.option_token, strike=self.strike,
            direction=self.direction, qty=self.qty, reason=reason,
        )
        exit_price = actual_exit if actual_exit else exit_ltp

        pts_gained = round(exit_price - self.entry_price, 2)
        pnl_rs     = round(pts_gained * self.qty, 2)
        cost       = OptionManager.calc_trade_cost(self.entry_price, exit_price, self.qty)
        net_rs     = round(pnl_rs - cost, 2)

        self.day_pnl_rs += net_rs
        self.cap_mgr.update_after_trade(net_rs)
        duration = round((exit_time - self.entry_time).total_seconds() / 60, 1)

        # ── Update day counters ───────────────────────────
        is_sl = reason in ("SL", "Trail SL")

        if is_sl:
            self.consec_sl += 1
        else:
            self.consec_sl  = 0   # any non-SL exit resets streak

        # ── Direction fatigue tracking ────────────────────
        if pts_gained >= config.FATIGUE_MIN_PROFIT_PTS:
            dw = self._dir_wins[self.direction]
            dw["wins"]          += 1
            dw["last_win_time"]  = exit_time

        print(f"\n{'='*55}")
        print(f"  EXIT  #{self.trade_count} — {reason}")
        print(f"  {self.direction} {self.strike}  "
              f"{self.entry_time.strftime('%H:%M')}→{exit_time.strftime('%H:%M')} "
              f"({duration}m)")
        print(f"  Entry={self.entry_price:.2f}  Exit={exit_price:.2f}  "
              f"Peak={self.peak_price:.2f}")
        print(f"  P&L: {pts_gained:+.2f}pts = Rs{pnl_rs:+.0f}  "
              f"Cost={cost:.0f}  Net=Rs{net_rs:+.0f}")
        print(f"  Day P&L: Rs{self.day_pnl_rs:+,.0f}  ConsecSL={self.consec_sl}")

        self.telegram.alert_exit(
            direction=self.direction, strike=self.strike,
            entry_price=self.entry_price, exit_price=exit_price,
            pnl_pts=pts_gained, net_rs=net_rs, reason=reason,
        )

        self.report_mgr.log_trade({
            "entry_time"    : self.entry_time,
            "exit_time"     : exit_time,
            "direction"     : self.direction,
            "strike"        : self.strike,
            "expiry"        : self.opt_mgr.expiry_str,
            "atm_at_entry"  : "",
            "entry_price"   : self.entry_price,
            "exit_price"    : exit_price,
            "peak_price"    : self.peak_price,
            "entry_vwap"    : self.entry_vwap,
            "entry_dist"    : round(abs(self.entry_price - self.entry_vwap), 2),
            "nifty_at_entry": self.futures_engine.ltp,
            "nifty_at_exit" : self.futures_engine.ltp,
            "pnl_rs"        : pnl_rs,
            "total_cost"    : cost,
            "net_rs"        : net_rs,
            "exit_reason"   : reason,
            "exit_phase"    : reason,
            "target_points" : self.target_pts,
            "target_reason" : f"VIX={'HIGH' if self.high_vix else 'LOW'} {self.current_vix:.1f}",
            "breakeven_done": self.trail_active,
            "trail_active"  : self.trail_active,
        })

        if self.option_token:
            self._unsubscribe_option(self.option_token)
            self.option_token = None

    # ── Direction fatigue check ───────────────────────────

    def _is_direction_fatigued(self, direction: str, t: datetime.datetime) -> bool:
        """
        Returns True if this direction should be skipped due to fatigue.
        Fatigue: >= DIRECTION_FATIGUE_COUNT profitable exits in this direction.
        Resets after DIRECTION_COOLDOWN_MINS minutes since last win.
        """
        dw = self._dir_wins[direction]
        if dw["wins"] < config.DIRECTION_FATIGUE_COUNT:
            return False
        if dw["last_win_time"] is None:
            return False
        mins_since = (t - dw["last_win_time"]).total_seconds() / 60
        if mins_since >= config.DIRECTION_COOLDOWN_MINS:
            # Cooldown expired — reset wins
            dw["wins"]         = 0
            dw["last_win_time"] = None
            return False
        return True   # still in cooldown

    # ── Helpers ──────────────────────────────────────────

    def _is_market_hours(self, t: datetime.datetime) -> bool:
        open_t  = datetime.time(*map(int, config.MARKET_OPEN.split(":")))
        close_t = datetime.time(*map(int, config.SQUARE_OFF_TIME.split(":")))
        return open_t <= t.time() <= close_t

    def _on_reconnect(self, new_client):
        self.client = new_client
        self.opt_mgr.client = new_client
        self._setup_websocket()
        time.sleep(2)
        self._subscribe_futures()
        if self.option_token:
            self._subscribe_option(self.option_token)

    def _square_off_all(self):
        if self.in_trade:
            print(f"\n[SquareOff] 3:25 PM — closing {self.direction} {self.strike}")
            ltp = self.option_ltp if self.option_ltp > 0 else self.entry_price
            self._exit_trade(ltp, "Square-off 3:25 PM")

    def _end_of_day(self):
        print("\n" + "="*60 + "\n  END OF DAY")
        report = self.report_mgr.generate_daily_report()
        print(report)
        self.cap_mgr.print_status()
        self.report_mgr.close()
        self.telegram.alert_shutdown(
            trades=self.trade_count, net_pnl=self.day_pnl_rs)

    def _print_status(self):
        t     = now_ist()
        state = self.futures_engine.get_state()
        pos   = "▲" if state["was_above"] else "▼"
        print(f"\n[{t.strftime('%H:%M:%S')}] "
              f"F={state['ltp']:.2f} VWAP={state['vwap']:.2f}{pos} "
              f"VIX={self.current_vix:.1f} "
              f"Trades={self.trade_count} ConsecSL={self.consec_sl}", end="")
        if self.in_trade:
            unreal = round((self.option_ltp - self.entry_price) * self.qty, 0)
            trail_tag = "🔒" if self.trail_active else ""
            print(f" | {self.direction}{self.strike} "
                  f"E={self.entry_price:.0f} L={self.option_ltp:.0f} "
                  f"SL={self.sl_price:.0f} TGT={self.target_price:.0f} "
                  f"Unreal=Rs{unreal:+.0f}{trail_tag}", end="")
        print(f" | DayPnL=Rs{self.day_pnl_rs:+,.0f}")

    def _check_no_tick(self):
        t       = now_ist()
        elapsed = (t - self._last_tick_time).total_seconds()
        if elapsed > 300 and self._is_market_hours(t) and not self._circuit_alerted:
            msg = f"No tick for {elapsed/60:.0f} mins — circuit halt?"
            self.logger.warning(f"[Circuit] {msg}")
            self.telegram.alert_risk(msg)
            self._circuit_alerted = True

    def _handle_sigterm(self, signum, frame):
        print(f"\n[Shutdown] Signal {signum} received — stopping...")
        self._running = False

    def _graceful_shutdown(self):
        print("\n[Shutdown] Saving state and exiting...")
        try:
            self._square_off_all()
        except Exception as e:
            self.logger.error(f"[Shutdown] square_off error: {e}")
        try:
            self._end_of_day()
        except Exception as e:
            self.logger.error(f"[Shutdown] end_of_day error: {e}")
        try:
            if self.session_mgr:
                self.session_mgr.stop()
        except Exception:
            pass
        print("[Shutdown] Done.")
        import os as _os
        _os._exit(0)   # force exit — kills all threads including WS and pre-load

    # ── Main loop ─────────────────────────────────────────

    def run(self):
        self.initialize()
        self._subscribe_futures()

        print(f"[Main] WS started — entry from {config.ENTRY_START} IST")
        print(f"[Main] Square-off at {config.SQUARE_OFF_TIME} IST\n")

        sq_done         = False
        last_status_min = -1
        last_save_min   = -1

        print(f"[Main] Entry from 09:16 IST | Square-off at {config.SQUARE_OFF_TIME} IST")
        print(f"[Main] Auto-shutdown at {self._shutdown_time.strftime('%H:%M:%S')} IST\n")

        try:
            while self._running:
                t = now_ist()

                # ── Auto-shutdown: 5h 50m from start time ─────────
                if t >= self._shutdown_time:
                    print(f"\n[Main] Auto-shutdown time reached "
                          f"({self._shutdown_time.strftime('%H:%M:%S')}) — stopping")
                    break

                # ── Square-off at config time ──────────────────────
                sq_time = datetime.time(*map(int, config.SQUARE_OFF_TIME.split(":")))
                if t.time() >= sq_time and not sq_done:
                    self._square_off_all()
                    sq_done = True

                # ── Status print every minute ──────────────────────
                if t.minute != last_status_min:
                    self._print_status()
                    self._check_no_tick()
                    last_status_min = t.minute

                # ── Autosave every 30 min ─────────────────────────
                if t.minute % 30 == 0 and t.minute != last_save_min:
                    self.cap_mgr._save()
                    last_save_min = t.minute

                time.sleep(1)

        except KeyboardInterrupt:
            print("\n[Main] Keyboard interrupt — shutting down")
        finally:
            self._graceful_shutdown()


if __name__ == "__main__":
    algo = FuturesVWAPAlgo()
    algo.run()
