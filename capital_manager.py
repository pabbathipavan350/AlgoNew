# ============================================================
# CAPITAL_MANAGER.PY — Persistent Capital Tracker
# ============================================================
# FIX #9: Auto-backup capital.json daily
#   - Saves backup as capital_backup_DDMMYYYY.json
#   - On load: if main file corrupt, restores from latest backup
#   - Never lose capital tracking again
# ============================================================

import json
import os
import glob
import datetime
import config


class CapitalManager:

    def __init__(self):
        self.capital_file = config.CAPITAL_FILE
        self.state        = self._load()

    def _load(self):
        """Load capital state — with backup restore if corrupted."""
        # Try main file first
        if os.path.exists(self.capital_file):
            try:
                with open(self.capital_file, 'r') as f:
                    state = json.load(f)
                print(f"[Capital] Loaded: Rs {state['current_capital']:,.0f} | "
                      f"Peak: Rs {state['peak_capital']:,.0f}")
                self._save_backup(state)  # backup on every successful load
                return state
            except Exception as e:
                print(f"[Capital] Main file corrupt: {e}")
                print(f"[Capital] Attempting restore from backup...")
                restored = self._restore_from_backup()
                if restored:
                    return restored

        # Fresh start
        state = {
            'initial_capital'  : config.INITIAL_CAPITAL,
            'current_capital'  : config.INITIAL_CAPITAL,
            'deployed_capital' : config.INITIAL_CAPITAL,
            'peak_capital'     : config.INITIAL_CAPITAL,
            'total_pnl'        : 0.0,
            'start_date'       : str(datetime.date.today()),
            'last_updated'     : str(datetime.date.today()),
        }
        self._save(state)
        print(f"[Capital] Fresh start: Rs {config.INITIAL_CAPITAL:,.0f}")
        return state

    def _save(self, state=None):
        """Save capital state to main file."""
        if state is None:
            state = self.state
        state['last_updated'] = str(datetime.date.today())
        with open(self.capital_file, 'w') as f:
            json.dump(state, f, indent=2)

    def _save_backup(self, state=None):
        """
        FIX #9: Save daily backup of capital state.
        File: capital_backup_DDMMYYYY.json
        Keeps last 30 days of backups.
        """
        if state is None:
            state = self.state
        today     = datetime.date.today().strftime('%d%m%Y')
        backup_file = f"capital_backup_{today}.json"
        try:
            with open(backup_file, 'w') as f:
                json.dump(state, f, indent=2)
            # Clean old backups — keep last 30 days
            backups = sorted(glob.glob("capital_backup_*.json"))
            for old in backups[:-30]:
                try:
                    os.remove(old)
                except Exception:
                    pass
        except Exception as e:
            print(f"[Capital] Backup warning: {e}")

    def _restore_from_backup(self):
        """Restore capital from most recent backup file."""
        backups = sorted(glob.glob("capital_backup_*.json"), reverse=True)
        for backup in backups:
            try:
                with open(backup, 'r') as f:
                    state = json.load(f)
                print(f"[Capital] Restored from: {backup}")
                print(f"[Capital] Capital: Rs {state['current_capital']:,.0f}")
                self._save(state)  # write back to main file
                return state
            except Exception:
                continue
        print("[Capital] No backup found — starting fresh")
        return None

    @property
    def current_capital(self):
        return self.state['current_capital']

    @property
    def deployed_capital(self):
        return self.state['deployed_capital']

    def update_after_trade(self, pnl_rs):
        """Update capital after trade. Check for doubling."""
        self.state['current_capital'] += pnl_rs
        self.state['total_pnl']       += pnl_rs

        if self.state['current_capital'] > self.state['peak_capital']:
            self.state['peak_capital'] = self.state['current_capital']

        # Check double threshold — deploy more when capital doubles
        threshold = self.state['initial_capital'] * 2   # doubles when 2× initial
        if self.state['current_capital'] >= threshold:
            old = self.state['deployed_capital']
            self.state['deployed_capital'] = (
                int(self.state['current_capital'] / 10000) * 10000
            )
            if self.state['deployed_capital'] != old:
                print(f"\n[Capital] *** CAPITAL DOUBLED! ***")
                print(f"[Capital] Deployed: Rs {old:,.0f} → "
                      f"Rs {self.state['deployed_capital']:,.0f}")

        self._save()
        self._save_backup()
        return self.state['current_capital']

    def calc_lots(self, option_price):
        """Calculate lots from deployed capital."""
        if option_price <= 0:
            return 1
        lots = int(self.state['deployed_capital'] /
                   (option_price * config.LOT_SIZE))
        return max(1, lots)

    def get_summary(self):
        s   = self.state
        roi = ((s['current_capital'] - s['initial_capital']) /
               s['initial_capital']) * 100
        return {
            'initial'  : s['initial_capital'],
            'current'  : s['current_capital'],
            'deployed' : s['deployed_capital'],
            'total_pnl': s['total_pnl'],
            'roi_pct'  : round(roi, 2),
            'peak'     : s['peak_capital'],
            'start'    : s['start_date'],
        }

    def print_status(self):
        s = self.get_summary()
        print(f"\n[Capital] Status:")
        print(f"  Initial capital  : Rs {s['initial']:>10,.0f}")
        print(f"  Current capital  : Rs {s['current']:>10,.0f}")
        print(f"  Deployed today   : Rs {s['deployed']:>10,.0f}")
        print(f"  Total P&L        : Rs {s['total_pnl']:>+10,.0f}")
        print(f"  ROI              : {s['roi_pct']:>+.1f}%")
        print(f"  Running since    : {s['start']}")
