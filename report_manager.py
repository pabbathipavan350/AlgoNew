# ============================================================
# REPORT_MANAGER.PY — Trade Journal & Daily Report
# ============================================================
# CSV trade log (reports/trade_log.csv) — one row per trade.
# Daily summary JSON (reports/daily_summary.json) — history.
# ============================================================

import csv
import os
import json
import datetime
import config


class ReportManager:

    def __init__(self, capital_manager):
        self.cap_mgr   = capital_manager
        self.today     = datetime.date.today()
        self.trades    = []
        self.vix_at_open = 0.0
        os.makedirs("reports", exist_ok=True)
        self._init_trade_log()
        self._init_daily_history()

    def set_vix(self, vix):
        self.vix_at_open = vix

    # ── CSV trade log ──────────────────────────────────────
    def _init_trade_log(self):
        exists       = os.path.exists(config.TRADE_LOG_FILE)
        self._log    = open(config.TRADE_LOG_FILE, 'a', newline='', encoding='utf-8')
        self._writer = csv.writer(self._log)
        if not exists:
            self._writer.writerow([
                # Identity
                'Date', 'Trade#', 'Mode',
                # Timing
                'Entry Time', 'Exit Time', 'Duration (mins)',
                # Direction & strikes
                'Direction', 'Strike', 'Expiry', 'ATM at Entry',
                # Prices
                'Option Entry', 'Option Exit', 'Option Peak',
                'VWAP at Entry', 'Dist from VWAP (pts)',
                # Nifty context
                'Nifty at Entry', 'Nifty at Exit', 'Nifty Move',
                # Points analysis
                'Pts Gained', 'Pts Max Possible', 'Pts Missed',
                'Trail Efficiency %',
                # P&L
                'Gross PnL', 'Cost', 'Net PnL',
                'Capital After', 'Return on Capital %',
                # Exit analysis
                'Exit Phase', 'Exit Reason Short',
                'Breakeven Triggered', 'Trail Triggered',
                # Context
                'VIX', 'Target Pts', 'Target Reason',
            ])

    def _init_daily_history(self):
        self.daily_log_file  = "reports/daily_summary.json"
        self._daily_history  = []
        if os.path.exists(self.daily_log_file):
            try:
                with open(self.daily_log_file, 'r') as f:
                    self._daily_history = json.load(f)
            except Exception:
                self._daily_history = []

    # ── Log one completed trade ────────────────────────────
    def log_trade(self, trade):
        self.trades.append(trade)

        entry_dt = trade['entry_time']
        exit_dt  = trade['exit_time']
        duration = round((exit_dt - entry_dt).total_seconds() / 60, 1)

        entry_price = trade['entry_price']
        exit_price  = trade['exit_price']
        peak_price  = trade.get('peak_price', exit_price)
        entry_vwap  = trade.get('entry_vwap', 0.0)
        dist_vwap   = trade.get('entry_dist', round(abs(entry_price - entry_vwap), 2))
        nifty_entry = trade.get('nifty_at_entry', 0.0)
        nifty_exit  = trade.get('nifty_at_exit', 0.0)
        nifty_move  = round(nifty_exit - nifty_entry, 2) if nifty_entry > 0 and nifty_exit > 0 else 0.0

        pts_gained  = round(exit_price - entry_price, 2)
        pts_max     = round(peak_price - entry_price, 2)
        pts_missed  = round(pts_max - pts_gained, 2)
        trail_eff   = round(pts_gained / pts_max * 100, 1) if pts_max > 0 else 0.0

        cap_summary = self.cap_mgr.get_summary()
        deployed    = cap_summary.get('deployed', config.INITIAL_CAPITAL)
        roi_pct     = round(trade['net_rs'] / deployed * 100, 3) if deployed > 0 else 0.0
        cap_after   = cap_summary.get('current', 0)

        exit_reason = trade.get('exit_reason', '')
        exit_short  = exit_reason.split('|')[0].strip()[:50] if exit_reason else ''

        mode = 'PAPER' if config.PAPER_TRADE else 'LIVE'

        self._writer.writerow([
            str(self.today),
            len(self.trades),
            mode,
            entry_dt.strftime('%H:%M:%S'),
            exit_dt.strftime('%H:%M:%S'),
            duration,
            trade['direction'],
            trade['strike'],
            trade.get('expiry', ''),
            trade.get('atm_at_entry', ''),
            f"{entry_price:.2f}",
            f"{exit_price:.2f}",
            f"{peak_price:.2f}",
            f"{entry_vwap:.2f}",
            f"{dist_vwap:.2f}",
            f"{nifty_entry:.2f}" if nifty_entry else '',
            f"{nifty_exit:.2f}"  if nifty_exit  else '',
            f"{nifty_move:.2f}"  if nifty_move  else '',
            f"{pts_gained:+.2f}",
            f"{pts_max:.2f}",
            f"{pts_missed:.2f}",
            f"{trail_eff:.1f}",
            f"{trade['pnl_rs']:+.2f}",
            f"{trade['total_cost']:.2f}",
            f"{trade['net_rs']:+.2f}",
            f"{cap_after:,.0f}",
            f"{roi_pct:.3f}",
            trade.get('exit_phase', ''),
            exit_short,
            'Y' if trade.get('breakeven_done') else 'N',
            'Y' if trade.get('trail_active') else 'N',
            f"{self.vix_at_open:.1f}",
            f"{trade.get('target_points', 0):.1f}",
            trade.get('target_reason', ''),
        ])
        self._log.flush()

    # ── Daily report ───────────────────────────────────────
    def generate_daily_report(self):
        today_str = self.today.strftime('%Y%m%d')
        fname     = f"reports/report_{today_str}.txt"
        cap       = self.cap_mgr.get_summary()

        total    = len(self.trades)
        winners  = [t for t in self.trades if t['net_rs'] > 0]
        losers   = [t for t in self.trades if t['net_rs'] <= 0]
        win_rate = len(winners) / total * 100 if total > 0 else 0

        net_pnl    = sum(t['net_rs']    for t in self.trades)
        gross_pnl  = sum(t['pnl_rs']    for t in self.trades)
        total_cost = sum(t['total_cost'] for t in self.trades)
        deployed   = cap.get('deployed', config.INITIAL_CAPITAL)
        day_roi    = net_pnl / deployed * 100 if deployed > 0 else 0

        trail_exits = [t for t in self.trades if t.get('exit_phase') == 'Trail SL']
        be_exits    = [t for t in self.trades if t.get('exit_phase') == 'Breakeven SL']
        sl_exits    = [t for t in self.trades if t.get('exit_phase') == 'Initial SL']
        sq_exits    = [t for t in self.trades if t.get('exit_phase') == 'Square-off']
        flip_exits  = [t for t in self.trades if t.get('exit_phase') == 'Flip']
        tgt_exits   = [t for t in self.trades if t.get('exit_phase') == 'Target']

        ce_trades = [t for t in self.trades if t['direction'] == 'CE']
        pe_trades = [t for t in self.trades if t['direction'] == 'PE']
        ce_pnl    = sum(t['net_rs'] for t in ce_trades)
        pe_pnl    = sum(t['net_rs'] for t in pe_trades)

        durations = []
        for t in self.trades:
            try:
                d = (t['exit_time'] - t['entry_time']).total_seconds() / 60
                durations.append(d)
            except Exception:
                durations.append(0)
        avg_dur = round(sum(durations) / len(durations), 1) if durations else 0

        trail_effs = []
        for t in self.trades:
            peak   = t.get('peak_price', t['exit_price'])
            gained = t['exit_price'] - t['entry_price']
            maxp   = peak - t['entry_price']
            if maxp > 0:
                trail_effs.append(gained / maxp * 100)
        avg_eff = round(sum(trail_effs) / len(trail_effs), 1) if trail_effs else 0

        best  = max(self.trades, key=lambda t: t['net_rs']) if self.trades else None
        worst = min(self.trades, key=lambda t: t['net_rs']) if self.trades else None

        L = []
        L.append("=" * 62)
        L.append(f"  ALGO DAILY REPORT — {self.today.strftime('%A, %d %b %Y')}")
        L.append(f"  Mode: {'PAPER TRADE' if config.PAPER_TRADE else '*** LIVE ***'}")
        L.append(f"  VIX: {self.vix_at_open:.1f}")
        L.append("=" * 62)
        L.append("")
        L.append("  SUMMARY")
        L.append("  " + "-"*50)
        L.append(f"  Trades      : {total}  (CE:{len(ce_trades)}  PE:{len(pe_trades)})")
        L.append(f"  Win/Loss    : {len(winners)}/{len(losers)}  ({win_rate:.0f}%)")
        L.append(f"  Avg hold    : {avg_dur} mins")
        L.append(f"  Trail eff   : {avg_eff}%  (how much of each move captured)")
        L.append("")
        L.append("  EXIT BREAKDOWN")
        L.append("  " + "-"*50)
        L.append(f"  Target hit  : {len(tgt_exits)}")
        L.append(f"  Trail SL    : {len(trail_exits)}  (good — rode the trend)")
        L.append(f"  Breakeven SL: {len(be_exits)}   (ok — capital protected)")
        L.append(f"  Initial SL  : {len(sl_exits)}   (VWAP broke against us)")
        L.append(f"  Square-off  : {len(sq_exits)}")
        L.append(f"  Flip        : {len(flip_exits)}")
        L.append("")
        L.append("  P&L")
        L.append("  " + "-"*50)
        L.append(f"  CE P&L      : Rs {ce_pnl:+,.0f}")
        L.append(f"  PE P&L      : Rs {pe_pnl:+,.0f}")
        L.append(f"  Gross P&L   : Rs {gross_pnl:+,.0f}")
        L.append(f"  Total costs : Rs {total_cost:,.0f}")
        L.append(f"  NET P&L     : Rs {net_pnl:+,.0f}")
        L.append(f"  Day ROI     : {day_roi:+.2f}%  on Rs {deployed:,.0f}")
        L.append("")
        if best and worst:
            L.append("  BEST / WORST TRADE")
            L.append("  " + "-"*50)
            L.append(f"  Best  : #{self.trades.index(best)+1} {best['direction']} "
                     f"{best['entry_time'].strftime('%H:%M')}->"
                     f"{best['exit_time'].strftime('%H:%M')}  "
                     f"Rs {best['net_rs']:+,.0f}  | {best.get('exit_phase','')}")
            L.append(f"  Worst : #{self.trades.index(worst)+1} {worst['direction']} "
                     f"{worst['entry_time'].strftime('%H:%M')}->"
                     f"{worst['exit_time'].strftime('%H:%M')}  "
                     f"Rs {worst['net_rs']:+,.0f}  | {worst.get('exit_phase','')}")
            L.append("")
        L.append("  TRADE DETAIL")
        L.append("  " + "-"*50)
        for i, t in enumerate(self.trades, 1):
            peak     = t.get('peak_price', t['exit_price'])
            pts      = t['exit_price'] - t['entry_price']
            max_pts  = peak - t['entry_price']
            eff      = round(pts / max_pts * 100, 0) if max_pts > 0 else 0
            missed   = round(max_pts - pts, 1)
            dur      = durations[i-1] if i-1 < len(durations) else 0
            sign     = "+" if t['net_rs'] > 0 else "-"
            L.append(f"  {sign} #{i:02d} | {t['direction']} {t['strike']} | "
                     f"{t['entry_time'].strftime('%H:%M')}->{t['exit_time'].strftime('%H:%M')} ({dur:.0f}m)")
            L.append(f"       Entry={t['entry_price']:.0f} VWAP={t.get('entry_vwap',0):.0f} "
                     f"dist={t.get('entry_dist',0):.1f}pts | "
                     f"Exit={t['exit_price']:.0f} Peak={peak:.0f}")
            L.append(f"       Got={pts:+.1f}  Max={max_pts:.1f}  "
                     f"Eff={eff:.0f}%  Missed={missed:.1f}pts | Net=Rs{t['net_rs']:+.0f}")
        if not self.trades:
            L.append("  No trades today.")
        L.append("")
        L.append("  CAPITAL")
        L.append("  " + "-"*50)
        L.append(f"  Current     : Rs {cap.get('current',0):>10,.0f}")
        L.append(f"  Deployed    : Rs {cap.get('deployed',0):>10,.0f}")
        L.append(f"  Overall P&L : Rs {cap.get('total_pnl',0):>+10,.0f}")
        L.append(f"  Overall ROI : {cap.get('roi_pct',0):>+.2f}%")
        if len(self._daily_history) > 0:
            L.append("")
            L.append("  LAST 10 DAYS")
            L.append("  " + "-"*50)
            for d in self._daily_history[-10:]:
                bar = ("=" * min(int(abs(d['net_pnl']) / 500), 20))
                sgn = "+" if d['net_pnl'] >= 0 else "-"
                L.append(f"  {d['date']}  {sgn}{bar:<20}  "
                         f"Rs {d['net_pnl']:>+7,.0f}  ({d['return_pct']:>+.1f}%)")
        L.append("")
        L.append("="*62)

        report = "\n".join(L)
        with open(fname, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"\n  [ReportManager] Detailed report saved: {fname}")
        self._save_daily_summary(net_pnl, day_roi, total, len(winners))
        return report

    def _save_daily_summary(self, net_pnl, day_roi, trades, wins):
        entry = {
            'date'       : str(self.today),
            'net_pnl'    : round(net_pnl, 2),
            'return_pct' : round(day_roi, 2),
            'trades'     : trades,
            'wins'       : wins,
            'vix'        : self.vix_at_open,
        }
        self._daily_history = [d for d in self._daily_history
                               if d['date'] != str(self.today)]
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
