# ============================================================
# REPORT_MANAGER.PY — Comprehensive Analytics & Reporting
# ============================================================
# Captures everything needed for deep analysis:
#
# Per trade:
#   - Entry/exit time, duration
#   - Nifty price at entry/exit, VWAP at entry
#   - Distance from VWAP at entry (quality measure)
#   - Option entry/exit/peak price
#   - Points gained, max possible (peak-entry)
#   - Trail efficiency % (how much of the move we captured)
#   - Exit reason (SL hit / trail SL / breakeven / squareoff)
#   - SL analysis: why SL hit (VWAP crossed? option dropped?)
#   - Time in trade (minutes)
#   - VIX at entry
#
# Daily report:
#   - Trade count, timing pattern
#   - Win/loss with reasons
#   - Best/worst trade
#   - Avg hold time
#   - Signal quality (how tight entry zone was)
#   - Day type: Trending / Choppy / Gap
#   - % return on capital
#
# Weekly/cumulative:
#   - Running P&L chart (text-based)
#   - Win rate trend
#   - Best trading hours
#   - CE vs PE performance
# ============================================================

import csv
import os
import json
import datetime
import config


class ReportManager:

    def __init__(self, capital_manager):
        self.cap_mgr    = capital_manager
        self.today      = datetime.date.today()
        self.trades     = []
        self.signals_fired   = 0    # total signals today
        self.signals_skipped = 0    # skipped (slippage/zone/gap)
        self.vix_at_open     = 0.0
        self._init_trade_log()
        self._init_daily_log()

    def set_vix(self, vix):
        self.vix_at_open = vix

    def signal_fired(self):
        self.signals_fired += 1

    def signal_skipped(self, reason):
        self.signals_skipped += 1

    # ── Trade log CSV (master — all days) ─────────────────
    def _init_trade_log(self):
        exists = os.path.exists(config.TRADE_LOG_FILE)
        self._log    = open(config.TRADE_LOG_FILE, 'a',
                            newline='', encoding='utf-8')
        self._writer = csv.writer(self._log)
        if not exists:
            self._writer.writerow([
                # Identity
                'Date', 'Trade#',
                # Timing
                'Entry Time', 'Exit Time', 'Duration Mins',
                # Signal
                'Direction', 'Signal Type',
                'Strike', 'Expiry',
                # Nifty levels
                'Nifty Entry', 'Nifty Exit', 'Nifty Move',
                'VWAP at Entry', 'Dist from VWAP (pts)',
                # Option prices
                'Option Entry', 'Option Peak', 'Option Exit',
                'Pts Gained', 'Pts Max Possible', 'Trail Efficiency %',
                # P&L
                'Gross PnL', 'Cost', 'Net PnL',
                'Return on Capital %',
                # Exit analysis
                'Exit Reason', 'Exit Category',
                'Breakeven Triggered', 'Trail Triggered',
                # Market context
                'VIX', 'Capital After',
                # Mode
                'Mode'
            ])

    # ── Daily summary JSON log ────────────────────────────
    def _init_daily_log(self):
        self.daily_log_file = "daily_summary.json"
        self._daily_history = []
        if os.path.exists(self.daily_log_file):
            try:
                with open(self.daily_log_file, 'r') as f:
                    self._daily_history = json.load(f)
            except Exception:
                self._daily_history = []

    # ── Log a completed trade ──────────────────────────────
    def log_trade(self, trade):
        """Log trade with full analytics."""
        self.trades.append(trade)

        # Compute analytics
        entry_dt = datetime.datetime.strptime(
            f"{trade['date']} {trade['entry_time']}", '%Y-%m-%d %H:%M:%S')
        exit_dt  = datetime.datetime.strptime(
            f"{trade['date']} {trade['exit_time']}", '%Y-%m-%d %H:%M:%S')
        duration = round((exit_dt - entry_dt).total_seconds() / 60, 1)

        nifty_move     = round(trade['nifty_exit'] - trade['nifty_entry'], 2)
        dist_from_vwap = round(abs(trade['nifty_entry'] - trade['vwap_entry']), 2)
        pts_gained     = round(trade['option_exit'] - trade['option_entry'], 2)
        pts_max        = round(trade.get('option_peak', trade['option_exit'])
                               - trade['option_entry'], 2)
        trail_eff      = round((pts_gained / pts_max * 100)
                               if pts_max > 0 else 0, 1)
        roi_pct        = round(trade['net_pnl'] /
                               self.cap_mgr.state['deployed_capital'] * 100, 3)

        # Exit category for analysis
        reason = trade['exit_reason']
        if 'Trail SL' in reason:
            exit_cat = 'Trail SL'
        elif 'Nifty SL' in reason and 'BREAKEVEN' in reason:
            exit_cat = 'Breakeven SL'
        elif 'Nifty SL' in reason:
            exit_cat = 'Hard SL'
        elif 'Square' in reason:
            exit_cat = 'Square Off'
        elif 'emergency' in reason.lower() or 'disconnect' in reason.lower():
            exit_cat = 'Emergency Exit'
        else:
            exit_cat = 'Other'

        be_triggered    = 'YES' if trade.get('breakeven_triggered') else 'NO'
        trail_triggered = 'YES' if trade.get('trail_triggered') else 'NO'

        mode = 'PAPER' if config.PAPER_TRADE else 'LIVE'

        self._writer.writerow([
            trade['date'],
            len(self.trades),
            trade['entry_time'],
            trade['exit_time'],
            duration,
            trade['direction'],
            trade.get('signal_type', 'CROSS'),
            trade['strike'],
            trade['expiry'],
            trade['nifty_entry'],
            trade['nifty_exit'],
            nifty_move,
            trade['vwap_entry'],
            dist_from_vwap,
            trade['option_entry'],
            trade.get('option_peak', trade['option_exit']),
            trade['option_exit'],
            pts_gained,
            pts_max,
            trail_eff,
            f"{trade['gross_pnl']:+.2f}",
            f"{trade['cost']:.2f}",
            f"{trade['net_pnl']:+.2f}",
            roi_pct,
            trade['exit_reason'],
            exit_cat,
            be_triggered,
            trail_triggered,
            self.vix_at_open,
            f"{trade['capital_after']:,.0f}",
            mode,
        ])
        self._log.flush()

    # ── Daily report ──────────────────────────────────────
    def generate_daily_report(self):
        today_str = self.today.strftime('%d%m%Y')
        filename  = f"daily_report_{today_str}.txt"
        cap       = self.cap_mgr.get_summary()

        total      = len(self.trades)
        winners    = [t for t in self.trades if t['net_pnl'] > 0]
        losers     = [t for t in self.trades if t['net_pnl'] <= 0]
        be_exits   = [t for t in self.trades if 'BREAKEVEN' in t.get('exit_reason','')]
        trail_exits= [t for t in self.trades if 'Trail SL' in t.get('exit_reason','')]
        sl_exits   = [t for t in self.trades
                      if 'Nifty SL' in t.get('exit_reason','')
                      and 'BREAKEVEN' not in t.get('exit_reason','')]
        sq_exits   = [t for t in self.trades if 'Square' in t.get('exit_reason','')]

        net_pnl    = sum(t['net_pnl']   for t in self.trades)
        gross_pnl  = sum(t['gross_pnl'] for t in self.trades)
        total_cost = sum(t['cost']       for t in self.trades)
        win_rate   = (len(winners)/total*100) if total > 0 else 0
        day_roi    = (net_pnl / cap['deployed'] * 100) if cap['deployed'] > 0 else 0

        ce_trades  = [t for t in self.trades if t['direction']=='CE']
        pe_trades  = [t for t in self.trades if t['direction']=='PE']
        ce_pnl     = sum(t['net_pnl'] for t in ce_trades)
        pe_pnl     = sum(t['net_pnl'] for t in pe_trades)

        # Duration analysis
        durations = []
        for t in self.trades:
            try:
                e = datetime.datetime.strptime(
                    f"{t['date']} {t['entry_time']}", '%Y-%m-%d %H:%M:%S')
                x = datetime.datetime.strptime(
                    f"{t['date']} {t['exit_time']}", '%Y-%m-%d %H:%M:%S')
                durations.append((x-e).total_seconds()/60)
            except Exception:
                pass
        avg_duration = round(sum(durations)/len(durations), 1) if durations else 0

        # Trail efficiency
        trail_effs = []
        for t in self.trades:
            peak   = t.get('option_peak', t['option_exit'])
            gained = t['option_exit'] - t['option_entry']
            maxp   = peak - t['option_entry']
            if maxp > 0:
                trail_effs.append(gained/maxp*100)
        avg_trail_eff = round(sum(trail_effs)/len(trail_effs), 1) if trail_effs else 0

        # Best/worst
        best  = max(self.trades, key=lambda t: t['net_pnl']) if self.trades else None
        worst = min(self.trades, key=lambda t: t['net_pnl']) if self.trades else None

        # Day type classification
        if total == 0:
            day_type = "NO SIGNALS"
        elif len(sl_exits) >= 2:
            day_type = "CHOPPY (multiple SL hits)"
        elif net_pnl > cap['deployed'] * 0.05:
            day_type = "TRENDING (profitable)"
        elif net_pnl < 0:
            day_type = "BAD DAY (net loss)"
        else:
            day_type = "NORMAL"

        L = []
        L.append("=" * 62)
        L.append(f"  NIFTY OPTIONS ALGO — DAILY REPORT")
        L.append(f"  Date    : {self.today.strftime('%A, %d %B %Y')}")
        L.append(f"  Mode    : {'PAPER TRADE' if config.PAPER_TRADE else '*** LIVE ***'}")
        L.append(f"  VIX     : {self.vix_at_open:.1f}")
        L.append(f"  Day Type: {day_type}")
        L.append("=" * 62)

        L.append("")
        L.append("  SIGNAL ANALYSIS")
        L.append("  " + "─" * 50)
        L.append(f"  Signals generated : {self.signals_fired}")
        L.append(f"  Signals skipped   : {self.signals_skipped}  "
                 f"(zone/slippage/gap filter)")
        L.append(f"  Trades taken      : {total}")
        L.append(f"  Signal → Trade %  : "
                 f"{(total/self.signals_fired*100):.0f}%" 
                 if self.signals_fired > 0 else "  Signal → Trade %  : N/A")

        L.append("")
        L.append("  TRADE SUMMARY")
        L.append("  " + "─" * 50)
        L.append(f"  Total Trades   : {total}")
        L.append(f"  Winners        : {len(winners)}  "
                 f"({win_rate:.1f}%)")
        L.append(f"  Losers         : {len(losers)}")
        L.append(f"  CE Trades      : {len(ce_trades)}  "
                 f"(P&L: Rs {ce_pnl:+,.0f})")
        L.append(f"  PE Trades      : {len(pe_trades)}  "
                 f"(P&L: Rs {pe_pnl:+,.0f})")
        L.append(f"  Avg hold time  : {avg_duration} mins")
        L.append(f"  Trail efficiency: {avg_trail_eff}%  "
                 f"(how much of the move we captured)")

        L.append("")
        L.append("  EXIT ANALYSIS  ← why each trade ended")
        L.append("  " + "─" * 50)
        L.append(f"  Trail SL hit   : {len(trail_exits)}  "
                 f"(good — rode the trend)")
        L.append(f"  Breakeven SL   : {len(be_exits)}  "
                 f"(ok — protected capital)")
        L.append(f"  Hard SL hit    : {len(sl_exits)}  "
                 f"(VWAP broke against us)")
        L.append(f"  Square off     : {len(sq_exits)}  "
                 f"(held till 3:15 PM)")

        if sl_exits:
            L.append("")
            L.append("  SL HIT ANALYSIS  ← why SL fired")
            L.append("  " + "─" * 50)
            for t in sl_exits:
                dist  = abs(t['nifty_entry'] - t['vwap_entry'])
                nmove = t['nifty_exit'] - t['nifty_entry']
                L.append(f"  Trade #{self.trades.index(t)+1}: "
                         f"{t['direction']} entered {t['entry_time'][:5]} | "
                         f"Entry dist from VWAP: {dist:.1f} pts | "
                         f"Nifty moved: {nmove:+.0f} pts | "
                         f"Loss: Rs {t['net_pnl']:+.0f}")
                L.append(f"    Reason: {t['exit_reason']}")

        L.append("")
        L.append("  P&L SUMMARY")
        L.append("  " + "─" * 50)
        L.append(f"  Gross P&L      : Rs {gross_pnl:>+10,.2f}")
        L.append(f"  Total Costs    : Rs {total_cost:>10,.2f}  "
                 f"(Rs 340 × {total} trades)")
        L.append(f"  NET P&L TODAY  : Rs {net_pnl:>+10,.2f}")
        L.append(f"  Day Return     : {day_roi:>+.2f}%  "
                 f"on Rs {cap['deployed']:,.0f} deployed")

        if best and worst:
            L.append("")
            L.append("  BEST / WORST TRADE")
            L.append("  " + "─" * 50)
            L.append(f"  Best  : #{self.trades.index(best)+1}  "
                     f"{best['direction']} {best['entry_time'][:5]} → "
                     f"{best['exit_time'][:5]}  "
                     f"Rs {best['net_pnl']:+,.0f}  "
                     f"| {best['exit_reason'][:35]}")
            L.append(f"  Worst : #{self.trades.index(worst)+1}  "
                     f"{worst['direction']} {worst['entry_time'][:5]} → "
                     f"{worst['exit_time'][:5]}  "
                     f"Rs {worst['net_pnl']:+,.0f}  "
                     f"| {worst['exit_reason'][:35]}")

        L.append("")
        L.append("  TRADE-BY-TRADE DETAIL")
        L.append("  " + "─" * 50)
        if self.trades:
            for i, t in enumerate(self.trades, 1):
                peak    = t.get('option_peak', t['option_exit'])
                pts     = t['option_exit'] - t['option_entry']
                max_pts = peak - t['option_entry']
                eff     = round(pts/max_pts*100,1) if max_pts>0 else 0
                dur     = durations[i-1] if i-1 < len(durations) else 0
                sign    = "✓" if t['net_pnl'] > 0 else "✗"
                L.append(f"  {sign} #{i:02d} | {t['direction']} {t['strike']} | "
                         f"{t['entry_time'][:5]}-{t['exit_time'][:5]} "
                         f"({dur:.0f}min)")
                L.append(f"       Nifty: {t['nifty_entry']:.0f}→{t['nifty_exit']:.0f} "
                         f"(VWAP was {t['vwap_entry']:.0f}) | "
                         f"Option: {t['option_entry']:.0f}→"
                         f"peak {peak:.0f}→exit {t['option_exit']:.0f}")
                L.append(f"       Points: {pts:+.0f} of {max_pts:.0f} possible "
                         f"({eff:.0f}% captured) | "
                         f"Net: Rs {t['net_pnl']:+,.0f} | "
                         f"Exit: {t['exit_reason'][:40]}")
        else:
            L.append("  No trades today.")

        L.append("")
        L.append("  CAPITAL STATUS")
        L.append("  " + "─" * 50)
        L.append(f"  Starting capital : Rs {cap['initial']:>10,.0f}")
        L.append(f"  Current capital  : Rs {cap['current']:>10,.0f}")
        L.append(f"  Deployed capital : Rs {cap['deployed']:>10,.0f}")
        L.append(f"  Overall P&L      : Rs {cap['total_pnl']:>+10,.0f}")
        L.append(f"  Overall ROI      : {cap['roi_pct']:>+.2f}%")
        L.append(f"  Running since    : {cap['start']}")

        # Running P&L sparkline (text-based)
        if len(self._daily_history) > 0:
            L.append("")
            L.append("  RUNNING P&L HISTORY  (last 10 days)")
            L.append("  " + "─" * 50)
            recent = self._daily_history[-10:]
            for d in recent:
                bar_len = min(int(abs(d['net_pnl']) / 500), 20)
                bar     = ("█" * bar_len) if d['net_pnl'] >= 0 else ("▒" * bar_len)
                sign    = "+" if d['net_pnl'] >= 0 else "-"
                L.append(f"  {d['date']}  {bar:<20}  "
                         f"Rs {d['net_pnl']:>+7,.0f}  "
                         f"({d['return_pct']:>+.1f}%)")

        L.append("")
        L.append("=" * 62)

        report = "\n".join(L)
        print("\n" + report)

        with open(filename, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"\n  Report saved → {filename}")

        # Save daily summary to history
        self._save_daily_summary(net_pnl, day_roi, total,
                                 len(winners), day_type)

        return report

    def _save_daily_summary(self, net_pnl, day_roi, trades,
                             wins, day_type):
        """Append today to daily history JSON."""
        entry = {
            'date'       : str(self.today),
            'net_pnl'    : round(net_pnl, 2),
            'return_pct' : round(day_roi, 2),
            'trades'     : trades,
            'wins'       : wins,
            'day_type'   : day_type,
            'vix'        : self.vix_at_open,
        }
        # Remove today if already exists (re-run scenario)
        self._daily_history = [
            d for d in self._daily_history
            if d['date'] != str(self.today)
        ]
        self._daily_history.append(entry)
        try:
            with open(self.daily_log_file, 'w') as f:
                json.dump(self._daily_history, f, indent=2)
        except Exception as e:
            print(f"[Report] History save error: {e}")

    def close(self):
        try:
            self._log.close()
        except Exception:
            pass
