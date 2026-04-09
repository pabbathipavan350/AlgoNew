# ============================================================
# MAIN.PY — Nifty Futures VWAP Options Algo
# ============================================================
# Strategy:
#   - Subscribe to current-month Nifty futures token
#   - Track ap field (= session VWAP) on every tick
#   - Futures crosses ABOVE VWAP → buy ITM CE (delta≥0.85, OI≥12L)
#   - Futures crosses BELOW VWAP → buy ITM PE (delta≥0.85, OI≥12L)
#   - SL    : option entry price - 15 pts
#   - Target: option entry price + 45 pts
#   - Exit by 3:25 PM if still in trade
#   - Capital: Rs 5,00,000 | 5 lots fixed
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
from option_manager    import OptionManager, find_futures_token
from capital_manager   import CapitalManager
from report_manager    import ReportManager
from session_manager   import SessionManager
from telegram_notifier import TelegramNotifier


# ── IST helper ────────────────────────────────────────────

def now_ist() -> datetime.datetime:
    """Current time in IST (UTC+5:30). Works on Linux and Windows."""
    utc_now = datetime.datetime.utcnow()
    return utc_now + datetime.timedelta(hours=5, minutes=30)


# ── Logging setup ─────────────────────────────────────────

def setup_logging():
    os.makedirs("logs", exist_ok=True)
    log_file = f"logs/algo_{now_ist().strftime('%Y%m%d')}.log"
    fmt      = "%(asctime)s %(levelname)-8s %(name)s | %(message)s"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=20*1024*1024, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(fmt))
    root.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(fmt))
    root.addHandler(ch)

    return logging.getLogger("main")


