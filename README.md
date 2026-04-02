# Nifty Options Live Trading Algo

## Strategy
| Parameter | Value |
|-----------|-------|
| Signal | 3-min VWAP crossover (2-candle confirm + volume filter) |
| Entry | Only within 5 pts of VWAP |
| CE | Nifty closes above VWAP → buy CE (delta ≥ 0.8) |
| PE | Nifty closes below VWAP → buy PE (delta ≥ 0.8) |
| Stop Loss | VWAP − 5 pts (trails continuously as VWAP moves) |
| Trail | Activates at +20 pts, trails every 1 pt |
| Target | 40–50 pts (exits when trail SL hits) |
| Expiry | Current week Thursday |
| Capital | Rs 40,000 start → auto-doubles when portfolio doubles |
| Costs | Rs 20 × 2 legs + Rs 300 tax = Rs 340 per trade |

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
2. Subscribe to Nifty live feed at 9:15 AM
3. Watch for VWAP crossover signals from 9:20 AM
4. Auto-pick ITM strike (delta ≥ 0.8) from option chain
5. Place buy order, manage SL and trailing automatically
6. Square off at 3:20 PM if still in trade
7. Generate daily report at 3:25 PM
8. Save capital state for next day

## Output Files
| File | Contents |
|------|----------|
| `trade_log.csv` | All trades — cumulative across days |
| `daily_report_DDMMYYYY.txt` | Today's trade report |
| `capital.json` | Capital state (persists across days) |
| `algo_log.txt` | Detailed system log |

## Capital Doubling Logic
- Start: Rs 40,000 deployed
- When total capital reaches Rs 80,000 → deploy Rs 80,000
- When total capital reaches Rs 160,000 → deploy Rs 160,000
- Capital state saved in `capital.json` — survives restarts

## Files
| File | Purpose |
|------|---------|
| `main.py` | Entry point — run this |
| `vwap_engine.py` | VWAP calculation + signal detection |
| `option_manager.py` | Strike picker + order placement |
| `capital_manager.py` | Capital tracking + auto-doubling |
| `report_manager.py` | Daily report generator |
| `config.py` | All settings |
| `auth.py` | Kotak Neo login |
