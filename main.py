# ============================================================
# MAIN.PY — Nifty Options VWAP Algo
# ============================================================
# VWAP: Kotak WS 'ap' field = session VWAP since 9:15 AM.
#   No calculation. No REST sync. Direct from exchange.

# Force IST (UTC+5:30) — required when running on GitHub Actions
# or any server outside India. Must be set before datetime imports.
import os
os.environ["TZ"] = "Asia/Kolkata"
try:
    import time as _time
    _time.tzset()   # applies TZ on Linux/Mac (GitHub runners are Linux)
except AttributeError:
    pass  # Windows: TZ env var alone is sufficient
# Strikes: CE = ATM-200, PE = ATM+200 (exact mirror pair).
#   Subscribe only 2 tokens from the start.
#   Refresh hourly if ATM shifts >= 50pts.
# Entry: 0-5pts above VWAP, mirror opposite below VWAP.
# SL: VWAP - VWAP_SL_BUFFER (dynamic, tracks rising VWAP).
# Square off: 3:25 PM.
# ============================================================

import datetime
import logging
import threading
import signal
import time
import os

import config
from auth              import get_kotak_session
from vwap_engine       import StrategyEngine
from option_manager    import OptionManager, get_next_expiry
from capital_manager   import CapitalManager
from session_manager   import SessionManager
from telegram_notifier import TelegramNotifier
from report_manager    import ReportManager