# ── Algo class ────────────────────────────────────────────

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

        # Subscribed tokens
        self.futures_token  = None   # current-month Nifty futures
        self.option_token   = None   # currently held option (CE or PE)

        # Position state
        self.in_trade       = False
        self.direction      = None   # 'CE' or 'PE'
        self.entry_type     = None   # 'cross' or 'pullback'
        self.strike         = None
        self.entry_price    = 0.0
        self.entry_time     = None
        self.entry_vwap     = 0.0
        self.sl_price       = 0.0
        self.target_price   = 0.0
        self.peak_price     = 0.0
        self.qty            = config.LOTS * config.LOT_SIZE

        # Option tick tracking (for SL/target monitoring)
        self.option_ltp     = 0.0

        # Day P&L guard
        self.day_pnl_rs     = 0.0

        # No-tick circuit breaker
        self._last_tick_time = now_ist()
        self._circuit_alerted = False

        # Shutdown flag
        self._running       = True
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        signal.signal(signal.SIGINT,  self._handle_sigterm)

    # ── Startup ───────────────────────────────────────────

    def initialize(self):
        print("\n" + "="*60)
        print("  Nifty Futures VWAP Options Algo")
        mode = "*** PAPER TRADE ***" if config.PAPER_TRADE else "*** LIVE MODE ***"
        print(f"  Mode    : {mode}")
        print(f"  Capital : Rs {config.TOTAL_CAPITAL:,.0f}")
        print(f"  Lots    : {config.LOTS} × {config.LOT_SIZE} = {self.qty} qty")
        print(f"  SL      : {config.SL_PTS} pts below entry")
        print(f"  Target  : {config.TARGET_PTS} pts above entry")
        print("="*60)

        # Auth
        self.client = get_kotak_session()

        # Session keepalive
        self.session_mgr = SessionManager(self.client, get_kotak_session)
        self.session_mgr.on_reconnect = self._on_reconnect
        self.session_mgr.start()

        # Managers
        self.cap_mgr    = CapitalManager()
        self.opt_mgr    = OptionManager(self.client)
        self.report_mgr = ReportManager(self.cap_mgr)

        # Resolve futures token
        self._resolve_futures_token()

        # Setup WS
        self._setup_websocket()

        # Print expiry
        exp = self.opt_mgr.expiry_date
        print(f"\n[Init] Expiry: {exp.strftime('%d %b %Y')} "
              f"({(exp - datetime.date.today()).days}d)")
        print(f"[Init] Futures token: {self.futures_token}")

        self.telegram.alert_startup(
            mode    = "PAPER" if config.PAPER_TRADE else "LIVE",
            expiry  = str(exp),
            atm     = "—",
        )
        print("\n[Init] ✅ Initialisation complete — waiting for 9:15 AM\n")

    def _resolve_futures_token(self):
        """Find and store the current-month Nifty futures token."""
        expiry_str = self.opt_mgr.expiry_str
        print(f"[Init] Resolving futures token for expiry {expiry_str}...")
        self.futures_token = find_futures_token(self.client, expiry_str)
        if not self.futures_token:
            raise RuntimeError(
                f"Could not resolve Nifty futures token for {expiry_str}. "
                f"Check scrip master and expiry date."
            )

    # ── WebSocket setup ───────────────────────────────────

    def _setup_websocket(self):
        """
        Kotak Neo v2 SDK pattern:
          1. Assign callbacks as attributes on the client object.
          2. Call client.subscribe() — this starts the WS internally.
          DO NOT call connect() or ws_connect() — they do not exist in v2.
        """
        self.client.on_message = self._on_message
        self.client.on_error   = self._on_ws_error
        self.client.on_close   = self._on_ws_close
        self.client.on_open    = self._on_ws_open

    def _on_ws_open(self, *args):
        print("[WS] Connected — subscribing futures token")
        self._subscribe_futures()

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
                isIndex=False,
                isDepth=False,
            )
            print(f"[WS] Subscribed futures token={self.futures_token}")
        except Exception as e:
            self.logger.error(f"Subscribe futures error: {e}")

    def _subscribe_option(self, token: str):
        try:
            self.client.subscribe(
                instrument_tokens=[{"instrument_token": token,
                                    "exchange_segment": config.FO_SEGMENT}],
                isIndex=False,
                isDepth=False,
            )
            self.logger.info(f"[WS] Subscribed option token={token}")
        except Exception as e:
            self.logger.error(f"Subscribe option error: {e}")

    def _unsubscribe_option(self, token: str):
        try:
            self.client.unsubscribe(
                instrument_tokens=[{"instrument_token": token,
                                    "exchange_segment": config.FO_SEGMENT}],
                isIndex=False,
                isDepth=False,
            )
        except Exception:
            pass

    # ── Tick handler ──────────────────────────────────────

    def _on_message(self, message):
        try:
            if not isinstance(message, (dict, list)):
                return

            ticks = message if isinstance(message, list) else [message]

            for tick in ticks:
                token = str(tick.get("tk") or tick.get("token") or
                            tick.get("instrument_token") or "")
                self._last_tick_time  = now_ist()
                self._circuit_alerted = False

                if token == str(self.futures_token):
                    self._on_futures_tick(tick)
                elif token == str(self.option_token):
                    self._on_option_tick(tick)

        except Exception as e:
            self.logger.error(f"_on_message error: {e}", exc_info=True)

    def _on_futures_tick(self, tick: dict):
        t = now_ist()

        # Feed to VWAP engine
        self.futures_engine.on_tick(tick)

        state = self.futures_engine.get_state()
        ltp   = state["ltp"]
        vwap  = state["vwap"]

        # Only act during trading hours
        if not self._is_market_hours(t):
            return

        # Check for entry signal — directional filtering:
        # If a position is open in one direction, signals in the SAME direction
        # are dropped. Signals in the OPPOSITE direction are allowed through
        # (e.g. CE open → PE cross/pullback can still enter).
        sig, sig_type = self.futures_engine.check_signal()
        if sig:
            same_direction = self.in_trade and self.direction == sig
            if same_direction:
                # Already holding this direction — do not re-enter
                self.logger.debug(
                    f"[Guard] {sig} signal ({sig_type}) ignored — "
                    f"already in {self.direction} trade"
                )
            else:
                # Either no open trade, or opposite direction — allow entry
                self._on_signal(sig, sig_type, ltp, vwap, t)

    def _on_option_tick(self, tick: dict):
        ltp = float(tick.get("ltp") or tick.get("lp") or 0)
        if ltp <= 0:
            return

        self.option_ltp = ltp

        if ltp > self.peak_price:
            self.peak_price = ltp

        if not self.in_trade:
            return

        # ── Check target ──────────────────────────────────
        if ltp >= self.target_price:
            self.logger.info(f"[Exit] TARGET HIT ltp={ltp:.2f} target={self.target_price:.2f}")
            self._exit_trade(ltp, "Target")
            return

        # ── Check SL ──────────────────────────────────────
        if ltp <= self.sl_price:
            self.logger.info(f"[Exit] SL HIT ltp={ltp:.2f} sl={self.sl_price:.2f}")
            self._exit_trade(ltp, "SL")
            return

    # ── Signal handler ────────────────────────────────────

    def _on_signal(self, direction: str, sig_type: str,
                   futures_ltp: float, futures_vwap: float,
                   t: datetime.datetime):
        """Called when a futures VWAP cross or pullback signal is detected."""
        self.logger.info(f"[Signal] {direction} ({sig_type}) futures={futures_ltp:.2f} "
                         f"vwap={futures_vwap:.2f}")

        # ── Opposite-direction flip: close existing trade first ───────────
        # If we are in a CE trade and a PE signal fires (or vice versa),
        # exit the current position before entering the new one.
        if self.in_trade and self.direction != direction:
            print(f"\n[Flip] {self.direction} open — closing before entering {direction}")
            self.logger.info(f"[Flip] Closing {self.direction} to enter {direction}")
            self._exit_trade(self.option_ltp or self.entry_price, "Flip")

        # Day loss guard
        if self.day_pnl_rs <= config.MAX_DAILY_LOSS_RS:
            self.logger.warning(f"[Guard] Day loss limit hit ({self.day_pnl_rs:.0f}) — no entry")
            return

        # Square-off time guard
        sq_time = datetime.time(*map(int, config.SQUARE_OFF_TIME.split(":")))
        if t.time() >= sq_time:
            self.logger.info("[Guard] Past square-off time — no entry")
            return

        # Expiry day cutoff
        if datetime.date.today() == self.opt_mgr.expiry_date:
            cutoff = datetime.time(*map(int, config.EXPIRY_DAY_CUTOFF.split(":")))
            if t.time() >= cutoff:
                self.logger.info("[Guard] Expiry day cutoff — no entry")
                return

        print(f"\n{'='*50}")
        print(f"[Signal] {direction} {sig_type.upper()} at {t.strftime('%H:%M:%S')}")
        print(f"         Futures LTP={futures_ltp:.2f}  VWAP={futures_vwap:.2f}  "
              f"dist={futures_ltp - futures_vwap:+.2f}pts")

        # Pick strike
        print(f"[Strike] Selecting {direction} strike...")
        info = self.opt_mgr.pick_strike(futures_ltp, direction)
        if not info:
            self.logger.error(f"No valid strike found for {direction} — skipping")
            self.telegram.alert_risk(f"No valid {direction} strike found at {t.strftime('%H:%M')}")
            return

        # Get option LTP for order pricing
        # Fetch a quick quote to get current option LTP
        option_ltp = self._get_option_ltp(info["token"])
        if option_ltp <= 0:
            self.logger.error("Could not fetch option LTP — skipping")
            return

        print(f"[Strike] {direction} {info['strike']} | "
              f"delta={info['delta']:.2f} OI={info['oi']:,} "
              f"LTP={option_ltp:.2f}")

        # Place buy order
        fill = self.opt_mgr.place_buy_order(
            token     = info["token"],
            strike    = info["strike"],
            direction = direction,
            ltp       = option_ltp,
        )
        if not fill:
            self.logger.error("Buy order not filled — skipping")
            return

        fill_px = fill["fill_price"]

        # Set trade state
        self.in_trade     = True
        self.direction    = direction
        self.entry_type   = sig_type       # 'cross' or 'pullback'
        self.strike       = info["strike"]
        self.option_token = info["token"]
        self.entry_price  = fill_px
        self.entry_time   = t
        self.entry_vwap   = futures_vwap
        self.sl_price     = round(fill_px - config.SL_PTS, 2)
        self.target_price = round(fill_px + config.TARGET_PTS, 2)
        self.peak_price   = fill_px
        self.option_ltp   = fill_px

        # Subscribe option token for monitoring
        self._subscribe_option(info["token"])

        print(f"\n✅ ENTRY CONFIRMED")
        print(f"   Direction : {direction}  ({sig_type.upper()})")
        print(f"   Strike    : {info['strike']} (exp {info['expiry_str']})")
        print(f"   Entry     : Rs {fill_px:.2f}")
        print(f"   SL        : Rs {self.sl_price:.2f}  (−{config.SL_PTS} pts)")
        print(f"   Target    : Rs {self.target_price:.2f}  (+{config.TARGET_PTS} pts)")
        print(f"   Qty       : {self.qty}")
        print(f"   Futures   : {futures_ltp:.2f} | VWAP: {futures_vwap:.2f}  ")
        print(f"   Dist VWAP : {futures_ltp - futures_vwap:+.2f} pts")

        self.telegram.alert_entry(
            direction   = direction,
            strike      = info["strike"],
            entry_price = fill_px,
            vwap        = futures_vwap,
            sl          = self.sl_price,
            target      = self.target_price,
            qty         = self.qty,
        )

    # ── Exit handler ──────────────────────────────────────

    def _exit_trade(self, exit_ltp: float, reason: str):
        if not self.in_trade:
            return

        self.in_trade = False
        exit_time     = now_ist()

        # Place exit order (paper: use exit_ltp directly)
        actual_exit = self.opt_mgr.place_exit_order(
            token     = self.option_token,
            strike    = self.strike,
            direction = self.direction,
            qty       = self.qty,
            reason    = reason,
        )
        exit_price = actual_exit if actual_exit else exit_ltp

        # P&L
        pts_gained = round(exit_price - self.entry_price, 2)
        pnl_rs     = round(pts_gained * self.qty, 2)
        cost       = OptionManager.calc_trade_cost(
            self.entry_price, exit_price, self.qty
        )
        net_rs     = round(pnl_rs - cost, 2)

        self.day_pnl_rs += net_rs
        self.cap_mgr.update_after_trade(net_rs)

        # Duration
        duration = round((exit_time - self.entry_time).total_seconds() / 60, 1)

        print(f"\n{'='*50}")
        print(f"  EXIT — {reason}")
        print(f"  Direction : {self.direction} {self.strike}")
        print(f"  Entry     : Rs {self.entry_price:.2f}")
        print(f"  Exit      : Rs {exit_price:.2f}")
        print(f"  P&L       : {pts_gained:+.2f} pts = Rs {pnl_rs:+.0f}")
        print(f"  Cost      : Rs {cost:.2f}")
        print(f"  Net       : Rs {net_rs:+.0f}")
        print(f"  Duration  : {duration} mins")
        print(f"  Day P&L   : Rs {self.day_pnl_rs:+,.0f}")

        self.telegram.alert_exit(
            direction   = self.direction,
            strike      = self.strike,
            entry_price = self.entry_price,
            exit_price  = exit_price,
            pnl_pts     = pts_gained,
            net_rs      = net_rs,
            reason      = reason,
        )

        # Log trade
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
            "target_points" : config.TARGET_PTS,
            "target_reason" : "Fixed 45 pts",
            "breakeven_done": False,
            "trail_active"  : False,
        })

        # Unsubscribe option token
        if self.option_token:
            self._unsubscribe_option(self.option_token)
            self.option_token = None

    # ── Helpers ───────────────────────────────────────────

    def _get_option_ltp(self, token: str) -> float:
        """Fetch current LTP for an option token via quotes API."""
        try:
            resp = self.client.quotes(
                instrument_tokens=[{"instrument_token": token,
                                    "exchange_segment": config.FO_SEGMENT}],
                quote_type="ltp"
            )
            if isinstance(resp, list) and resp:
                ltp = float(resp[0].get("ltp") or resp[0].get("last_price") or 0)
                return ltp
        except Exception as e:
            self.logger.debug(f"get_option_ltp error: {e}")
        return 0.0

    def _is_market_hours(self, t: datetime.datetime) -> bool:
        open_t  = datetime.time(*map(int, config.MARKET_OPEN.split(":")))
        close_t = datetime.time(*map(int, config.SQUARE_OFF_TIME.split(":")))
        return open_t <= t.time() <= close_t

    def _on_reconnect(self, new_client):
        """Called by SessionManager after re-login.
        Kotak Neo v2: re-assign callbacks, then re-subscribe to restart WS."""
        self.client = new_client
        self.opt_mgr.client = new_client
        self._setup_websocket()   # re-assign callbacks on new client
        time.sleep(2)
        self._subscribe_futures()         # subscribe() restarts WS automatically
        if self.option_token:
            self._subscribe_option(self.option_token)
        self.logger.info("Re-subscribed after reconnect")

    # ── Square-off and end-of-day ─────────────────────────

    def _square_off_all(self):
        """Force exit any open position at 3:25 PM."""
        if self.in_trade:
            print(f"\n[SquareOff] 3:25 PM — closing {self.direction} {self.strike}")
            ltp = self.option_ltp if self.option_ltp > 0 else self.entry_price
            self._exit_trade(ltp, "Square-off 3:25 PM")

    def _end_of_day(self):
        """Generate report and save state."""
        print("\n" + "="*60)
        print("  END OF DAY")
        report = self.report_mgr.generate_daily_report()
        print(report)
        self.cap_mgr.print_status()
        self.report_mgr.close()
        self.telegram.alert_shutdown(
            trades  = len(self.report_mgr.trades),
            net_pnl = self.day_pnl_rs,
        )

    # ── Status print ──────────────────────────────────────

    def _print_status(self):
        t     = now_ist()
        state = self.futures_engine.get_state()
        pos   = "ABOVE" if state["was_above"] else "BELOW"
        print(f"\n[{t.strftime('%H:%M:%S')}] "
              f"Futures={state['ltp']:.2f} VWAP={state['vwap']:.2f} ({pos}) "
              f"ticks={state['ticks']}", end="")
        if self.in_trade:
            unrealised = round((self.option_ltp - self.entry_price) * self.qty, 0)
            print(f" | IN TRADE {self.direction} {self.strike} "
                  f"entry={self.entry_price:.2f} ltp={self.option_ltp:.2f} "
                  f"SL={self.sl_price:.2f} TGT={self.target_price:.2f} "
                  f"unreal=Rs{unrealised:+.0f}", end="")
        print(f" | DayPnL=Rs{self.day_pnl_rs:+,.0f}")

    # ── No-tick circuit breaker ───────────────────────────

    def _check_no_tick(self):
        t       = now_ist()
        elapsed = (t - self._last_tick_time).total_seconds()
        if elapsed > 300 and self._is_market_hours(t) and not self._circuit_alerted:
            msg = f"No tick for {elapsed/60:.0f} mins — possible circuit/halt"
            self.logger.warning(f"[Circuit] {msg}")
            self.telegram.alert_risk(msg)
            self._circuit_alerted = True

    # ── Shutdown handler ──────────────────────────────────

    def _handle_sigterm(self, signum, frame):
        print(f"\n[Shutdown] Signal {signum} received — graceful shutdown")
        self._running = False

    def _graceful_shutdown(self):
        print("\n[Shutdown] Saving state...")
        self._square_off_all()
        self._end_of_day()
        if self.session_mgr:
            self.session_mgr.stop()

    # ── Main loop ─────────────────────────────────────────

    def run(self):
        self.initialize()

        # Kotak Neo v2: calling subscribe() starts the WS automatically.
        # Assign callbacks first (done in _setup_websocket during initialize),
        # then call subscribe() — the SDK opens the socket and delivers ticks.
        self._subscribe_futures()
        print("\n[Main] WebSocket started — waiting for ticks...")
        print(f"[Main] Entry enabled from {config.ENTRY_START} IST")
        print(f"[Main] Square-off at {config.SQUARE_OFF_TIME} IST\n")

        sq_done         = False
        last_status_min = -1
        last_save_min   = -1

        try:
            while self._running:
                t = now_ist()

                # Square-off at 3:25 PM
                sq_time = datetime.time(*map(int, config.SQUARE_OFF_TIME.split(":")))
                if t.time() >= sq_time and not sq_done:
                    self._square_off_all()
                    sq_done = True

                # End of day at 3:32 PM
                eod_time = datetime.time(15, 32)
                if t.time() >= eod_time:
                    break

                # Status print every minute
                if t.minute != last_status_min:
                    self._print_status()
                    self._check_no_tick()
                    last_status_min = t.minute

                # Autosave capital every 30 minutes
                if t.minute % 30 == 0 and t.minute != last_save_min:
                    self.cap_mgr._save()
                    last_save_min = t.minute

                time.sleep(1)

        except KeyboardInterrupt:
            print("\n[Main] Keyboard interrupt")

        finally:
            self._graceful_shutdown()


# ── Entry point ───────────────────────────────────────────

if __name__ == "__main__":
    algo = FuturesVWAPAlgo()
    algo.run()
