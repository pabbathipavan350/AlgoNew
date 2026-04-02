# ============================================================
# GTT_MANAGER.PY — Broker-Level Stop Loss (Power Failure Fix)
# ============================================================
# FIX #2: Power/Internet outage protection
#
# PROBLEM:
#   If your laptop dies or internet cuts while in a trade,
#   the position stays open at the exchange with NO stop loss.
#   Nifty can fall 200 pts and your option goes to zero.
#
# SOLUTION — Two-layer SL:
#   Layer 1 (Software): Our VWAP-based trailing SL in the algo
#   Layer 2 (Broker):   GTT stop order placed at Kotak server
#                       Fires even if your laptop is OFF
#
# HOW IT WORKS:
#   On entry → immediately place GTT sell order at hard SL price
#              (option entry price - 20 pts as safety net)
#   On exit  → cancel the GTT order
#   On trail → update GTT order as trail SL rises
#
# NOTE: Kotak Neo API v2 uses place_order with SL-M type
#       for stop-loss market orders (closest to GTT available)
#       AMO=NO, trigger_price = SL level
# ============================================================

import logging
import config

logger = logging.getLogger(__name__)

# Hard SL in option points — safety net only
# Algo's VWAP SL will normally fire first
# GTT fires only if algo is completely offline
HARD_SL_BELOW_ENTRY = 20   # option drops 20 pts from entry → GTT fires


class GTTManager:

    def __init__(self, client):
        self.client       = client
        self.gtt_order_id = None   # current GTT order ID
        self.gtt_sl_price = None   # current GTT trigger price

    def place_gtt_sl(self, trading_symbol, qty, option_entry_price):
        """
        Place a stop-loss market order at broker level.
        Trigger price = option_entry - HARD_SL_BELOW_ENTRY pts
        This is the safety net if algo goes offline.

        In Kotak Neo: SL-M order with trigger_price acts as GTT SL.
        """
        sl_trigger = round(option_entry_price - HARD_SL_BELOW_ENTRY, 1)
        sl_trigger = max(sl_trigger, 1.0)   # never below Rs 1

        print(f"  [GTT] Placing broker SL at Rs {sl_trigger:.1f} "
              f"(entry={option_entry_price:.1f} - {HARD_SL_BELOW_ENTRY} pts)")

        if config.PAPER_TRADE:
            self.gtt_order_id = f"GTT_PAPER_{trading_symbol}"
            self.gtt_sl_price = sl_trigger
            print(f"  [GTT] Paper mode — SL noted at Rs {sl_trigger:.1f}")
            return True

        try:
            resp = self.client.place_order(
                exchange_segment  = config.FO_SEGMENT,
                product           = "MIS",
                price             = "0",
                order_type        = "SL-M",     # Stop Loss Market
                quantity          = str(qty),
                validity          = "DAY",
                trading_symbol    = trading_symbol,
                transaction_type  = "S",        # Sell to exit long
                amo               = "NO",
                disclosed_quantity= "0",
                market_protection = "0",
                pf                = "N",
                trigger_price     = str(sl_trigger),
                tag               = "GTT_SL"
            )

            if resp and resp.get('stat') == 'Ok':
                self.gtt_order_id = resp.get('nOrdNo', '')
                self.gtt_sl_price = sl_trigger
                print(f"  [GTT] ✅ Broker SL placed | "
                      f"OrderID={self.gtt_order_id} | "
                      f"Trigger=Rs {sl_trigger:.1f}")
                logger.info(f"GTT SL placed: {trading_symbol} "
                           f"trigger={sl_trigger} id={self.gtt_order_id}")
                return True
            else:
                print(f"  [GTT] ⚠️  SL order failed: {resp}")
                logger.warning(f"GTT SL failed: {resp}")
                return False

        except Exception as e:
            print(f"  [GTT] ⚠️  SL order error: {e}")
            logger.error(f"GTT SL error: {e}")
            return False

    def cancel_gtt_sl(self):
        """Cancel the broker SL order when algo exits normally."""
        if not self.gtt_order_id:
            return

        if config.PAPER_TRADE:
            print(f"  [GTT] Paper — SL cancelled")
            self.gtt_order_id = None
            self.gtt_sl_price = None
            return

        try:
            resp = self.client.cancel_order(order_id=self.gtt_order_id)
            if resp and resp.get('stat') == 'Ok':
                print(f"  [GTT] ✅ Broker SL cancelled (algo exited normally)")
            else:
                # Order may have already fired or been filled
                print(f"  [GTT] Note: SL cancel response: {resp}")
        except Exception as e:
            logger.debug(f"GTT cancel note: {e}")
        finally:
            self.gtt_order_id = None
            self.gtt_sl_price = None

    def update_gtt_sl(self, trading_symbol, qty, new_sl_price):
        """
        Update broker SL as trail moves up.
        Cancel old order → place new one at higher price.
        Only updates if new SL is meaningfully higher (> 5 pts).
        """
        if not self.gtt_order_id:
            return
        if self.gtt_sl_price and new_sl_price <= self.gtt_sl_price + 5:
            return   # not worth updating for small moves

        # Cancel old, place new
        self.cancel_gtt_sl()
        self.place_gtt_sl(trading_symbol, qty, new_sl_price + HARD_SL_BELOW_ENTRY)

    @property
    def is_active(self):
        return self.gtt_order_id is not None