# ── Logging ───────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
log_file = f"logs/algo_{datetime.date.today().strftime('%Y%m%d')}.log"
logging.basicConfig(
    level    = logging.DEBUG,
    format   = "%(asctime)s,%(msecs)03d [%(levelname)s] %(message)s",
    datefmt  = "%Y-%m-%d %H:%M:%S",
    handlers = [
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
logging.getLogger().handlers[1].setLevel(logging.WARNING)

STRIKE_STEP = 50
ITM_DEPTH   = 200


def _calc_trade_cost(entry_price: float, exit_price: float, lot_size: int) -> float:
    today      = datetime.date.today()
    stt_rate   = 0.0015 if today >= datetime.date(2026, 4, 1) else 0.001
    sell_value = exit_price  * lot_size
    buy_value  = entry_price * lot_size
    total_prem = sell_value + buy_value
    stt        = round(sell_value * stt_rate,    2)
    txn_charge = round(total_prem * 0.00035,     2)
    stamp      = round(buy_value  * 0.00003,     2)
    brokerage  = 40.0
    gst        = round((brokerage + txn_charge) * 0.18, 2)
    return round(stt + txn_charge + stamp + brokerage + gst, 2)


class NiftyOptionsAlgo:

    def __init__(self):
        self.client     = None
        self.eng        = StrategyEngine()
        self.opt_mgr    = None
        self.cap_mgr    = CapitalManager()
        self.sess_mgr   = None
        self._lock      = threading.Lock()
        self.is_running = True

        self.expiry_date = None
        self.ce_tokens   = {}
        self.pe_tokens   = {}
        self.ce_symbols  = {}
        self.pe_symbols  = {}
        self.all_tokens  = {}

        self.active_trade = None
        self.trades_today = []
        self.telegram     = TelegramNotifier()
        self.report_mgr   = None
        self.today_vix    = 15.0
        self.prev_close   = 0.0
        self.gap_pct      = 0.0

        self.ws_connected      = False
        self.last_tick_time    = None
        self._reconnect_count  = 0
        self._reconnect_active = False
        self.subscribed        = False

        self._last_autosave     = None
        self._autosave_mins     = 30
        self._preopen_refreshed = False

    # ── SIGTERM handler ────────────────────────────────────
    def _on_sigterm(self, signum, frame):
        print("\n[SIGTERM] Saving and exiting gracefully...")
        logger.warning("SIGTERM received — graceful shutdown")
        self.is_running = False
        self._graceful_shutdown(reason="SIGTERM")

    def _graceful_shutdown(self, reason="shutdown"):
        try:
            if self.sess_mgr:
                self.sess_mgr.stop()
        except Exception:
            pass
        print(f"\n[*] Saving data ({reason})...")
        try:
            self._autosave_capital()
        except Exception as e:
            logger.error(f"Capital save: {e}")
        try:
            self.save_daily_report()
        except Exception as e:
            logger.error(f"Report save: {e}")
        try:
            if self.report_mgr:
                self.report_mgr.close()
        except Exception:
            pass
        try:
            net_today = sum(t['net_rs'] for t in self.trades_today)
            self.telegram.alert_shutdown(len(self.trades_today), net_today)
        except Exception:
            pass
        print("[*] Done. Goodbye.")

    def _autosave_capital(self):
        try:
            self.cap_mgr._save()
        except Exception as e:
            logger.debug(f"Autosave: {e}")

    # ── Initialize ────────────────────────────────────────
    def initialize(self):
        print("\n" + "="*62)
        print("  NIFTY OPTIONS ALGO — VWAP Mirror Pair Strategy")
        print("="*62)
        mode = "PAPER TRADE" if config.PAPER_TRADE else "*** LIVE — REAL MONEY ***"
        print(f"  Mode       : {mode}")
        print(f"  Strikes    : CE=ATM-200, PE=ATM+200 (exact mirror pair)")
        print(f"  VWAP source: Kotak WS ap field (no calculation)")
        print(f"  Entry zone : 0-{config.ENTRY_ZONE_PTS}pts above VWAP + mirror below")
        print(f"  SL         : VWAP - {config.VWAP_SL_BUFFER}pts (dynamic)")
        print(f"  Breakeven  : +{config.BREAKEVEN_TRIGGER}pts | Trail: +{config.TRAIL_TRIGGER_PTS}pts")
        print(f"  Square off : 3:25 PM")
        print("="*62)

        signal.signal(signal.SIGTERM, self._on_sigterm)

        self.cap_mgr.print_status()

        os.makedirs("reports", exist_ok=True)
        self.report_mgr = ReportManager(self.cap_mgr)

        print("\n[*] Logging into Kotak Neo...")
        self.client   = get_kotak_session()
        self.setup_websocket()

        self.opt_mgr  = OptionManager(self.client)
        self.sess_mgr = SessionManager(self.client, get_kotak_session)
        self.sess_mgr.on_reconnect = self._on_session_reconnect
        self.sess_mgr.start()

        self.expiry_date = get_next_expiry()
        print(f"[*] Weekly expiry  : {self.expiry_date}")

        self._recover_open_position()
        self._fetch_vix()
        self._fetch_prev_close()

        if self.report_mgr:
            self.report_mgr.set_vix(self.today_vix)

        target_pts, target_reason = self._get_dynamic_target_points()
        print(f"[*] Profit target  : {target_pts:.1f} pts ({target_reason})")

        spot = self._get_nifty_spot()
        if spot <= 0:
            spot = 23000
        self._setup_strikes(spot)

        if self.prev_close > 0:
            self.gap_pct = abs(spot - self.prev_close) / self.prev_close * 100
            print(f"[*] Gap            : {self.gap_pct:.1f}% (info only)")

        print(f"\n[*] Waiting for 9:15 AM...")
        mode_str = "PAPER" if config.PAPER_TRADE else "LIVE"
        self.telegram.alert_startup(mode_str, self.expiry_date,
                                    getattr(self.eng, "atm_strike", 0))

    def _fetch_vix(self):
        try:
            for name in ["INDIA VIX", "India VIX"]:
                resp = self.client.quotes(
                    instrument_tokens=[{"instrument_token": name,
                                        "exchange_segment": config.CM_SEGMENT}],
                    quote_type="ltp")
                data = resp if isinstance(resp, list) else (
                       resp.get('message') or resp.get('data') or [])
                if data:
                    v = float(data[0].get('ltp') or 0)
                    if v > 0:
                        self.today_vix = v
                        print(f"[*] India VIX      : {v:.2f}")
                        return
        except Exception as e:
            logger.debug(f"VIX fetch: {e}")
        print(f"[*] India VIX      : {self.today_vix:.1f} (default)")

    def _fetch_prev_close(self):
        try:
            resp = self.client.quotes(
                instrument_tokens=[{"instrument_token": "Nifty 50",
                                    "exchange_segment": config.CM_SEGMENT}],
                quote_type="ohlc")
            data = resp if isinstance(resp, list) else (
                   resp.get('message') or resp.get('data') or [])
            if data:
                ohlc = data[0].get('ohlc') or {}
                self.prev_close = float(ohlc.get('close') or 0)
                if self.prev_close > 0:
                    print(f"[*] Nifty prev close: {self.prev_close:.2f}")
        except Exception as e:
            logger.debug(f"Prev close: {e}")

    def _get_nifty_spot(self):
        try:
            resp = self.client.quotes(
                instrument_tokens=[{"instrument_token": "Nifty 50",
                                    "exchange_segment": config.CM_SEGMENT}],
                quote_type="ltp")
            data = resp if isinstance(resp, list) else (
                   resp.get('message') or resp.get('data') or [])
            if data:
                return float(data[0].get('ltp') or 0)
        except Exception as e:
            logger.debug(f"Nifty spot: {e}")
        return 0.0

    def _setup_strikes(self, spot, force=False):
        now = datetime.datetime.now()
        if (not force and now.time() >= datetime.time(10, 0) and self.subscribed):
            return

        atm       = round(spot / STRIKE_STEP) * STRIKE_STEP
        ce_strike = atm - ITM_DEPTH
        pe_strike = atm + ITM_DEPTH

        print(f"\n[*] Mirror pair | Nifty={spot:.0f} ATM={atm}")
        print(f"    CE={ce_strike} (200pts ITM) | PE={pe_strike} (200pts ITM)")

        ce_tokens  = {}
        pe_tokens  = {}
        ce_symbols = {}
        pe_symbols = {}

        tok = self.opt_mgr.get_option_token(ce_strike, 'CE', self.expiry_date)
        if tok:
            ce_tokens[ce_strike]  = tok
            ce_symbols[ce_strike] = self.opt_mgr.get_trading_symbol(
                ce_strike, 'CE', self.expiry_date)
        else:
            logger.error(f"CE token not found for {ce_strike}")

        tok = self.opt_mgr.get_option_token(pe_strike, 'PE', self.expiry_date)
        if tok:
            pe_tokens[pe_strike]  = tok
            pe_symbols[pe_strike] = self.opt_mgr.get_trading_symbol(
                pe_strike, 'PE', self.expiry_date)
        else:
            logger.error(f"PE token not found for {pe_strike}")

        self.ce_tokens  = ce_tokens
        self.pe_tokens  = pe_tokens
        self.ce_symbols = ce_symbols
        self.pe_symbols = pe_symbols
        self.all_tokens = {}
        for strike, tok in ce_tokens.items():
            self.all_tokens[str(tok)] = (strike, 'CE')
        for strike, tok in pe_tokens.items():
            self.all_tokens[str(tok)] = (strike, 'PE')

        self.eng.setup_strikes(spot, ce_tokens, pe_tokens)
        self.eng.update_atm(spot)
        print(f"    Tokens: CE={ce_tokens} | PE={pe_tokens}")

    def _start_hourly_strike_refresh(self):
        def _loop():
            while self.is_running:
                time.sleep(3600)
                if not self.is_running:
                    break
                now = datetime.datetime.now()
                t   = now.time()
                if t < datetime.time(10, 0) or t >= datetime.time(15, 0):
                    continue
                if self.active_trade:
                    logger.debug("Hourly refresh skipped — trade active")
                    continue
                try:
                    spot = self._get_nifty_spot()
                    if spot <= 0:
                        continue
                    new_atm = round(spot / STRIKE_STEP) * STRIKE_STEP
                    old_atm = self.eng.atm_strike
                    if abs(new_atm - old_atm) < STRIKE_STEP:
                        logger.debug(f"Hourly: ATM unchanged ({old_atm})")
                        continue
                    print(f"  [StrikeRefresh] ATM {old_atm}->>{new_atm} (Nifty={spot:.0f})")
                    try:
                        drop = [{"instrument_token": tok,
                                 "exchange_segment": config.FO_SEGMENT}
                                for tok in self.all_tokens.keys()]
                        if drop:
                            self.client.unsubscribe(instrument_tokens=drop,
                                                    isIndex=False, isDepth=False)
                    except Exception as e:
                        logger.debug(f"Unsubscribe: {e}")
                    self._setup_strikes(spot, force=True)
                    self.subscribe_options()
                    print(f"  [StrikeRefresh] CE={new_atm-200} PE={new_atm+200} OK")
                    self.telegram.alert_session(
                        f"Strikes refreshed ATM {old_atm}->{new_atm}")
                except Exception as e:
                    logger.error(f"Hourly refresh error: {e}")
        threading.Thread(target=_loop, daemon=True, name="HourlyStrikeRefresh").start()

    # ── WebSocket ─────────────────────────────────────────
    def _ws_on_message(self, message):
        if not self.is_running:
            return
        try:
            if not isinstance(message, dict):
                return
            if message.get('type', '') not in ['stock_feed', 'sf', 'index_feed', 'if']:
                return

            now   = datetime.datetime.now()
            ticks = message.get('data', [])

            for tick in ticks:
                token = str(tick.get('tk', '') or tick.get('token', '') or '')
                ltp   = float(tick.get('ltp', 0) or tick.get('ltP', 0) or 0)

                # 'ap' = Sigma(price*qty)/Sigma(qty) since 9:15 AM = session VWAP
                # This is the ONLY VWAP source. No REST sync. No candle math.
                ap = float(tick.get('ap', 0) or 0)

                if ltp <= 0:
                    continue

                self.last_tick_time = now

                with self._lock:
                    if token not in self.all_tokens:
                        continue
                    self.eng.add_tick(token, ltp)
                    if ap > 0:
                        self.eng.set_vwap_direct(token, ap)

                # Entry/exit checks run outside lock so ticks keep flowing
                self._on_tick(now)

        except Exception as e:
            logger.debug(f"on_message: {e}")

    def _ws_on_open(self, msg):
        logger.info("WS connected")
        self.ws_connected     = True
        self._reconnect_count = 0
        print(f"  [WS] Connected ({datetime.datetime.now().strftime('%H:%M:%S')})")

    def _ws_on_error(self, error):
        logger.error(f"WS error: {error}")
        self.ws_connected = False

    def _ws_on_close(self, msg):
        logger.warning(f"WS closed: {msg}")
        self.ws_connected = False
        if datetime.datetime.now().time() < datetime.time(15, 25):
            print("\n  [WS] Disconnected — reconnecting...")
            self._trigger_reconnect()

    def setup_websocket(self):
        self.client.on_message = self._ws_on_message
        self.client.on_error   = self._ws_on_error
        self.client.on_close   = self._ws_on_close
        self.client.on_open    = self._ws_on_open

    def subscribe_options(self):
        if not self.all_tokens:
            print("  [WS] No tokens to subscribe")
            return
        try:
            tok_list = [{"instrument_token": str(tok),
                         "exchange_segment": config.FO_SEGMENT}
                        for tok in self.all_tokens.keys()]
            resp = self.client.quotes(instrument_tokens=tok_list, quote_type="ltp")
            data = resp if isinstance(resp, list) else (
                   resp.get('message') or resp.get('data') or [])
            seeded = 0
            if data:
                for item in data:
                    tok = str(item.get('instrument_token') or item.get('tk') or '')
                    ltp = float(item.get('ltp') or 0)
                    if tok in self.all_tokens and ltp > 0:
                        self.eng.seed_ltp(tok, ltp)
                        seeded += 1
                if seeded == 0 and len(data) == len(tok_list):
                    for i, (tok, _) in enumerate(self.all_tokens.items()):
                        ltp = float(data[i].get('ltp') or 0)
                        if ltp > 0:
                            self.eng.seed_ltp(tok, ltp)
                            seeded += 1
            print(f"  Seeded {seeded}/{len(self.all_tokens)} LTPs")
        except Exception as e:
            logger.debug(f"LTP seed: {e}")
        try:
            tokens = [{"instrument_token": str(tok),
                       "exchange_segment": config.FO_SEGMENT}
                      for tok in self.all_tokens.keys()]
            self.client.subscribe(instrument_tokens=tokens,
                                  isIndex=False, isDepth=False)
            self.subscribed = True
            ce_s = list(self.ce_tokens.keys())[0] if self.ce_tokens else '?'
            pe_s = list(self.pe_tokens.keys())[0] if self.pe_tokens else '?'
            print(f"  Subscribed 2 tokens: CE={ce_s} | PE={pe_s} OK")
        except Exception as e:
            logger.error(f"Subscribe error: {e}")

    def _trigger_reconnect(self):
        if self._reconnect_active:
            return
        if datetime.datetime.now().time() >= datetime.time(15, 25):
            return
        threading.Timer(5.0, self._do_reconnect).start()

    def _do_reconnect(self):
        if self._reconnect_active:
            return
        self._reconnect_active = True
        try:
            self._reconnect_count += 1
            if self._reconnect_count > 20:
                print("  [WS] Max reconnects reached")
                return
            wait = min(5 * (2 ** min(self._reconnect_count - 1, 3)), 60)
            print(f"  [WS] Reconnect {self._reconnect_count}/20 (wait {wait}s)...")
            time.sleep(wait)
            self.setup_websocket()
            time.sleep(2)
            self.subscribe_options()
            time.sleep(3)
            if self.ws_connected:
                print("  [WS] Reconnected OK")
        except Exception as e:
            logger.error(f"Reconnect: {e}")
        finally:
            self._reconnect_active = False

    # ── Core tick handler ──────────────────────────────────
    def _on_tick(self, now: datetime.datetime):
        t = now.time()
        if t < datetime.time(9, 15) or t >= datetime.time(15, 25):
            return
        circuit_suspected = (
            self.last_tick_time is not None and
            (now - self.last_tick_time).total_seconds() > config.NO_TICK_CIRCUIT_SECS
        )
        if self.active_trade:
            self._check_exit(now)
        elif not circuit_suspected:
            self._check_entry(now)
        elif not getattr(self, '_circuit_alerted', False):
            self._circuit_alerted = True
            self.telegram.alert_risk(
                f"No ticks for {config.NO_TICK_CIRCUIT_SECS}s — circuit suspected")
        else:
            self._circuit_alerted = False

    # ── Dynamic target ─────────────────────────────────────
    def _days_to_expiry(self):
        if not self.expiry_date:
            return 99
        return (self.expiry_date - datetime.date.today()).days

    def _get_dynamic_target_points(self):
        d = self._days_to_expiry()
        if d <= 0:
            return config.TARGET_EXPIRY_DAY_PTS, "expiry day"
        if d == 1:
            return config.TARGET_NEAR_EXPIRY_PTS, "near expiry"
        if self.today_vix >= config.VIX_HIGH_THRESHOLD:
            return config.TARGET_HIGH_VIX_PTS, f"high VIX ({self.today_vix:.2f})"
        if self.today_vix >= config.VIX_MEDIUM_THRESHOLD:
            return config.TARGET_MEDIUM_VIX_PTS, f"medium VIX ({self.today_vix:.2f})"
        return config.TARGET_LOW_VIX_PTS, f"low VIX ({self.today_vix:.2f})"

    # ── Entry ──────────────────────────────────────────────
    def _check_entry(self, now: datetime.datetime):
        signal, token, vwap_obj = self.eng.check_entry(now)
        if not signal:
            return
        ref_price = vwap_obj.ltp
        if ref_price <= 0:
            logger.debug("Entry: LTP unavailable")
            return

        strike = vwap_obj.strike
        symbol = (self.ce_symbols.get(strike, f"NIFTY_CE_{strike}") if signal == 'CE'
                  else self.pe_symbols.get(strike, f"NIFTY_PE_{strike}"))

        target_pts, target_reason = self._get_dynamic_target_points()
        entry_exec = self._execute_managed_order(
            side='BUY', token=token, symbol=symbol, ref_price=ref_price,
            quantity=config.LOT_SIZE, timeout_secs=config.ORDER_FILL_TIMEOUT_SECS,
            chase_remaining=False)

        filled_qty = int(entry_exec.get('filled_qty') or 0)
        if filled_qty <= 0:
            status = entry_exec.get('status') or 'unknown'
            rej    = entry_exec.get('rej_reason') or ''
            print(f"  [Entry] {symbol} not filled (status={status})"
                  + (f" | {rej}" if rej else ""))
            logger.warning(f"Entry not filled: {symbol} {status} {rej}")
            return

        entry_price = float(entry_exec.get('avg_price') or ref_price)
        self.eng.on_entry(signal, token, entry_price, vwap_obj, now,
                          target_points=target_pts)

        self.active_trade = {
            'direction'      : signal,
            'token'          : token,
            'strike'         : strike,
            'symbol'         : symbol,
            'entry_price'    : entry_price,
            'entry_time'     : now,
            'entry_vwap'     : vwap_obj.get_vwap(),
            'entry_dist'     : vwap_obj.dist_above_vwap(),
            'atm_at_entry'   : self.eng.atm_strike,
            'nifty_at_entry' : self._get_nifty_spot(),
            'sl_price'       : self.eng.sl_price,
            'target_points'  : target_pts,
            'target_price'   : self.eng.target_price,
            'target_reason'  : target_reason,
            'order_id'       : entry_exec.get('order_id'),
            'filled_qty'     : filled_qty,
            'peak_price'     : entry_price,
        }

        ts      = now.strftime('%H:%M:%S')
        itm_pts = abs(self.eng.atm_strike - strike)
        session = 'EARLY' if now.time() < datetime.time(9, 40) else 'Normal'
        print(f"\n  {'='*55}")
        print(f"  {'PAPER' if config.PAPER_TRADE else 'LIVE'} TRADE: BUY {signal}  [{ts}]")
        print(f"     Symbol : {symbol}")
        print(f"     Strike : {strike} | ATM={self.eng.atm_strike} | ITM={itm_pts}pts")
        print(f"     Entry  : Rs {entry_price:.2f}")
        print(f"     VWAP   : Rs {vwap_obj.get_vwap():.2f}  "
              f"(dist={vwap_obj.dist_above_vwap():.1f}pts above)")
        print(f"     SL     : Rs {self.eng.sl_price:.2f}")
        print(f"     Target : Rs {self.eng.target_price:.2f} "
              f"(+{target_pts:.1f}pts | {target_reason})")
        print(f"     Qty    : {filled_qty} | Session: {session}")
        print(f"  {'='*55}\n")
        self.telegram.alert_entry(signal, strike, entry_price,
                                  vwap_obj.get_vwap(), self.eng.sl_price,
                                  self.eng.target_price, filled_qty)

    # ── Exit ───────────────────────────────────────────────
    def _check_exit(self, now: datetime.datetime):
        if self.active_trade and self.eng.active_token:
            cur_ltp = self.eng.get_ltp(self.eng.active_token)
            if cur_ltp > 0:
                self.active_trade['peak_price'] = max(
                    self.active_trade.get('peak_price', 0), cur_ltp)

        should_exit, reason = self.eng.check_exit(now)
        if not should_exit:
            return
        flip = reason and 'flip' in reason

        trade     = self.active_trade
        ref_price = self.eng.get_ltp(trade['token'])
        if ref_price <= 0:
            return

        qty_to_sell = int(trade.get('filled_qty') or config.LOT_SIZE)
        exit_exec = self._execute_managed_order(
            side='SELL', token=trade['token'], symbol=trade['symbol'],
            ref_price=ref_price, quantity=qty_to_sell,
            timeout_secs=config.EXIT_FILL_TIMEOUT_SECS, chase_remaining=True)

        sold_qty      = int(exit_exec.get('filled_qty') or 0)
        remaining_qty = max(qty_to_sell - sold_qty, 0)

        if sold_qty <= 0:
            status = exit_exec.get('status') or 'unknown'
            rej    = exit_exec.get('rej_reason') or ''
            print(f"  [Exit] No qty sold {trade['symbol']} (status={status})"
                  + (f" | {rej}" if rej else "") + " — keeping watch")
            logger.warning(f"Exit not executed: {trade['symbol']} {status} {rej}")
            return

        if remaining_qty > 0:
            trade['filled_qty'] = remaining_qty
            print(f"  [Exit] {sold_qty} sold, {remaining_qty} still open — watching")
            return

        exit_price = float(exit_exec.get('avg_price') or ref_price)
        qty        = qty_to_sell
        pnl_pts    = exit_price - trade['entry_price']
        pnl_rs     = pnl_pts * qty
        total_cost = _calc_trade_cost(trade['entry_price'], exit_price, qty)
        net_rs     = round(pnl_rs - total_cost, 2)
        won        = net_rs > 0
        nifty_exit = self._get_nifty_spot()

        exit_phase = ('Trail SL'     if self.eng.trail_active   else
                      'Breakeven SL' if self.eng.breakeven_done else
                      'Initial SL'   if 'SL hit'  in reason    else
                      'Target'       if 'Target'  in reason    else
                      'Square-off'   if 'Square'  in reason    else
                      'Flip'         if 'flip'    in reason    else 'Other')

        completed = {
            **trade,
            'exit_price'     : exit_price,
            'exit_time'      : now,
            'exit_reason'    : reason,
            'exit_phase'     : exit_phase,
            'exit_order_id'  : exit_exec.get('order_id'),
            'nifty_at_exit'  : nifty_exit,
            'sold_qty'       : sold_qty,
            'pnl_pts'        : round(pnl_pts, 2),
            'pnl_rs'         : round(pnl_rs, 2),
            'total_cost'     : total_cost,
            'net_rs'         : net_rs,
            'won'            : won,
            'breakeven_done' : self.eng.breakeven_done,
            'trail_active'   : self.eng.trail_active,
        }

        self.cap_mgr.update_after_trade(net_rs)
        self.trades_today.append(completed)

        if self.report_mgr:
            try:
                self.report_mgr.log_trade(completed)
            except Exception as e:
                logger.error(f"Journal log: {e}")

        peak      = trade.get('peak_price', exit_price)
        max_pts   = peak - trade['entry_price']
        captured  = round(pnl_pts / max_pts * 100, 0) if max_pts > 0 else 0
        print(f"\n  {'='*55}")
        print(f"  {'WIN' if won else 'LOSS'} | {reason.split('|')[0].strip()}")
        print(f"     Entry  : Rs {trade['entry_price']:.2f} @ "
              f"{trade['entry_time'].strftime('%H:%M:%S')}")
        print(f"     Exit   : Rs {exit_price:.2f} @ {now.strftime('%H:%M:%S')}")
        print(f"     Peak   : Rs {peak:.2f}  |  Captured: {captured:.0f}% of move")
        print(f"     Qty    : {qty}")
        print(f"     P&L    : {pnl_pts:+.2f} pts = Rs {pnl_rs:+.0f}")
        print(f"     Costs  : Rs {total_cost:.0f}")
        print(f"     Net    : Rs {net_rs:+.0f}")
        print(f"  {'='*55}\n")
        self.telegram.alert_exit(trade['direction'], trade['strike'],
                                 trade['entry_price'], exit_price,
                                 pnl_pts, net_rs, reason)

        self.eng.on_exit()
        self.active_trade = None

        if flip:
            prev_dir = trade['direction']
            new_dir  = 'PE' if prev_dir == 'CE' else 'CE'
            print(f"  [FLIP] {prev_dir} broke VWAP — checking {new_dir} entry...")
            self.eng.last_signal_time = None
            self._check_entry(now)
            if self.active_trade:
                print(f"  [FLIP] Entered {self.active_trade['direction']} "
                      f"{self.active_trade['strike']}")
            else:
                print(f"  [FLIP] No valid {new_dir} entry — staying out")

    # ── Order execution ────────────────────────────────────
    def _recover_open_position(self):
        if config.PAPER_TRADE:
            return
        try:
            resp = self.client.positions()
            rows = resp if isinstance(resp, list) else (
                   resp.get('data') or resp.get('message') or [])
            for row in rows:
                sym     = str(row.get('trdSym') or row.get('trading_symbol') or '')
                net_qty = int(float(row.get('netQty') or row.get('net_quantity') or 0))
                avg_prc = float(row.get('avgPrc') or row.get('average_price') or 0)
                if net_qty == 0 or not sym:
                    continue
                direction = ('CE' if sym.endswith('CE') else
                             'PE' if sym.endswith('PE') else None)
                if not direction:
                    continue
                token = None
                for tok, (strike, opt_type) in self.all_tokens.items():
                    chk = (self.ce_symbols.get(strike) if direction == 'CE'
                           else self.pe_symbols.get(strike))
                    if chk == sym:
                        token = tok
                        break
                if not token:
                    print(f"  [Recovery] OPEN POSITION {sym} qty={net_qty} "
                          f"— TOKEN NOT FOUND — MANUAL EXIT NEEDED")
                    continue
                vwap_obj = (self.eng.ce_strikes if direction == 'CE'
                            else self.eng.pe_strikes).get(token)
                vwap_val = vwap_obj.get_vwap() if vwap_obj else 0.0
                sl_price = round((vwap_val - config.VWAP_SL_BUFFER)
                                 if vwap_val > 0 else (avg_prc - 10.0), 2)
                self.eng.in_trade      = True
                self.eng.direction     = direction
                self.eng.active_token  = token
                self.eng.active_strike = next(
                    (s for t, (s, o) in self.all_tokens.items() if t == token), 0)
                self.eng.entry_price   = avg_prc
                self.eng.entry_vwap    = vwap_val
                self.eng.sl_price      = sl_price
                self.eng.best_price    = avg_prc
                self.active_trade = {
                    'direction'     : direction,
                    'token'         : token,
                    'strike'        : self.eng.active_strike,
                    'symbol'        : sym,
                    'entry_price'   : avg_prc,
                    'entry_time'    : datetime.datetime.now(),
                    'entry_vwap'    : vwap_val,
                    'entry_dist'    : 0.0,
                    'atm_at_entry'  : self.eng.atm_strike,
                    'nifty_at_entry': 0.0,
                    'filled_qty'    : abs(net_qty),
                    'order_id'      : 'RECOVERED',
                    'peak_price'    : avg_prc,
                }
                print(f"  [Recovery] Resumed {direction} {sym} "
                      f"qty={net_qty} avg={avg_prc:.2f} SL={sl_price:.2f}")
        except Exception as e:
            logger.debug(f"Position recovery: {e}")

    def _emergency_save(self):
        """Save capital + report immediately. Called on SIGTERM or periodic autosave."""
        try:
            self.cap_mgr._save()
        except Exception as e:
            logger.error(f"Emergency capital save: {e}")
        try:
            self.save_daily_report()
        except Exception as e:
            logger.error(f"Emergency report save: {e}")
        if self.report_mgr:
            try:
                self.report_mgr.generate_daily_report()
            except Exception as e:
                logger.error(f"Emergency report_mgr save: {e}")

    def _on_session_reconnect(self, new_client):
        self.client = new_client
        if self.opt_mgr:
            self.opt_mgr.client = new_client
        print("  [Session] Client refreshed after re-login")
        self.telegram.alert_session("Session re-login successful")

    def _safe_int(self, value, default=0):
        try:
            return int(float(value))
        except Exception:
            return default

    def _safe_float(self, value, default=0.0):
        try:
            return float(value)
        except Exception:
            return default

    def _extract_order_id(self, resp):
        if isinstance(resp, dict):
            return str(resp.get('nOrdNo') or resp.get('order_id') or
                       resp.get('orderId') or '')
        return ''

    def _normalize_rows(self, resp):
        if isinstance(resp, list):
            return resp
        if isinstance(resp, dict):
            data = resp.get('data') or resp.get('message') or []
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return [data]
        return []

    def _get_order_snapshot(self, order_id):
        if not order_id or config.PAPER_TRADE:
            return {}
        snapshot = {}
        try:
            hist = self.client.order_history(order_id=order_id)
            rows = self._normalize_rows(hist)
            if rows:
                snapshot.update(rows[-1])
        except Exception as e:
            logger.debug(f"order_history({order_id}): {e}")
        if not snapshot:
            try:
                rpt  = self.client.order_report()
                rows = self._normalize_rows(rpt)
                for row in rows:
                    row_id = str(row.get('nOrdNo') or row.get('order_id') or
                                 row.get('orderId') or '')
                    if row_id == str(order_id):
                        snapshot = row
                        break
            except Exception as e:
                logger.debug(f"order_report {order_id}: {e}")
        status     = str(snapshot.get('ordSt') or snapshot.get('stat') or
                         snapshot.get('status') or '').lower()
        qty        = self._safe_int(snapshot.get('qty') or snapshot.get('quantity') or
                                    snapshot.get('ordQty'), 0)
        filled_qty = self._safe_int(snapshot.get('fldQty') or
                                    snapshot.get('filled_quantity') or
                                    snapshot.get('fillQty'), 0)
        unfilled   = self._safe_int(snapshot.get('unFldSz') or
                                    snapshot.get('pending_quantity') or
                                    max(qty - filled_qty, 0), 0)
        avg_price  = self._safe_float(snapshot.get('avgPrc') or
                                      snapshot.get('avg_price') or
                                      snapshot.get('average_price'), 0.0)
        return {
            **snapshot,
            'order_id'   : str(order_id),
            'status'     : status,
            'qty'        : qty,
            'filled_qty' : filled_qty,
            'pending_qty': unfilled,
            'avg_price'  : avg_price,
            'rej_reason' : (snapshot.get('rejRsn') or snapshot.get('rejReason') or
                            snapshot.get('rejMsg') or snapshot.get('remarks') or
                            snapshot.get('message') or ''),
        }

    def _cancel_open_order(self, order_id, amo='NO'):
        if not order_id or config.PAPER_TRADE:
            return {'order_id': order_id, 'cancelled': False}
        last_error = None
        for kwargs in (
            {'order_id': order_id, 'amo': amo, 'isVerify': True},
            {'order_id': order_id, 'isVerify': True},
            {'order_id': order_id, 'amo': amo},
            {'order_id': order_id},
        ):
            try:
                resp = self.client.cancel_order(**kwargs)
                return {'order_id': order_id, 'cancelled': True, 'response': resp}
            except Exception as e:
                last_error = e
        return {'order_id': order_id, 'cancelled': False,
                'error': str(last_error) if last_error else 'cancel failed'}

    def _place_order(self, side, token, symbol, price, quantity):
        is_buy = (side == 'BUY')
        if config.PAPER_TRADE:
            oid = f"PAPER_{side}_{datetime.datetime.now().strftime('%H%M%S%f')}"
            sim_price = (round(float(price) + config.BUY_LIMIT_BUFFER, 2)
                         if is_buy else float(price))
            logger.info(f"PAPER {side} {symbol} ref={price:.2f} sim={sim_price:.2f} qty={quantity}")
            return {'order_id': oid, 'avg_price': sim_price, 'amo': 'NO',
                    'response': {'stat': 'Ok', 'nOrdNo': oid}}

        tx_type = "B" if is_buy else "S"
        now_t   = datetime.datetime.now().time()
        amo     = ("YES" if config.ENABLE_AMO_OUTSIDE_HOURS and
                   (now_t < datetime.time(9, 15) or now_t > datetime.time(15, 30))
                   else "NO")
        if is_buy:
            limit_price = round(float(price) + config.BUY_LIMIT_BUFFER, 1)
            order_type  = "L"
            send_price  = str(limit_price)
        else:
            limit_price = float(price)
            order_type  = "MKT"
            send_price  = "0"
        try:
            resp = self.client.place_order(
                exchange_segment   = config.FO_SEGMENT,
                product            = "NRML",
                trading_symbol     = symbol,
                transaction_type   = tx_type,
                quantity           = str(quantity),
                order_type         = order_type,
                price              = send_price,
                validity           = "DAY",
                amo                = amo,
                disclosed_quantity = "0",
                market_protection  = "0",
                pf                 = "N",
                trigger_price      = "0",
                tag                = f"VWAP_{side}",
            )
            oid = self._extract_order_id(resp)
            logger.info(f"LIVE {side} {symbol} ref={price:.2f} "
                        f"{'limit='+str(limit_price) if is_buy else 'MKT'} "
                        f"qty={quantity} amo={amo} id={oid}")
            return {'order_id': oid, 'avg_price': limit_price, 'amo': amo, 'response': resp}
        except Exception as e:
            logger.error(f"Place order ({side} {symbol} qty={quantity}): {e}")
            return {'order_id': None, 'avg_price': 0.0, 'amo': amo, 'error': str(e)}

    def _wait_for_order_fill(self, order_id, expected_qty, timeout_secs, amo='NO'):
        if config.PAPER_TRADE:
            return {'order_id': order_id, 'status': 'complete',
                    'qty': expected_qty, 'filled_qty': expected_qty,
                    'pending_qty': 0, 'avg_price': 0.0}
        deadline = time.time() + max(timeout_secs, 1)
        last     = {'order_id': order_id, 'status': 'unknown',
                    'qty': expected_qty, 'filled_qty': 0,
                    'pending_qty': expected_qty, 'avg_price': 0.0}
        terminal = {'complete', 'completed', 'traded', 'cancelled', 'canceled', 'rejected'}
        while time.time() < deadline:
            snap = self._get_order_snapshot(order_id)
            if snap:
                last    = snap
                status  = str(snap.get('status') or '').lower()
                filled  = int(snap.get('filled_qty') or 0)
                pending = int(snap.get('pending_qty') or max(expected_qty - filled, 0))
                if filled >= expected_qty or pending <= 0 or status in terminal:
                    return last
            time.sleep(config.ORDER_STATUS_POLL_SECS)
        filled  = int(last.get('filled_qty') or 0)
        pending = max(expected_qty - filled, 0)
        if pending > 0:
            cancel_info = self._cancel_open_order(order_id, amo=amo)
            last['cancel_response'] = cancel_info
            time.sleep(1)
            snap = self._get_order_snapshot(order_id)
            if snap:
                last = {**last, **snap, 'cancel_response': cancel_info}
        return last

    def _execute_managed_order(self, side, token, symbol, ref_price, quantity,
                               timeout_secs, chase_remaining=False):
        if quantity <= 0:
            return {'filled_qty': 0, 'avg_price': 0.0,
                    'pending_qty': 0, 'status': 'skipped'}
        if config.PAPER_TRADE:
            fill_price = round(float(ref_price) +
                               (1.0 if side == 'BUY' else -1.0), 2)
            return {'order_id'   : f"PAPER_{side}_{datetime.datetime.now().strftime('%H%M%S%f')}",
                    'filled_qty' : quantity, 'pending_qty': 0,
                    'status'     : 'complete', 'avg_price': fill_price, 'attempts': 1}

        remaining    = int(quantity)
        total_filled = 0
        total_value  = 0.0
        final_oid    = None
        final_status = 'unknown'
        final_rej    = ''
        attempts     = 1 if (side == 'BUY' or not chase_remaining) else max(1, config.EXIT_RETRY_ATTEMPTS)

        for attempt in range(1, attempts + 1):
            live_ref = (self.eng.get_ltp(token) or ref_price) if side == 'SELL' else ref_price
            placed   = self._place_order(side, token, symbol, live_ref, remaining)
            final_oid = placed.get('order_id')
            if not final_oid:
                final_status = 'place_failed'
                break
            snap         = self._wait_for_order_fill(
                final_oid, remaining, timeout_secs, amo=placed.get('amo', 'NO'))
            filled_now   = self._safe_int(snap.get('filled_qty'), 0)
            avg_now      = self._safe_float(snap.get('avg_price'), 0.0)
            final_status = snap.get('status') or final_status
            final_rej    = snap.get('rej_reason') or final_rej
            if filled_now > 0:
                total_filled += filled_now
                total_value  += filled_now * (avg_now or live_ref)
            remaining = max(quantity - total_filled, 0)
            logger.info(f"{side} attempt={attempt} {symbol} filled={filled_now} "
                        f"total={total_filled} remaining={remaining}")
            if remaining <= 0 or side == 'BUY' or not chase_remaining:
                break

        avg_price = round(total_value / total_filled, 2) if total_filled > 0 else 0.0
        return {'order_id'   : final_oid, 'filled_qty' : total_filled,
                'pending_qty': remaining, 'status'     : final_status,
                'avg_price'  : avg_price, 'attempts'   : attempts,
                'rej_reason' : final_rej}

    # ── Daily report ───────────────────────────────────────
    def save_daily_report(self):
        today = datetime.date.today()
        os.makedirs("reports", exist_ok=True)
        fname = f"reports/daily_{today.strftime('%Y%m%d')}.txt"

        total_pnl = sum(t['net_rs'] for t in self.trades_today)
        wins      = sum(1 for t in self.trades_today if t['won'])
        losses    = len(self.trades_today) - wins
        win_rate  = wins / len(self.trades_today) * 100 if self.trades_today else 0

        lines = []
        lines.append("="*62)
        lines.append(f"  DAILY REPORT — {today.strftime('%d %b %Y')} "
                     f"({'PAPER' if config.PAPER_TRADE else 'LIVE'})")
        lines.append("="*62)
        lines.append(f"  Expiry      : {self.expiry_date}")
        lines.append(f"  India VIX   : {self.today_vix:.2f}")
        lines.append(f"  Gap         : {self.gap_pct:.1f}%")
        lines.append(f"  ATM strike  : {self.eng.atm_strike}")
        lines.append(f"  Mirror pair : CE={self.eng.atm_strike-200}  PE={self.eng.atm_strike+200}")
        lines.append("")
        lines.append(f"  Trades: {len(self.trades_today)}  Wins: {wins}  "
                     f"Losses: {losses}  Win rate: {win_rate:.0f}%")
        lines.append(f"  Total P&L   : Rs {total_pnl:+.0f} (net of costs)")
        lines.append("")
        lines.append("  TRADE LOG:")
        lines.append("  " + "-"*55)

        for i, t in enumerate(self.trades_today, 1):
            result    = "WIN" if t['won'] else "LOSS"
            peak      = t.get('peak_price', t['exit_price'])
            max_pts   = peak - t['entry_price']
            got_pts   = t['pnl_pts']
            captured  = round(got_pts / max_pts * 100, 0) if max_pts > 0 else 0
            missed    = round(max_pts - got_pts, 1)
            duration  = round((t['exit_time'] - t['entry_time']).total_seconds() / 60, 1)
            lines.append(
                f"  #{i} {result} | {t['direction']} {t['strike']} | "
                f"{t['entry_time'].strftime('%H:%M:%S')}"
                f"->{t['exit_time'].strftime('%H:%M:%S')} ({duration}min)")
            lines.append(
                f"     Entry={t['entry_price']:.2f} VWAP={t['entry_vwap']:.2f} "
                f"dist={t['entry_dist']:.1f}pts  Exit={t['exit_price']:.2f}  Peak={peak:.2f}")
            lines.append(
                f"     Got={got_pts:+.1f}pts  Max={max_pts:.1f}pts  "
                f"Captured={captured:.0f}%  Missed={missed:.1f}pts  Net=Rs{t['net_rs']:+.0f}")
            lines.append(
                f"     Phase={t.get('exit_phase','?')}"
                f"  BE={'Y' if t.get('breakeven_done') else 'N'}"
                f"  Trail={'Y' if t.get('trail_active') else 'N'}")

        if not self.trades_today:
            lines.append("  No trades today.")
        lines.append("")
        lines.append("  OBSERVATIONS (fill manually):")
        lines.append("  1. ")
        lines.append("  2. ")
        lines.append("="*62)

        content = "\n".join(lines)
        with open(fname, 'w', encoding='utf-8') as f:
            f.write(content)
        print(content)
        print(f"\n  [Report] Saved: {fname}")

        if self.report_mgr:
            try:
                self.report_mgr.generate_daily_report()
            except Exception as e:
                logger.error(f"ReportManager error: {e}")

    # ── Main run loop ──────────────────────────────────────
    def run(self):
        self.initialize()
        subscribed  = False
        last_status = None
        print("\n[*] Running. Ctrl+C to stop.\n")

        try:
            while self.is_running:
                now = datetime.datetime.now()
                t   = now.time()

                # Pre-open refresh at 9:12 — indicative prices are live
                if (t >= datetime.time(9, 12) and t < datetime.time(9, 15)
                        and not self._preopen_refreshed):
                    self._preopen_refreshed = True
                    spot = self._get_nifty_spot()
                    if spot > 0:
                        new_atm = round(spot / STRIKE_STEP) * STRIKE_STEP
                        old_atm = self.eng.atm_strike
                        print(f"  [9:12 Refresh] Nifty={spot:.0f} ATM: {old_atm}->{new_atm}")
                        if new_atm != old_atm:
                            self._setup_strikes(spot, force=True)
                            print("  [9:12 Refresh] Pair updated before market open")
                        else:
                            print("  [9:12 Refresh] ATM unchanged")

                # Subscribe at 9:15 — only 2 tokens
                if (t >= datetime.time(9, 15) and t < datetime.time(15, 25)
                        and not subscribed):
                    self.subscribe_options()
                    subscribed = True
                    print("  Market open — watching for signals\n")
                    self._start_hourly_strike_refresh()

                # Square-off check at 3:25
                if t >= datetime.time(15, 25) and self.active_trade:
                    with self._lock:
                        self._check_exit(now)

                # WS watchdog
                if (subscribed and self.last_tick_time and t < datetime.time(15, 25)):
                    secs = (now - self.last_tick_time).total_seconds()
                    if secs > 60 and self.ws_connected:
                        self.ws_connected = False
                        print(f"\n  [WS] No tick for {secs:.0f}s — reconnecting...")
                        self._trigger_reconnect()

                # Status print every 5 mins
                minute = now.minute
                if minute % 5 == 0 and minute != last_status:
                    last_status = minute
                    ws_icon = "[OK]" if self.ws_connected else "[DISCONNECTED]"
                    print(f"  [{now.strftime('%H:%M')}] WS:{ws_icon} | "
                          f"Trades:{len(self.trades_today)} | "
                          f"VIX:{self.today_vix:.1f} | "
                          f"{self.eng.get_status()}")

                # Periodic auto-save every 30 mins
                if (subscribed and
                        (self._last_autosave is None or
                         (now - self._last_autosave).total_seconds() >= self._autosave_mins * 60)):
                    self._last_autosave = now
                    self._autosave_capital()
                    logger.debug(f"Auto-saved @ {now.strftime('%H:%M')}")

                if t >= datetime.time(15, 26) and subscribed:
                    break

                time.sleep(1)

        except KeyboardInterrupt:
            print("\n\n[*] Stopped by user.")
            if self.active_trade:
                now   = datetime.datetime.now()
                trade = self.active_trade
                ep    = self.eng.get_ltp(trade['token'])
                if ep <= 0:
                    ep = trade['entry_price']
                pnl_pts    = ep - trade['entry_price']
                qty        = int(trade.get('filled_qty') or config.LOT_SIZE)
                pnl_rs     = pnl_pts * qty
                total_cost = _calc_trade_cost(trade['entry_price'], ep, qty)
                net_rs     = round(pnl_rs - total_cost, 2)
                completed  = {
                    **trade,
                    'exit_price'    : ep,
                    'exit_time'     : now,
                    'exit_reason'   : 'Stopped by user',
                    'exit_phase'    : 'Manual stop',
                    'nifty_at_exit' : 0.0,
                    'sold_qty'      : qty,
                    'pnl_pts'       : round(pnl_pts, 2),
                    'pnl_rs'        : round(pnl_rs, 2),
                    'total_cost'    : total_cost,
                    'net_rs'        : net_rs,
                    'won'           : net_rs > 0,
                    'breakeven_done': self.eng.breakeven_done,
                    'trail_active'  : self.eng.trail_active,
                }
                self.cap_mgr.update_after_trade(net_rs)
                self.trades_today.append(completed)
                if self.report_mgr:
                    try:
                        self.report_mgr.log_trade(completed)
                    except Exception:
                        pass
                self.active_trade = None
                print(f"  Forced exit @ {ep:.2f} | Net P&L = Rs {net_rs:+.0f}")

        finally:
            self.is_running = False
            self._graceful_shutdown(reason="end of session")


if __name__ == '__main__':
    algo = NiftyOptionsAlgo()
    algo.run()
