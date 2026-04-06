# Nifty Options Live Trading Algo

## Strategy
| Parameter | Value |
|-----------|-------|
| Signal | VWAP crossover (entry strike 0–4pts above VWAP + opposite strike below VWAP) |
| Entry type | **Pullback** (was_above=True) → wait 60s for better fill, re-confirm; **Fresh cross** → enter immediately |
| Strike | OI-filtered: VIX+expiry matrix selects ITM depth, algo walks toward ATM until OI ≥ 5 lakh |
| CE | Nifty CE 0–4pts above its VWAP AND PE below its VWAP → buy CE |
| PE | Nifty PE 0–4pts above its VWAP AND CE below its VWAP → buy PE |
| Expiry | Current week Tuesday (holiday-adjusted) |
| Capital | Rs 30,000 start → auto-doubles when portfolio doubles |
| Costs | ~Rs 340 per round-trip |

## Profit Ladder (4-level stepped SL — ratchet only, never down)
| Phase | Trigger | SL moves to | Notes |
|-------|---------|-------------|-------|
| 0 | 0 → +10pts | effective_VWAP − 4pts (dynamic) | Tracks rising VWAP, capped at entry − 10 |
| 1 | +10pts | entry + 0 (breakeven) | Zero-loss guaranteed |
| 2 | +25pts | entry + 10 | Minimum +10pts locked |
| 3 | +35pts | entry + 25 | Minimum +25pts locked |
| 4 | +40pts | **Book profit** — exit immediately | |

## Overtrading Guard
If **10 trades** happen within any rolling 60-minute window, new entries are **paused until 12:00 PM**. The algo continues receiving ticks and managing open positions during the pause, and logs any signals it sees.

## Setup (one time)
```
pip install -r requirements.txt
```

## IMPORTANT — Paper vs Live Mode

**Default is PAPER TRADE mode** — no real orders are placed.

To switch modes, open `config.py` and change line 7:
```python
PAPER_TRADE = True    # ← Paper mode (safe, simulated)
PAPER_TRADE = False   # ← Live mode (REAL money, REAL orders)
```

**Recommended steps:**
1. Run in paper mode for at least 1–2 weeks
2. Check daily reports — verify signals, P&L, logic
3. Only then change to `PAPER_TRADE = False`

## Run (every trading day)
Start before 9:15 AM:
```
python main.py
```

The algo will:
1. Auto-login to Kotak Neo (TOTP + MPIN — no input needed)
2. Scan OI at startup to select liquid ITM strikes
3. Subscribe to 2 tokens (CE + PE) at 9:15 AM
4. Watch for VWAP signals from 9:15 AM
5. Fresh cross → enter immediately; pullback → wait 60s, re-confirm, enter
6. Manage SL via 4-level profit ladder automatically
7. Book profit at +40pts; square off at 3:25 PM if still in trade
8. Generate daily report at end of session
9. Save capital state for next day

## Output Files
| File | Contents |
|------|----------|
| `reports/trade_log.csv` | All trades — cumulative across days |
| `reports/daily_DDMMYYYY.txt` | Today's trade report |
| `capital.json` | Capital state (persists across days) |
| `logs/algo_YYYYMMDD.log` | Detailed system log |

## Capital Doubling Logic
- Start: Rs 30,000 deployed
- When total capital reaches Rs 60,000 → deploy Rs 60,000
- When total capital reaches Rs 120,000 → deploy Rs 120,000
- Capital state saved in `capital.json` — survives restarts

## Files
| File | Purpose |
|------|---------|
| `main.py` | Entry point — run this |
| `vwap_engine.py` | VWAP calculation + signal detection + profit ladder |
| `option_manager.py` | Strike picker (OI-filtered) + order placement |
| `capital_manager.py` | Capital tracking + auto-doubling |
| `report_manager.py` | Daily report generator |
| `config.py` | All settings |
| `auth.py` | Kotak Neo login |
