# ============================================================
# MAIN.PY — Nifty Options TradingView VWAP Algo
# ============================================================
# - 5 CE + 5 PE strikes, each 150 pts apart, centered at ATM
# - All 10 VWAPs calculated from 9:15 AM
# - Strike refresh only before 10:00 AM
# - Gap protection: gap > 1.5% → skip till 9:45 AM
# - Entry: closest ATM strike in 0-3 pt zone + opposite below VWAP
# - SL: 5 pts early, 10+dist normal
# - Breakeven: +10 pts → SL = entry+1
# - Trail: +20 pts → 1 pt trail
# - Square off: 3:25 PM
# ============================================================
import os
os.environ['TZ'] = 'Asia/Kolkata'
import time
time.tzset()
import datetime
# NSE fetcher removed — NSE blocks all automated requests with 403.
# VWAP is now calculated purely from Kotak REST volume deltas (most accurate).
_NSE_AVAILABLE = False
import logging
import threading
import time
import os

import config
from auth            import get_kotak_session
from vwap_engine     import StrategyEngine
from option_manager  import OptionManager, get_next_expiry
from capital_manager import CapitalManager
from session_manager import SessionManager
from telegram_notifier import TelegramNotifier

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

STRIKE_STEP   = 50    # Nifty strike interval
ITM_DEPTH_PTS = 200   # How deep ITM to scan (200 pts)
OTM_COUNT     = 1     # How many OTM strikes to include as backup


