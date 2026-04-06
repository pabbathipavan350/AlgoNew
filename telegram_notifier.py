# ============================================================
# TELEGRAM_NOTIFIER.PY — Optional Trade Alerts via Telegram
# ============================================================
# Sends a Telegram message when:
#   - Trade entry taken
#   - SL hit / target hit / trade exited
#   - Daily loss limit hit
#   - Session reconnect happens
#   - Circuit breaker suspected
#
# SETUP (one time):
#   1. Message @BotFather on Telegram → /newbot → copy the token
#   2. Message your bot once, then open:
#      https://api.telegram.org/bot<TOKEN>/getUpdates
#      Copy the "chat_id" number from the response
#   3. Add to your .env file:
#      TELEGRAM_BOT_TOKEN=your_token_here
#      TELEGRAM_CHAT_ID=your_chat_id_here
#
# If token/chat_id not set → silently disabled, algo runs normally.
# ============================================================

import os
import threading
import logging

logger = logging.getLogger(__name__)

try:
    import urllib.request
    import urllib.parse
    _URLLIB_OK = True
except ImportError:
    _URLLIB_OK = False


class TelegramNotifier:
    """
    Sends Telegram messages in a background thread so it never
    blocks the main algo loop — even if Telegram is slow/down.
    """

    def __init__(self):
        self.token   = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
        self.chat_id = os.getenv('TELEGRAM_CHAT_ID', '').strip()
        self.enabled = bool(self.token and self.chat_id and _URLLIB_OK)

        if self.enabled:
            print(f"  [Telegram] ✅ Alerts enabled (chat_id={self.chat_id})")
        else:
            print(f"  [Telegram] ⚠️  Alerts disabled "
                  f"(set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env to enable)")

    def send(self, message: str):
        """Send message in background thread — non-blocking."""
        if not self.enabled:
            return
        threading.Thread(
            target=self._send_sync,
            args=(message,),
            daemon=True,
            name='TelegramSend'
        ).start()

    def _send_sync(self, message: str):
        """Actual HTTP call — runs in background thread."""
        try:
            url  = f"https://api.telegram.org/bot{self.token}/sendMessage"
            data = urllib.parse.urlencode({
                'chat_id'    : self.chat_id,
                'text'       : message,
                'parse_mode' : 'HTML',
            }).encode('utf-8')
            req  = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req, timeout=8) as resp:
                if resp.status != 200:
                    logger.debug(f"Telegram HTTP {resp.status}")
        except Exception as e:
            logger.debug(f"Telegram send failed: {e}")

    # ── Pre-formatted alert helpers ──────────────────────────

    def alert_entry(self, direction, strike, entry_price, vwap, sl, target, qty):
        emoji = "🟢" if direction == 'CE' else "🔴"
        msg = (
            f"{emoji} <b>ENTRY — {direction} {strike}</b>\n"
            f"Price  : ₹{entry_price:.2f}\n"
            f"VWAP   : ₹{vwap:.2f}\n"
            f"SL     : ₹{sl:.2f}\n"
            f"Target : ₹{target:.2f}\n"
            f"Qty    : {qty}"
        )
        self.send(msg)

    def alert_exit(self, direction, strike, entry_price, exit_price, pnl_pts, net_rs, reason):
        emoji = "✅" if net_rs >= 0 else "❌"
        msg = (
            f"{emoji} <b>EXIT — {direction} {strike}</b>\n"
            f"Entry  : ₹{entry_price:.2f}\n"
            f"Exit   : ₹{exit_price:.2f}\n"
            f"P&amp;L   : {pnl_pts:+.1f} pts = ₹{net_rs:+.0f}\n"
            f"Reason : {reason.split('|')[0].strip()}"
        )
        self.send(msg)

    def alert_risk(self, message: str):
        self.send(f"⚠️ <b>RISK ALERT</b>\n{message}")

    def alert_session(self, message: str):
        self.send(f"🔄 <b>SESSION</b>\n{message}")

    def alert_startup(self, mode, expiry, atm):
        self.send(
            f"🚀 <b>ALGO STARTED</b>\n"
            f"Mode   : {mode}\n"
            f"Expiry : {expiry}\n"
            f"ATM    : {atm}"
        )

    def alert_shutdown(self, trades, net_pnl):
        emoji = "✅" if net_pnl >= 0 else "❌"
        self.send(
            f"{emoji} <b>ALGO STOPPED</b>\n"
            f"Trades : {trades}\n"
            f"Net P&amp;L : ₹{net_pnl:+.0f}"
        )