def _build_ce_strikes(atm, step=STRIKE_STEP,
                      itm_pts=ITM_DEPTH_PTS, otm=OTM_COUNT):
    """
    CE strikes: 1 OTM → ATM → deep ITM (200 pts below ATM).
    CE goes ITM as strike goes DOWN.
    e.g. ATM=23200 → [23250(OTM), 23200(ATM), 23150, 23100, 23050, 23000(200 ITM)]
    """
    strikes = []
    for i in range(-otm, itm_pts // step + 1):   # -1 OTM to +4 ITM steps
        strikes.append(atm - i * step)
    return sorted(set(strikes), reverse=True)     # highest first


def _build_pe_strikes(atm, step=STRIKE_STEP,
                      itm_pts=ITM_DEPTH_PTS, otm=OTM_COUNT):
    """
    PE strikes: deep ITM (200 pts above ATM) → ATM → 1 OTM.
    PE goes ITM as strike goes UP.
    e.g. ATM=23200 → [23400(200 ITM), 23350, 23300, 23250, 23200(ATM), 23150(OTM)]
    """
    strikes = []
    for i in range(-otm, itm_pts // step + 1):
        strikes.append(atm + i * step)
    return sorted(set(strikes))                   # lowest first


def _calc_trade_cost(entry_price: float, exit_price: float, lot_size: int) -> float:
    """
    Accurate NSE F&O cost breakdown for one round-trip options trade (buy + sell).

    STT history:
      Till Sep 30 2024  : 0.0625% on sell premium
      Oct 1 2024 onward : 0.1%    on sell premium  (Budget FY25)
      Apr 1 2026 onward : 0.15%   on sell premium  (Budget FY27)

    Other charges (per round-trip):
      Brokerage    : Rs 40 flat (Rs 20 buy + Rs 20 sell)
      NSE txn fee  : 0.035% of total premium turnover (buy + sell)
      Stamp duty   : 0.003% on buy side only
      GST          : 18% on (brokerage + txn charge)
    """
    today = datetime.date.today()
    stt_rate   = 0.0015 if today >= datetime.date(2026, 4, 1) else 0.001
    sell_value = exit_price  * lot_size
    buy_value  = entry_price * lot_size
    total_prem = sell_value + buy_value
    stt        = round(sell_value  * stt_rate,   2)
    txn_charge = round(total_prem  * 0.00035,    2)   # 0.035% both legs
    stamp      = round(buy_value   * 0.00003,    2)   # 0.003% buy only
    brokerage  = 40.0                                  # Rs 20 x 2 legs
    gst        = round((brokerage + txn_charge) * 0.18, 2)
    return round(stt + txn_charge + stamp + brokerage + gst, 2)


class NiftyOptionsAlgo:

    def __init__(self):
        self.client       = None
        self.eng          = StrategyEngine()
        self.opt_mgr      = None
        self.cap_mgr      = CapitalManager()
        self.sess_mgr     = None
        self._lock        = threading.Lock()
        self.is_running   = True
        # NSE removed — using Kotak REST volume deltas for VWAP
        self._last_nse_sync = None

        # Option state
        self.expiry_date  = None
        self.ce_tokens    = {}   # strike → token
        self.pe_tokens    = {}   # strike → token
        self.ce_symbols   = {}   # strike → symbol
        self.pe_symbols   = {}   # strike → symbol
        self.all_tokens   = {}   # token → (strike, type)

        # Trade state
        self.active_trade = None
        self.trades_today   = []
        self.daily_halted   = False   # set True when daily loss limit hit
        self.telegram       = TelegramNotifier()
        self.today_vix    = 15.0
        self.prev_close   = 0.0
        self.gap_pct      = 0.0

        # VWAP sync state (exchange resync every 60s)
        self.last_vwap_sync  = None
        self.vwap_sync_secs  = 60    # resync every 60 seconds

        # WS state
        self.ws_connected    = False
        self.last_tick_time  = None
        self._reconnect_count  = 0
        self._reconnect_active = False
        self.subscribed      = False
        self._raw_logged     = False
        self._tick_keys_logged = False

    # ── Initialize ────────────────────────────────────────
    def initialize(self):
        print("\n" + "="*62)
        print("  NIFTY OPTIONS ALGO — Multi-Strike VWAP Strategy")
        print("="*62)
        mode = "PAPER TRADE" if config.PAPER_TRADE else "*** LIVE — REAL MONEY ***"
        print(f"  Mode       : {mode}")
        print(f"  Strikes    : ATM + 200pts ITM + 1 OTM (50pt steps)")
        print(f"  Entry zone : 0-3 pts above option VWAP")
        print(f"  SL (early) : 5 pts  (9:15-9:40)")
        print(f"  SL (normal): 10 pts + dist above VWAP")
        print(f"  Breakeven  : +10 pts | Trail: +20 pts")
        print(f"  Gap protect: No entries if gap > 1.5% until 9:45")
        print(f"  Square off : 3:25 PM")
        print("="*62)

        self.cap_mgr.print_status()

        print("\n[*] Logging into Kotak Neo...")
        self._raw_logged = False
        self.client      = get_kotak_session()
        self.setup_websocket()

        self.opt_mgr  = OptionManager(self.client)
        self.sess_mgr = SessionManager(self.client, get_kotak_session)
        self.sess_mgr.on_reconnect = self._on_session_reconnect
        self.sess_mgr.start()

        self.expiry_date = get_next_expiry()
        print(f"[*] Weekly expiry  : {self.expiry_date} (Monday/Tuesday adjusted)")

        self._recover_open_position()
        self._fetch_vix()
        self._fetch_prev_close()
        target_pts, target_reason = self._get_dynamic_target_points()
        print(f"[*] Profit target  : {target_pts:.1f} pts ({target_reason})")

        spot = self._get_nifty_spot()
        if spot <= 0:
            spot = 23000
        self._setup_strikes(spot)

        # Gap check
        if self.prev_close > 0:
            self.gap_pct = abs(spot - self.prev_close) / self.prev_close * 100
            if self.gap_pct > 1.5:
                print(f"\n  [GAP ALERT] Gap = {self.gap_pct:.1f}% > 1.5%")
                print(f"  [GAP ALERT] No entries until 9:45 AM!")
            else:
                print(f"\n[*] Gap = {self.gap_pct:.1f}% — normal open")

        print(f"\n[*] Waiting for 9:15 AM...")
        mode_str = "PAPER" if config.PAPER_TRADE else "LIVE"
        self.telegram.alert_startup(mode_str, self.expiry_date, getattr(self.eng, "atm_strike", 0))

    def _fetch_vix(self):
        try:
            for name in ["INDIA VIX", "India VIX"]:
                resp = self.client.quotes(
                    instrument_tokens=[{
                        "instrument_token": name,
                        "exchange_segment": config.CM_SEGMENT
                    }], quote_type="ltp")
                data = resp if isinstance(resp, list) else (
                       resp.get('message') or resp.get('data') or [])
                if data:
                    v = float(data[0].get('ltp') or 0)
                    if v > 0:
                        self.today_vix = v
                        print(f"[*] India VIX      : {v:.2f}")
                        return
        except Exception as e:
            logger.debug(f"VIX: {e}")
        print(f"[*] India VIX      : {self.today_vix:.1f} (default)")

    def _fetch_prev_close(self):
        try:
            resp = self.client.quotes(
                instrument_tokens=[{
                    "instrument_token": "Nifty 50",
                    "exchange_segment": config.CM_SEGMENT
                }], quote_type="ohlc")
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
                instrument_tokens=[{
                    "instrument_token": "Nifty 50",
                    "exchange_segment": config.CM_SEGMENT
                }], quote_type="ltp")
            data = resp if isinstance(resp, list) else (
                   resp.get('message') or resp.get('data') or [])
            if data:
                return float(data[0].get('ltp') or 0)
        except Exception as e:
            logger.debug(f"Spot: {e}")
        return 0.0

    def _setup_strikes(self, spot, force=False):
        """
        Setup CE + PE strikes.
        CE: ATM → 200 pts ITM (strikes go down) + 1 OTM above ATM
        PE: ATM → 200 pts ITM (strikes go up)   + 1 OTM below ATM
        Strike refresh locked after 10 AM.
        """
        now = datetime.datetime.now()
        if (not force
                and now.time() >= datetime.time(10, 0)
                and self.subscribed):
            print("  [Strikes] After 10 AM — no strike change")
            return

        atm          = round(spot / STRIKE_STEP) * STRIKE_STEP
        ce_strike_list = _build_ce_strikes(atm)
        pe_strike_list = _build_pe_strikes(atm)

        print(f"\n[*] Setting up strikes | Nifty={spot:.0f} ATM={atm}")
        print(f"    CE strikes (OTM→200 ITM): {ce_strike_list}")
        print(f"    PE strikes (200 ITM→OTM): {pe_strike_list}")

        ce_tokens  = {}
        pe_tokens  = {}
        ce_symbols = {}
        pe_symbols = {}

        for strike in ce_strike_list:
            tok = self.opt_mgr.get_option_token(strike, 'CE', self.expiry_date)
            if tok:
                ce_tokens[strike]  = tok
                ce_symbols[strike] = self.opt_mgr.get_trading_symbol(
                    strike, 'CE', self.expiry_date)

        for strike in pe_strike_list:
            tok = self.opt_mgr.get_option_token(strike, 'PE', self.expiry_date)
            if tok:
                pe_tokens[strike]  = tok
                pe_symbols[strike] = self.opt_mgr.get_trading_symbol(
                    strike, 'PE', self.expiry_date)

        self.ce_tokens  = ce_tokens
        self.pe_tokens  = pe_tokens
        self.ce_symbols = ce_symbols
        self.pe_symbols = pe_symbols

        # Build reverse lookup: token → (strike, type)
        self.all_tokens = {}
        for strike, tok in ce_tokens.items():
            self.all_tokens[str(tok)] = (strike, 'CE')
        for strike, tok in pe_tokens.items():
            self.all_tokens[str(tok)] = (strike, 'PE')

        # Setup engine
        self.eng.setup_strikes(spot, ce_tokens, pe_tokens)
        self.eng.update_atm(spot)

        print(f"    CE tokens: {ce_tokens}")
        print(f"    PE tokens: {pe_tokens}")

    def _start_hourly_strike_refresh(self):
        """
        Runs in a background thread — checks Nifty spot every hour
        and refreshes strikes if ATM has shifted by >= 50pts AND
        no trade is currently active.

        Parallel so it never blocks tick processing or signal checking.
        """
        def _loop():
            while self.is_running:
                time.sleep(3600)   # wait 1 hour
                if not self.is_running:
                    break
                now = datetime.datetime.now()
                t   = now.time()
                # Only during market hours, not too close to open/close
                if t < datetime.time(10, 0) or t >= datetime.time(15, 0):
                    continue
                if self.active_trade:
                    logger.debug("Hourly strike refresh skipped — trade active")
                    continue
                try:
                    spot = self._get_nifty_spot()
                    if spot <= 0:
                        continue
                    new_atm = round(spot / STRIKE_STEP) * STRIKE_STEP
                    old_atm = self.eng.atm_strike
                    if abs(new_atm - old_atm) < STRIKE_STEP:
                        logger.debug(f"Hourly check: ATM unchanged ({old_atm}) — no refresh needed")
                        continue
                    print(f"  [StrikeRefresh] ATM shifted {old_atm}→{new_atm} "
                          f"(Nifty={spot:.0f}) — refreshing strikes...")
                    # Unsubscribe current tokens
                    try:
                        drop = [{"instrument_token": tok,
                                 "exchange_segment": config.FO_SEGMENT}
                                for tok in self.all_tokens.keys()]
                        if drop:
                            self.client.unsubscribe(
                                instrument_tokens=drop,
                                isIndex=False, isDepth=False)
                    except Exception as e:
                        logger.debug(f"Unsubscribe before refresh: {e}")
                    # Reset trim flag so 9:25 trim can re-run if needed
                    self._trimmed = False
                    # Setup new strikes and subscribe
                    self._setup_strikes(spot, force=True)
                    self.subscribe_options()
                    print(f"  [StrikeRefresh] ✅ New strikes subscribed for ATM={new_atm}")
                    self.telegram.alert_session(
                        f"Strikes refreshed: ATM {old_atm}→{new_atm} (Nifty={spot:.0f})")
                except Exception as e:
                    logger.error(f"Hourly strike refresh error: {e}")

        t = threading.Thread(target=_loop, daemon=True, name="HourlyStrikeRefresh")
        t.start()

    # ── WebSocket ─────────────────────────────────────────
    def _ws_on_message(self, message):
        if not self.is_running:
            return
        try:
            # Log first raw tick
            if not self._raw_logged:
                import json, os
                os.makedirs("logs", exist_ok=True)
                with open("logs/raw_tick_debug.txt", "w") as f:
                    f.write("="*60 + "\n")
                    f.write(f"Captured: {datetime.datetime.now()}\n")
                    f.write(f"Tokens: {self.all_tokens}\n")
                    f.write("="*60 + "\n\n")
                    try:
                        f.write(json.dumps(message, indent=2))
                    except Exception:
                        f.write(str(message))
                self._raw_logged = True
                print(f"\n  [DEBUG] Raw tick saved to logs/raw_tick_debug.txt\n")

            if not isinstance(message, dict):
                return
            if message.get('type', '') not in [
                    'stock_feed', 'sf', 'index_feed', 'if']:
                return

            now   = datetime.datetime.now()
            ticks = message.get('data', [])

            for tick in ticks:
                token = str(tick.get('tk', '') or
                            tick.get('token', '') or '')
                ltp   = float(tick.get('ltp', 0) or
                              tick.get('ltP', 0) or 0)

                # TRUE VWAP FROM WS TICK:
                # Kotak WS sends in every tick:
                #   'to' = total turnover = Sigma(price x qty) since 9:15
                #   'v'  = total volume   = Sigma(qty)         since 9:15
                #   'ap' = to / v         = session VWAP (pre-computed)
                # This is IDENTICAL to TradingView VWAP. No NSE, no REST delay.
                ap  = float(tick.get('ap', 0) or 0)
                vol = float(tick.get('v',  0) or 0)

                ltt_str = tick.get('ltt', '')
                try:
                    tick_ts = datetime.datetime.strptime(
                        ltt_str, '%d/%m/%Y %H:%M:%S') if ltt_str else now
                except Exception:
                    tick_ts = now

                if ltp <= 0:
                    continue

                self.last_tick_time = now

                with self._lock:
                    if token in self.all_tokens:
                        self.eng.add_tick(token, ltp, 1.0, tick_ts)
                        if ap > 0:
                            self.eng.set_vwap_direct(token, ap, vol)
                    else:
                        if not self._tick_keys_logged:
                            print(f"\n  [DEBUG] Unknown token={token} "
                                  f"known={list(self.all_tokens.keys())}")
                            self._tick_keys_logged = True
                        continue

                # Order placement happens OUTSIDE the lock so ticks keep
                # flowing and VWAP keeps updating during order fill wait.
                if token in self.all_tokens:
                    self._on_tick(now)

        except Exception as e:
            logger.debug(f"on_message: {e}")

    def _ws_on_open(self, msg):
        logger.info("WS connected")
        self.ws_connected     = True
        self._reconnect_count = 0
        print(f"  [WS] Connected ({datetime.datetime.now().strftime('%H:%M:%S')})\n")

    def _ws_on_error(self, error):
        logger.error(f"WS error: {error}")
        self.ws_connected = False

    def _ws_on_close(self, msg):
        logger.warning(f"WS closed: {msg}")
        self.ws_connected = False
        t = datetime.datetime.now()
        if t.time() < datetime.time(15, 25):
            print(f"\n  [WS] Disconnected — reconnecting...")
            self._trigger_reconnect()

    def setup_websocket(self):
        self.client.on_message = self._ws_on_message
        self.client.on_error   = self._ws_on_error
        self.client.on_close   = self._ws_on_close
        self.client.on_open    = self._ws_on_open

    def subscribe_options(self):
        """Subscribe all CE + PE tokens. Seed LTPs via REST first."""
        if not self.all_tokens:
            print("  [WS] No tokens to subscribe")
            return

        # Seed LTPs via REST
        try:
            all_tok_list = [
                {"instrument_token": str(tok),
                 "exchange_segment": config.FO_SEGMENT}
                for tok in self.all_tokens.keys()
            ]
            resp = self.client.quotes(
                instrument_tokens=all_tok_list,
                quote_type="ltp")
            data = resp if isinstance(resp, list) else (
                   resp.get('message') or resp.get('data') or [])
            seeded = 0
            if data:
                # Try to match by token in response
                for item in data:
                    tok = str(item.get('instrument_token') or
                              item.get('tk') or '')
                    ltp = float(item.get('ltp') or 0)
                    if tok in self.all_tokens and ltp > 0:
                        self.eng.seed_ltp(tok, ltp)
                        seeded += 1
                # Fallback: if tokens not in response, seed by position
                if seeded == 0 and len(data) == len(all_tok_list):
                    for i, (tok, _) in enumerate(self.all_tokens.items()):
                        ltp = float(data[i].get('ltp') or 0)
                        if ltp > 0:
                            self.eng.seed_ltp(tok, ltp)
                            seeded += 1
            print(f"  Seeded {seeded}/{len(self.all_tokens)} LTPs via REST")
        except Exception as e:
            logger.debug(f"Seed error: {e}")

        # Subscribe via WS
        try:
            tokens = [{"instrument_token": str(tok),
                       "exchange_segment": config.FO_SEGMENT}
                      for tok in self.all_tokens.keys()]
            self.client.subscribe(
                instrument_tokens=tokens,
                isIndex=False, isDepth=False)
            self.subscribed = True
            print(f"  Subscribed {len(tokens)} tokens "
                  f"({len(self.ce_tokens)} CE + "
                  f"{len(self.pe_tokens)} PE) ✅")
        except Exception as e:
            logger.error(f"Subscribe error: {e}")

    def _trim_to_best_strikes(self):
        """
        At 9:25 AM — keep only best CE + PE within MIN_ITM_DISTANCE–MAX_ITM_DISTANCE zone.
        Unsubscribe remaining tokens to stabilize WS.
        Best = strike closest to MIN_ITM_DISTANCE boundary (most liquid) with most ticks.
        """
        self._trimmed = True
        ts = datetime.datetime.now().strftime('%H:%M:%S')
        spot = self._get_nifty_spot()
        if spot > 0:
            self.eng.update_atm(spot)

        atm = self.eng.atm_strike

        # Best ITM CE = CE strike in [ATM-MAX_ITM .. ATM-MIN_ITM] with ticks
        # Sorted highest first = least deep ITM first = most liquid
        best_ce_token  = None
        best_ce_strike = None
        for strike in sorted(self.ce_tokens.keys(), reverse=True):
            itm_dist = atm - strike
            if config.MIN_ITM_DISTANCE <= itm_dist <= config.MAX_ITM_DISTANCE:
                tok = str(self.ce_tokens[strike])
                vwap_obj = self.eng.ce_strikes.get(tok)
                if vwap_obj and vwap_obj.tick_count > 0:
                    best_ce_token  = tok
                    best_ce_strike = strike
                    break

        # Best ITM PE = PE strike in [ATM+MIN_ITM .. ATM+MAX_ITM] with ticks
        # Sorted lowest first = least deep ITM first = most liquid
        best_pe_token  = None
        best_pe_strike = None
        for strike in sorted(self.pe_tokens.keys()):
            itm_dist = strike - atm
            if config.MIN_ITM_DISTANCE <= itm_dist <= config.MAX_ITM_DISTANCE:
                tok = str(self.pe_tokens[strike])
                vwap_obj = self.eng.pe_strikes.get(tok)
                if vwap_obj and vwap_obj.tick_count > 0:
                    best_pe_token  = tok
                    best_pe_strike = strike
                    break

        # Fix 9: Never trim while a trade is active — active token must stay in all_tokens
        if self.active_trade:
            print(f"  [{ts}][Trim] Trade active — skipping trim to avoid token mismatch")
            self._trimmed = False  # allow retry after trade exits
            return

        if not best_ce_token or not best_pe_token:
            print(f"  [{ts}][Trim] No strikes found in {config.MIN_ITM_DISTANCE}\u2013{config.MAX_ITM_DISTANCE}pt ITM zone \u2014 keeping all")
            return

        ce_itm = atm - best_ce_strike
        pe_itm = best_pe_strike - atm
        print(f"\n  [{ts}][Trim] 9:25 AM \u2014 Dropping to best 2 ITM strikes:")
        print(f"  [{ts}][Trim] CE: strike={best_ce_strike} (ITM={ce_itm}pts) token={best_ce_token}")
        print(f"  [{ts}][Trim] PE: strike={best_pe_strike} (ITM={pe_itm}pts) token={best_pe_token}")

        # Unsubscribe all tokens except best 2
        keep = {best_ce_token, best_pe_token}
        drop_tokens = [
            {"instrument_token": tok,
             "exchange_segment": config.FO_SEGMENT}
            for tok in self.all_tokens.keys()
            if tok not in keep
        ]

        if drop_tokens:
            try:
                self.client.unsubscribe(
                    instrument_tokens=drop_tokens,
                    isIndex=False, isDepth=False)
                print(f"  [{ts}][Trim] Unsubscribed {len(drop_tokens)} tokens ✅")
            except Exception as e:
                logger.debug(f"Unsubscribe error: {e}")

        # Update all_tokens to only keep best 2
        self.all_tokens = {
            best_ce_token: self.all_tokens[best_ce_token],
            best_pe_token: self.all_tokens[best_pe_token],
        }

        # Update ce/pe tokens dicts
        self.ce_tokens  = {best_ce_strike: best_ce_token}
        self.pe_tokens  = {best_pe_strike: best_pe_token}
        self.ce_symbols = {best_ce_strike: self.ce_symbols.get(best_ce_strike, '')}
        self.pe_symbols = {best_pe_strike: self.pe_symbols.get(best_pe_strike, '')}

        # Update engine to only track best 2
        self.eng.ce_strikes = {
            best_ce_token: self.eng.ce_strikes[best_ce_token]}
        self.eng.pe_strikes = {
            best_pe_token: self.eng.pe_strikes[best_pe_token]}

        print(f"  [{ts}][Trim] Now tracking: "
              f"CE {best_ce_strike} (ITM={ce_itm}pts) + PE {best_pe_strike} (ITM={pe_itm}pts) only ✅\n")

    def _trigger_reconnect(self):
        if self._reconnect_active:
            return
        t = datetime.datetime.now().time()
        if t >= datetime.time(15, 25):
            return
        threading.Timer(5.0, self._do_reconnect).start()

    def _do_reconnect(self):
        if self._reconnect_active:
            return
        self._reconnect_active = True
        try:
            self._reconnect_count += 1
            if self._reconnect_count > 20:
                print("  [WS] Max reconnects (20) reached — giving up")
                return
            # Exponential backoff: 5s, 10s, 20s, 40s… capped at 60s
            wait = min(5 * (2 ** min(self._reconnect_count - 1, 3)), 60)
            print(f"  [WS] Reconnect {self._reconnect_count}/20 "
                  f"(wait {wait}s)...")
            time.sleep(wait)
            self.setup_websocket()
            time.sleep(2)
            self.subscribe_options()
            time.sleep(3)
            if self.ws_connected:
                print(f"  [WS] Reconnected ✅")
        except Exception as e:
            logger.error(f"Reconnect: {e}")
        finally:
            self._reconnect_active = False

    # ── Core tick handler ──────────────────────────────────
    def _on_tick(self, now: datetime.datetime):
        t = now.time()
        if t < datetime.time(9, 15) or t >= datetime.time(15, 25):
            return

        # Gap protection — skip entries till 9:45
        if self.gap_pct > 1.5 and t < datetime.time(9, 45):
            return

        # Fix 5: Circuit breaker / market halt detection
        # If no tick for 5+ minutes during market hours → assume circuit/halt
        # Exit monitoring still runs — only new entries are blocked
        circuit_suspected = (
            self.last_tick_time is not None and
            (now - self.last_tick_time).total_seconds() > config.NO_TICK_CIRCUIT_SECS
        )

        # Fix 6 (DISABLED — re-enable when capital grows):
        # Daily P&L hard stop — MAX_DAILY_LOSS_RS and MAX_TRADES_PER_DAY
        # Uncomment the block below to activate:
        # if not self.daily_halted:
        #     net_pnl_today = sum(t2['net_rs'] for t2 in self.trades_today)
        #     if net_pnl_today <= config.MAX_DAILY_LOSS_RS:
        #         self.daily_halted = True
        #         msg = f"Daily loss limit hit: Rs {net_pnl_today:.0f}"
        #         print(f"  [RISK] {msg}")
        #         self.telegram.alert_risk(msg)
        #     if len(self.trades_today) >= config.MAX_TRADES_PER_DAY:
        #         self.daily_halted = True
        #         msg = f"Max {config.MAX_TRADES_PER_DAY} trades/day reached"
        #         print(f"  [RISK] {msg}")
        #         self.telegram.alert_risk(msg)

        # Fix 7 (DISABLED — re-enable when capital grows):
        # Expiry day cutoff — no new entries after 2:30 PM on expiry day
        # Uncomment to activate:
        # is_expiry_day = (self.expiry_date and self.expiry_date == datetime.date.today())
        # cutoff_h, cutoff_m = map(int, config.EXPIRY_DAY_CUTOFF.split(':'))
        # expiry_cutoff_passed = is_expiry_day and t >= datetime.time(cutoff_h, cutoff_m)
        expiry_cutoff_passed = False   # disabled

        if self.active_trade:
            self._check_exit(now)
        elif not circuit_suspected and not expiry_cutoff_passed:
            self._check_entry(now)
        elif circuit_suspected and not self.active_trade:
            # Alert once when circuit first suspected
            if not getattr(self, '_circuit_alerted', False):
                self._circuit_alerted = True
                self.telegram.alert_risk(
                    f"No ticks for {config.NO_TICK_CIRCUIT_SECS}s — "
                    f"circuit breaker or market halt suspected. Entries paused.")
        else:
            self._circuit_alerted = False  # reset flag when ticks resume normally


    # ── TradingView VWAP Sync ──────────────────────────────

    def _nse_sync(self):
        # NSE removed — always returns 403. VWAP volume comes from
        # Kotak REST via _sync_exchange_vwap() which uses volume deltas.
        pass

    def _sync_exchange_vwap(self):
        """
        Fetch Kotak REST quotes for all active tokens every 60s.

        KEY: We use sync_quote_full(tok, ltp, volume) NOT sync_vwap(avg_price).
        Kotak's avg_price = average of last quote interval only, NOT session VWAP.
        vwap_engine.sync_quote() computes volume DELTA = volume_now - volume_prev,
        which gives the correct candle weight for TradingView-accurate VWAP.
        """
        if not self.all_tokens:
            return
        try:
            tok_list = [
                {"instrument_token": tok,
                 "exchange_segment": config.FO_SEGMENT}
                for tok in self.all_tokens.keys()
            ]
            resp = self.client.quotes(
                instrument_tokens=tok_list,
                quote_type="all")
            data = resp if isinstance(resp, list) else (
                   resp.get('message') or resp.get('data') or [])
            if not data:
                return

            synced = 0
            for item in data:
                tok = str(item.get('exchange_token') or
                          item.get('instrument_token') or
                          item.get('tk') or '')
                ltp       = float(item.get('last_price') or
                                  item.get('ltp') or
                                  item.get('lp') or 0)
                volume    = float(item.get('volume') or
                                  item.get('vol') or 0)
                avg_price = float(item.get('average_price') or
                                  item.get('avg_price') or 0)
                if tok in self.all_tokens and volume > 0:
                    # Use ltp+volume delta for accurate VWAP weight
                    self.eng.sync_quote_full(tok, ltp, volume, avg_price)
                    synced += 1

            if synced > 0:
                t = datetime.datetime.now().strftime('%H:%M:%S')
                logger.debug(f"VWAP synced {synced} strikes @ {t}")
        except Exception as e:
            logger.debug(f"VWAP sync error: {e}")

    # ── Entry ──────────────────────────────────────────────
    def _days_to_expiry(self):
        if not self.expiry_date:
            return 99
        return (self.expiry_date - datetime.date.today()).days

    def _get_dynamic_target_points(self):
        days_left = self._days_to_expiry()
        if days_left <= 0:
            target = config.TARGET_EXPIRY_DAY_PTS
            reason = "expiry day"
        elif days_left == 1:
            target = config.TARGET_NEAR_EXPIRY_PTS
            reason = "near expiry"
        elif self.today_vix >= config.VIX_HIGH_THRESHOLD:
            target = config.TARGET_HIGH_VIX_PTS
            reason = f"high VIX ({self.today_vix:.2f})"
        elif self.today_vix >= config.VIX_MEDIUM_THRESHOLD:
            target = config.TARGET_MEDIUM_VIX_PTS
            reason = f"medium VIX ({self.today_vix:.2f})"
        else:
            target = config.TARGET_LOW_VIX_PTS
            reason = f"low VIX ({self.today_vix:.2f})"
        return round(target, 2), reason

    def _check_entry(self, now: datetime.datetime):
        signal, token, vwap_obj = self.eng.check_entry(now)
        if not signal:
            return

        ref_price = vwap_obj.ltp
        if ref_price <= 0:
            print(f"  [Entry] Price unavailable — skip")
            return

        option_cost = ref_price * config.LOT_SIZE
        if option_cost > config.MAX_OPTION_COST:
            print(f"  [Skip] Too expensive: "
                  f"Rs {ref_price:.0f} x {config.LOT_SIZE} "
                  f"= Rs {option_cost:,.0f}")
            return

        strike = vwap_obj.strike
        if signal == 'CE':
            symbol = self.ce_symbols.get(strike, f"NIFTY_CE_{strike}")
        else:
            symbol = self.pe_symbols.get(strike, f"NIFTY_PE_{strike}")

        target_pts, target_reason = self._get_dynamic_target_points()
        entry_exec = self._execute_managed_order(
            side='BUY', token=token, symbol=symbol, ref_price=ref_price,
            quantity=config.LOT_SIZE, timeout_secs=config.ORDER_FILL_TIMEOUT_SECS,
            chase_remaining=False,
        )
        filled_qty = int(entry_exec.get('filled_qty') or 0)
        if filled_qty <= 0:
            status = entry_exec.get('status') or 'unknown'
            reason = entry_exec.get('rej_reason') or ''
            extra = f" | reason: {reason}" if reason else ''
            print(f"  [Entry] {symbol} order not filled (status={status}) — not starting trade watch{extra}")
            logger.warning(f"Entry skipped for {symbol}: status={status} filled_qty=0 pending_qty={entry_exec.get('pending_qty')} reason={reason}")
            return

        entry_price = float(entry_exec.get('avg_price') or ref_price)
        self.eng.on_entry(signal, token, entry_price, vwap_obj, now, target_points=target_pts)

        self.active_trade = {
            'direction'  : signal,
            'token'      : token,
            'strike'     : strike,
            'symbol'     : symbol,
            'entry_price': entry_price,
            'entry_time' : now,
            'entry_vwap' : vwap_obj.get_vwap(),
            'entry_dist' : vwap_obj.dist_above_vwap(),
            'sl_price'   : self.eng.sl_price,
            'target_points': target_pts,
            'target_price' : self.eng.target_price,
            'target_reason': target_reason,
            'order_id'   : entry_exec.get('order_id'),
            'requested_qty': config.LOT_SIZE,
            'filled_qty' : filled_qty,
            'pending_qty': max(config.LOT_SIZE - filled_qty, 0),
            'entry_limit_price': entry_exec.get('last_limit_price'),
            'entry_status': entry_exec.get('status'),
            'entry_rej_reason': entry_exec.get('rej_reason') or '',
            'entry_attempts': entry_exec.get('attempts') or 1,
        }

        session = 'EARLY 5pt SL' if now.time() < datetime.time(9,40)                   else 'Normal 10pt SL'
        ts = now.strftime('%H:%M:%S')
        print(f"\n  {'='*55}")
        print(f"  {'PAPER' if config.PAPER_TRADE else 'LIVE'} "
              f"TRADE: BUY {signal}  [{ts}]")
        print(f"     Symbol : {symbol}")
        print(f"     Strike : {strike} (ATM={self.eng.atm_strike})")
        print(f"     ITM    : {self.eng.atm_strike - strike if signal == 'CE' else strike - self.eng.atm_strike} pts")
        print(f"     Entry  : Rs {entry_price:.2f}")
        print(f"     VWAP   : Rs {vwap_obj.get_vwap():.2f}")
        print(f"     Dist   : {vwap_obj.dist_above_vwap():.1f} pts above VWAP")
        print(f"     Qty    : requested={config.LOT_SIZE} filled={filled_qty} pending_cancelled={max(config.LOT_SIZE - filled_qty, 0)}")
        print(f"     SL     : Rs {self.eng.sl_price:.2f}")
        print(f"     Target : Rs {self.eng.target_price:.2f} (+{target_pts:.1f} pts, {target_reason})")
        print(f"     Session: {session}")
        print(f"  {'='*55}\n")
        self.telegram.alert_entry(
            signal, strike, entry_price,
            vwap_obj.get_vwap(), self.eng.sl_price,
            self.eng.target_price, filled_qty
        )

    # ── Exit ───────────────────────────────────────────────
    def _check_exit(self, now: datetime.datetime):
        should_exit, reason = self.eng.check_exit(now)
        if not should_exit:
            return
        flip = reason and 'flip' in reason

        trade      = self.active_trade
        ref_price  = self.eng.get_ltp(trade['token'])
        if ref_price <= 0:
            return

        qty_to_sell = int(trade.get('filled_qty') or config.LOT_SIZE)
        exit_exec = self._execute_managed_order(
            side='SELL', token=trade['token'], symbol=trade['symbol'],
            ref_price=ref_price, quantity=qty_to_sell,
            timeout_secs=config.EXIT_FILL_TIMEOUT_SECS, chase_remaining=True,
        )
        sold_qty = int(exit_exec.get('filled_qty') or 0)
        remaining_qty = max(qty_to_sell - sold_qty, 0)
        if sold_qty <= 0:
            status = exit_exec.get('status') or 'unknown'
            reason = exit_exec.get('rej_reason') or ''
            extra = f" | reason: {reason}" if reason else ''
            print(f"  [Exit] No quantity sold yet for {trade['symbol']} (status={status}) — keeping trade active{extra}")
            logger.warning(f"Exit sell not executed for {trade['symbol']}: status={status} filled_qty=0 pending_qty={exit_exec.get('pending_qty')} reason={reason}")
            return
        if remaining_qty > 0:
            trade['filled_qty'] = remaining_qty
            trade['requested_qty'] = remaining_qty
            trade['pending_qty'] = 0
            print(f"  [Exit] Only {sold_qty} sold, {remaining_qty} still open — keeping watch on remaining qty")
            return

        exit_price = float(exit_exec.get('avg_price') or ref_price)
        qty        = qty_to_sell
        pnl_pts    = exit_price - trade['entry_price']
        pnl_rs     = pnl_pts * qty
        total_cost = _calc_trade_cost(trade['entry_price'], exit_price, qty)
        net_rs     = round(pnl_rs - total_cost, 2)
        won        = net_rs > 0

        self.cap_mgr.update_after_trade(net_rs)
        self.trades_today.append({
            **trade,
            'exit_price' : exit_price,
            'exit_time'  : now,
            'exit_reason': reason,
            'exit_order_id': exit_exec.get('order_id'),
            'exit_status': exit_exec.get('status'),
            'exit_rej_reason': exit_exec.get('rej_reason') or '',
            'sold_qty'   : sold_qty,
            'pnl_pts'    : round(pnl_pts, 2),
            'pnl_rs'     : round(pnl_rs, 2),
            'total_cost' : total_cost,
            'net_rs'     : net_rs,
            'won'        : won,
        })

        print(f"\n  {'='*55}")
        print(f"  {'WIN' if won else 'LOSS'} | "
              f"{reason.split('|')[0].strip()}")
        print(f"     Entry  : Rs {trade['entry_price']:.2f} @ "
              f"{trade['entry_time'].strftime('%H:%M:%S')}")
        print(f"     Exit   : Rs {exit_price:.2f} @ {now.strftime('%H:%M:%S')}")
        print(f"     Qty    : {qty}")
        print(f"     P&L    : {pnl_pts:+.2f} pts = Rs {pnl_rs:+.0f}")
        print(f"     Costs  : Rs {total_cost:.0f} "
              f"(STT+txn+brok+GST)")
        print(f"     Net    : Rs {net_rs:+.0f}")
        print(f"  {'='*55}\n")
        self.telegram.alert_exit(
            trade['direction'], trade['strike'],
            trade['entry_price'], exit_price,
            pnl_pts, net_rs, reason
        )

        self.eng.on_exit()
        self.active_trade = None

        if flip:
            # Flip triggered: current strike dropped below its VWAP,
            # opposite strike crossed above its VWAP.
            # Exit done above — now enter opposite direction immediately.
            prev_direction = trade['direction']
            new_direction  = 'PE' if prev_direction == 'CE' else 'CE'
            print(f"  [FLIP] {prev_direction} below VWAP + {new_direction} crossed VWAP")
            print(f"  [FLIP] Exited {prev_direction}, checking {new_direction} entry...")
            self.eng.last_signal_time = None   # bypass 10-min cooldown for flip
            self._check_entry(now)             # finds the opposite strike, sets active_trade
            if self.active_trade:
                print(f"  [FLIP] ✅ Entered {self.active_trade['direction']} {self.active_trade['strike']}")
            else:
                print(f"  [FLIP] ⚠️  No valid {new_direction} entry found — staying out")

    # ── Order placement ────────────────────────────────────
    def _recover_open_position(self):
        """On startup, check if there is already an open position from a previous run.
        If found, reconstruct active_trade so SL/exit monitoring resumes immediately.
        Prevents unmonitored open positions after crash or restart."""
        if config.PAPER_TRADE:
            return
        try:
            resp = self.client.positions()
            rows = resp if isinstance(resp, list) else (
                   resp.get('data') or resp.get('message') or [])
            for row in rows:
                sym      = str(row.get('trdSym') or row.get('trading_symbol') or '')
                net_qty  = int(float(row.get('netQty') or row.get('net_quantity') or 0))
                avg_prc  = float(row.get('avgPrc') or row.get('average_price') or 0)
                if net_qty == 0 or not sym:
                    continue
                # Identify CE or PE from symbol
                direction = 'CE' if sym.endswith('CE') else ('PE' if sym.endswith('PE') else None)
                if not direction:
                    continue
                # Find matching token
                token = None
                for tok, (strike, opt_type) in self.all_tokens.items():
                    sym_check = self.ce_symbols.get(strike) if direction == 'CE' else self.pe_symbols.get(strike)
                    if sym_check == sym:
                        token = tok
                        break
                if not token:
                    print(f"  [Recovery] ⚠️  Open position {sym} qty={net_qty} but token not found — MANUAL EXIT NEEDED")
                    logger.warning(f"Recovery: open position {sym} qty={net_qty} avg={avg_prc} — token not in tracked strikes")
                    continue
                vwap_obj = (self.eng.ce_strikes if direction == 'CE' else self.eng.pe_strikes).get(token)
                vwap_val = vwap_obj.get_vwap() if vwap_obj else 0.0
                sl_price = round((vwap_val - 5.0) if vwap_val > 0 else (avg_prc - 10.0), 2)
                self.eng.in_trade       = True
                self.eng.direction      = direction
                self.eng.active_token   = token
                self.eng.active_strike  = next((s for t,(s,o) in self.all_tokens.items() if t==token), 0)
                self.eng.entry_price    = avg_prc
                self.eng.entry_vwap     = vwap_val
                self.eng.sl_price       = sl_price
                self.eng.best_price     = avg_prc
                self.active_trade = {
                    'direction'  : direction,
                    'token'      : token,
                    'strike'     : self.eng.active_strike,
                    'symbol'     : sym,
                    'entry_price': avg_prc,
                    'entry_time' : datetime.datetime.now(),
                    'entry_vwap' : vwap_val,
                    'entry_dist' : 0.0,
                    'sl_price'   : sl_price,
                    'filled_qty' : abs(net_qty),
                    'order_id'   : 'RECOVERED',
                }
                print(f"  [Recovery] ✅ Resumed monitoring {direction} {sym} qty={net_qty} avg={avg_prc:.2f} SL={sl_price:.2f}")
                logger.info(f"Position recovered: {sym} qty={net_qty} avg={avg_prc} sl={sl_price}")
        except Exception as e:
            logger.debug(f"Position recovery check failed: {e}")

    def _on_session_reconnect(self, new_client):
        """Called by SessionManager when re-login succeeds.
        Updates self.client so all subsequent REST/order calls use the fresh session."""
        self.client = new_client
        if self.opt_mgr:
            self.opt_mgr.client = new_client
        print(f"  [Session] ✅ self.client updated after re-login")
        logger.info("self.client refreshed after session re-login")
        self.telegram.alert_session("Session re-login successful ✅ — algo continues")

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
            return str(resp.get('nOrdNo') or resp.get('order_id') or resp.get('orderId') or '')
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
            logger.debug(f"order_history({order_id}) failed: {e}")
        if not snapshot:
            try:
                rpt = self.client.order_report()
                rows = self._normalize_rows(rpt)
                for row in rows:
                    row_id = str(row.get('nOrdNo') or row.get('order_id') or row.get('orderId') or '')
                    if row_id == str(order_id):
                        snapshot = row
                        break
            except Exception as e:
                logger.debug(f"order_report lookup failed for {order_id}: {e}")
        status = str(snapshot.get('ordSt') or snapshot.get('stat') or snapshot.get('status') or '').lower()
        qty = self._safe_int(snapshot.get('qty') or snapshot.get('quantity') or snapshot.get('ordQty'), 0)
        filled_qty = self._safe_int(snapshot.get('fldQty') or snapshot.get('filled_quantity') or snapshot.get('fillQty'), 0)
        unfilled = self._safe_int(snapshot.get('unFldSz') or snapshot.get('pending_quantity') or max(qty - filled_qty, 0), 0)
        avg_price = self._safe_float(snapshot.get('avgPrc') or snapshot.get('avg_price') or snapshot.get('average_price'), 0.0)
        return {
            **snapshot,
            'order_id': str(order_id),
            'status': status,
            'qty': qty,
            'filled_qty': filled_qty,
            'pending_qty': unfilled,
            'avg_price': avg_price,
            'rej_reason': snapshot.get('rejRsn') or snapshot.get('rejReason') or snapshot.get('rejMsg') or snapshot.get('remarks') or snapshot.get('message') or '',
        }

    def _cancel_open_order(self, order_id, amo='NO'):
        if not order_id or config.PAPER_TRADE:
            return {'order_id': order_id, 'cancelled': False, 'response': None}
        last_error = None
        for kwargs in (
            {'order_id': order_id, 'amo': amo, 'isVerify': True},
            {'order_id': order_id, 'isVerify': True},
            {'order_id': order_id, 'amo': amo},
            {'order_id': order_id},
        ):
            try:
                resp = self.client.cancel_order(**kwargs)
                logger.info(f"cancel_order({kwargs}) -> {resp}")
                return {'order_id': order_id, 'cancelled': True, 'response': resp}
            except Exception as e:
                last_error = e
                logger.debug(f"cancel_order failed for {order_id} kwargs={kwargs}: {e}")
        return {'order_id': order_id, 'cancelled': False, 'error': str(last_error) if last_error else 'cancel failed'}

    def _place_order(self, side, token, symbol, price, quantity):
        # MKT is confirmed supported in Kotak Neo API docs.
        # Using MKT guarantees fill — no slippage risk from missed limit.
        if config.PAPER_TRADE:
            oid = f"PAPER_{side}_{datetime.datetime.now().strftime('%H%M%S')}"
            logger.info(f"PAPER {side} {symbol} ref={price:.2f} qty={quantity}")
            return {'order_id': oid, 'avg_price': float(price), 'amo': 'NO',
                    'response': {'stat': 'Ok', 'nOrdNo': oid}}

        tx_type = "B" if side == "BUY" else "S"
        now_t   = datetime.datetime.now().time()
        amo     = ("YES" if config.ENABLE_AMO_OUTSIDE_HOURS and
                   (now_t < datetime.time(9, 15) or now_t > datetime.time(15, 30))
                   else "NO")
        try:
            resp = self.client.place_order(
                exchange_segment   = config.FO_SEGMENT,
                product            = "NRML",
                trading_symbol     = symbol,
                transaction_type   = tx_type,
                quantity           = str(quantity),
                order_type         = "MKT",
                price              = "0",
                validity           = "DAY",
                amo                = amo,
                disclosed_quantity = "0",
                market_protection  = "0",
                pf                 = "N",
                trigger_price      = "0",
                tag                = f"VWAP_{side}",
            )
            oid = self._extract_order_id(resp)
            logger.info(f"LIVE {side} {symbol} ref={price:.2f} qty={quantity} amo={amo} | id={oid} | resp={resp}")
            return {'order_id': oid, 'avg_price': float(price), 'amo': amo, 'response': resp}
        except Exception as e:
            logger.error(f"Order error ({side} {symbol} qty={quantity}): {e}")
            return {'order_id': None, 'avg_price': 0.0, 'amo': amo, 'error': str(e)}

    def _wait_for_order_fill(self, order_id, expected_qty, timeout_secs, amo='NO'):
        if config.PAPER_TRADE:
            return {
                'order_id': order_id,
                'status': 'complete',
                'qty': expected_qty,
                'filled_qty': expected_qty,
                'pending_qty': 0,
                'avg_price': 0.0,
                'cancel_response': None,
            }
        deadline = time.time() + max(timeout_secs, 1)
        last = {'order_id': order_id, 'status': 'unknown', 'qty': expected_qty, 'filled_qty': 0, 'pending_qty': expected_qty, 'avg_price': 0.0}
        terminal = {'complete', 'completed', 'traded', 'cancelled', 'canceled', 'rejected'}
        while time.time() < deadline:
            snap = self._get_order_snapshot(order_id)
            if snap:
                last = snap
                status = str(snap.get('status') or '').lower()
                filled = int(snap.get('filled_qty') or 0)
                pending = int(snap.get('pending_qty') or max(expected_qty - filled, 0))
                if filled >= expected_qty or pending <= 0 or status in terminal:
                    if status in {'rejected', 'cancelled', 'canceled'} or (filled <= 0 and pending <= 0):
                        logger.warning(f"Order terminal state order_id={order_id} status={status} qty={expected_qty} filled={filled} pending={pending} reason={snap.get('rej_reason', '')}")
                    return last
            time.sleep(config.ORDER_STATUS_POLL_SECS)
        filled = int(last.get('filled_qty') or 0)
        pending = max(expected_qty - filled, 0)
        if pending > 0:
            cancel_info = self._cancel_open_order(order_id, amo=amo)
            last['cancel_response'] = cancel_info
            time.sleep(1)
            snap = self._get_order_snapshot(order_id)
            if snap:
                last = {**last, **snap, 'cancel_response': cancel_info}
        return last

    def _execute_managed_order(self, side, token, symbol, ref_price, quantity, timeout_secs, chase_remaining=False):
        if quantity <= 0:
            return {'filled_qty': 0, 'avg_price': 0.0, 'pending_qty': 0, 'status': 'skipped'}
        if config.PAPER_TRADE:
            fill_price = round(float(ref_price) + (1.0 if side == 'BUY' else -1.0), 2)
            return {
                'order_id': f"PAPER_{side}_{datetime.datetime.now().strftime('%H%M%S')}",
                'filled_qty': quantity,
                'pending_qty': 0,
                'status': 'complete',
                'avg_price': fill_price,
                'last_limit_price': float(ref_price),
                'attempts': 1,
            }

        remaining = int(quantity)
        total_filled = 0
        total_value = 0.0
        final_order_id = None
        final_status = 'unknown'
        final_rej_reason = ''
        last_limit_price = None
        attempts = 1 if (side == 'BUY' or not chase_remaining) else max(1, config.EXIT_RETRY_ATTEMPTS)

        for attempt in range(1, attempts + 1):
            live_ref = ref_price
            if side == 'SELL':
                live_ref = self.eng.get_ltp(token) or ref_price
            placed = self._place_order(side, token, symbol, live_ref, remaining)
            final_order_id = placed.get('order_id')
            last_limit_price = placed.get('limit_price')
            if not final_order_id:
                final_status = 'place_failed'
                break
            snap = self._wait_for_order_fill(final_order_id, remaining, timeout_secs, amo=placed.get('amo', 'NO'))
            filled_now = self._safe_int(snap.get('filled_qty'), 0)
            avg_now = self._safe_float(snap.get('avg_price'), 0.0)
            final_status = snap.get('status') or final_status
            final_rej_reason = snap.get('rej_reason') or final_rej_reason
            if filled_now > 0:
                total_filled += filled_now
                total_value += filled_now * (avg_now or live_ref)
            remaining = max(quantity - total_filled, 0)
            logger.info(f"{side} managed order attempt={attempt} symbol={symbol} filled_now={filled_now} total_filled={total_filled} remaining={remaining} snap={snap}")
            if remaining <= 0 or side == 'BUY' or not chase_remaining:
                break
        avg_price = round(total_value / total_filled, 2) if total_filled > 0 else 0.0
        return {
            'order_id': final_order_id,
            'filled_qty': total_filled,
            'pending_qty': remaining,
            'status': final_status,
            'avg_price': avg_price,
            'last_limit_price': last_limit_price,
            'attempts': attempts,
            'rej_reason': final_rej_reason,
        }

    # ── EOD Analysis ───────────────────────────────────────
    def save_daily_analysis(self):
        today = datetime.date.today()
        os.makedirs("reports", exist_ok=True)
        fname = f"reports/daily_{today.strftime('%Y%m%d')}.txt"

        total_pnl = sum(t['net_rs'] for t in self.trades_today)
        wins      = sum(1 for t in self.trades_today if t['won'])
        losses    = len(self.trades_today) - wins
        win_rate  = wins/len(self.trades_today)*100 if self.trades_today else 0

        lines = []
        lines.append("="*62)
        lines.append(f"  DAILY ANALYSIS — {today.strftime('%d %b %Y')} "
                     f"({'PAPER' if config.PAPER_TRADE else 'LIVE'})")
        lines.append("="*62)
        lines.append(f"  Expiry     : {self.expiry_date}")
        lines.append(f"  India VIX  : {self.today_vix:.2f}")
        lines.append(f"  Gap        : {self.gap_pct:.1f}%")
        lines.append(f"  ATM strike : {self.eng.atm_strike}")
        lines.append(f"  CE strikes : {sorted(self.ce_tokens.keys())}")
        lines.append(f"  PE strikes : {sorted(self.pe_tokens.keys())}")
        lines.append("")
        lines.append(f"  Trades     : {len(self.trades_today)}")
        lines.append(f"  Wins       : {wins} | Losses: {losses}")
        lines.append(f"  Win Rate   : {win_rate:.0f}%")
        lines.append(f"  Total P&L  : Rs {total_pnl:+.0f} (net of costs)")
        lines.append("")
        lines.append("  TRADE LOG:")
        lines.append("  " + "-"*55)

        for i, t in enumerate(self.trades_today, 1):
            result = "WIN" if t['won'] else "LOSS"
            lines.append(
                f"  #{i} {result} | {t['direction']} {t['strike']} | "
                f"Entry {t['entry_time'].strftime('%H:%M:%S')} "
                f"@ {t['entry_price']:.2f} "
                f"(VWAP={t['entry_vwap']:.2f} "
                f"dist={t['entry_dist']:.1f} qty={t.get('filled_qty', 0)}) | "
                f"Exit {t['exit_time'].strftime('%H:%M:%S')} "
                f"@ {t['exit_price']:.2f} | "
                f"Reason: {t['exit_reason'].split('|')[0].strip()} | "
                f"Net: Rs {t['net_rs']:+.0f}"
            )

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
        print(f"\n  [Report] Saved to {fname}")

    # ── Main run loop ──────────────────────────────────────
    def run(self):
        self.initialize()
        # NOTE: setup_websocket() is already called inside initialize().
        # Do NOT call it again here — that creates a duplicate WS connection.

        subscribed  = False
        last_status = None

        print("\n[*] Running. Ctrl+C to stop.\n")

        try:
            while self.is_running:
                now = datetime.datetime.now()
                t   = now.time()

                # Change 4: Re-fetch spot at 9:12 AM and update strikes
                # Algo starts at 9:00 AM with yesterday's closing price.
                # By 9:12 AM pre-open session has real indicative prices.
                # Re-check spot and pick correct ITM strikes before market opens.
                if (t >= datetime.time(9, 12)
                        and t < datetime.time(9, 15)
                        and not getattr(self, '_preopen_refreshed', False)):
                    self._preopen_refreshed = True
                    spot = self._get_nifty_spot()
                    if spot > 0:
                        new_atm = round(spot / STRIKE_STEP) * STRIKE_STEP
                        old_atm = self.eng.atm_strike
                        print(f"  [9:12 Refresh] Nifty={spot:.0f} "
                              f"ATM: {old_atm}→{new_atm}")
                        if new_atm != old_atm:
                            self._setup_strikes(spot, force=True)
                            print(f"  [9:12 Refresh] ✅ Strikes updated before market open")
                        else:
                            print(f"  [9:12 Refresh] ATM unchanged — no strike change needed")

                # Subscribe at 9:15 AM
                if (t >= datetime.time(9, 15)
                        and t < datetime.time(15, 25)
                        and not subscribed):
                    self.subscribe_options()
                    subscribed = True
                    gap_msg = (f"GAP {self.gap_pct:.1f}% > 1.5% — "
                               f"entries from 9:45 AM"
                               if self.gap_pct > 1.5
                               else "watching for signals from 9:40 AM")
                    print(f"  Market open — {gap_msg}\n")
                    self._start_hourly_strike_refresh()

                # At 9:25 AM — drop to best 2 tokens (1 ITM CE + 1 ITM PE)
                # Also runs if algo restarted after 9:26 AM
                if (t >= datetime.time(9, 25)
                        and subscribed
                        and not getattr(self, '_trimmed', False)):
                    self._trim_to_best_strikes()

                if t >= datetime.time(15, 25) and self.active_trade:
                    with self._lock:
                        self._check_exit(now)

                # WS watchdog
                if (subscribed and self.last_tick_time
                        and t < datetime.time(15, 25)):
                    secs = (now - self.last_tick_time).total_seconds()
                    if secs > 60 and self.ws_connected:
                        self.ws_connected = False
                        print(f"\n  [WS] No tick for {secs:.0f}s — reconnecting...")
                        self._trigger_reconnect()

                # Status every 5 mins
                minute = now.minute
                if minute % 5 == 0 and minute != last_status:
                    last_status = minute
                    ws_icon = "[OK]" if self.ws_connected else "[DISCONNECTED]"
                    print(f"  [{now.strftime('%H:%M')}] WS:{ws_icon} | "
                          f"Trades:{len(self.trades_today)} | "
                          f"VIX:{self.today_vix:.1f} | "
                          f"{self.eng.get_status()}")

                if t >= datetime.time(15, 26) and subscribed:
                    break

                # ── TradingView VWAP resync every 60s ────────
                if (subscribed
                        and t >= datetime.time(9, 15)
                        and t < datetime.time(15, 25)
                        and (self.last_vwap_sync is None or
                             (now - self.last_vwap_sync).total_seconds()
                             >= self.vwap_sync_secs)):
                    self._sync_exchange_vwap()
                    self._nse_sync()
                    self.last_vwap_sync = now

                time.sleep(1)

        except KeyboardInterrupt:
            print("\n\n[*] Stopped by user.")
            if self.active_trade:
                print("[*] Forcing exit on open trade...")
                now = datetime.datetime.now()
                with self._lock:
                    trade      = self.active_trade
                    exit_price = self.eng.get_ltp(trade['token'])
                    if exit_price <= 0:
                        exit_price = trade['entry_price']
                    pnl_pts    = exit_price - trade['entry_price']
                    qty        = int(trade.get('filled_qty') or config.LOT_SIZE)
                    pnl_rs     = pnl_pts * qty
                    total_cost = _calc_trade_cost(trade['entry_price'],
                                                  exit_price, qty)
                    net_rs     = round(pnl_rs - total_cost, 2)
                    won        = net_rs > 0
                    self.cap_mgr.update_after_trade(net_rs)
                    self.trades_today.append({
                        **trade,
                        'exit_price' : exit_price,
                        'exit_time'  : now,
                        'exit_reason': 'Stopped by user',
                        'pnl_pts'    : round(pnl_pts, 2),
                        'pnl_rs'     : round(pnl_rs, 2),
                        'total_cost' : total_cost,
                        'net_rs'     : net_rs,
                        'won'        : won,
                    })
                    self.active_trade = None
                    print(f"  Forced exit @ {exit_price:.2f} | "
                          f"Net P&L = Rs {net_rs:+.0f}")

        finally:
            self.is_running = False
            if self.sess_mgr:
                self.sess_mgr.stop()
            print("\n[*] Saving daily analysis...")
            self.save_daily_analysis()
            net_today = sum(t['net_rs'] for t in self.trades_today)
            self.telegram.alert_shutdown(len(self.trades_today), net_today)
            print("[*] Done. Goodbye")


if __name__ == '__main__':
    algo = NiftyOptionsAlgo()
    algo.run()
