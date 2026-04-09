# Nifty Futures VWAP Options Algo — v2

## Strategy
| Parameter | Value |
|-----------|-------|
| Signal | Nifty current-month **futures** VWAP cross or pullback (tick-level) |
| Buy CE | Futures crosses **above** VWAP, or pulls back to within 5pts above VWAP |
| Buy PE | Futures crosses **below** VWAP, or pulls back to within 5pts below VWAP |
| Strike | ITM · delta ≥ 0.85 · OI ≥ 12,00,000 · walks toward ATM if OI fails |
| Expiry | Current month (last Thursday) |
| SL | Option entry price − **15 pts** |
| Target | Option entry price + **45 pts** |
| Capital | Rs 5,00,000 · **5 lots fixed** |
| Square-off | 3:25 PM hard exit |

## Entry Types
| Type | Condition | Behaviour |
|------|-----------|-----------|
| **Fresh cross** | Futures crosses VWAP (below→above for CE, above→below for PE) | Enter immediately on the cross tick |
| **Pullback CE** | Futures was above VWAP, pulls back to within 5pts above it | Enter once per pullback visit — resets when price leaves zone |
| **Pullback PE** | Futures was below VWAP, pulls up to within 5pts below it | Enter once per pullback visit — resets when price leaves zone |

## Directional Re-entry Rules
- **Same direction blocked:** If CE is already open, any new CE signal (cross or pullback) is ignored.
- **Opposite direction allowed:** If CE is open and a PE signal fires, the CE position is closed first, then PE is entered. This handles genuine reversals.

## Why futures VWAP instead of option VWAP
Options VWAP (`ap` field) is noisy — wide spreads, thin volume, and delta sensitivity all cause false signals. Futures VWAP tracks the index with massive liquidity, giving cleaner crossovers.

## Cost breakdown (per round-trip, 5 lots × 65 qty = 325 qty)
| Component | ~Amount |
|-----------|---------|
| Brokerage (Rs 20 × 2) | Rs 40 |
| STT (0.05% of turnover) | ~Rs 15–50 |
| Exchange txn (0.053%) | ~Rs 16–53 |
| GST (18% on brokerage+exchange) | ~Rs 10–18 |
| Stamp duty (0.003% on buy) | ~Rs 5–15 |
| **Total** | **~Rs 85–180** |

## Setup
```
pip install -r requirements.txt
```
Edit `.env` with your Kotak credentials.

## Run
```
python main.py
```
Start before 9:15 AM. The algo will:
1. Login to Kotak Neo
2. Resolve current-month Nifty futures token from scrip master
3. Subscribe to futures WebSocket at 9:15 AM
4. Detect VWAP crosses and pullbacks tick-by-tick
5. On signal → pick ITM strike (delta ≥ 0.85, OI ≥ 12L) → buy immediately
6. Monitor option price — exit on SL (−15 pts) or Target (+45 pts)
7. Same-direction re-entry blocked; opposite-direction closes current and enters new
8. Square off at 3:25 PM · generate daily report

## Files
| File | Purpose |
|------|---------|
| `main.py` | Entry point — run this |
| `futures_engine.py` | Futures VWAP tracker · cross + pullback detector |
| `option_manager.py` | Strike picker (delta+OI) + order placement + cost calc |
| `config.py` | All settings |
| `capital_manager.py` | Capital tracking |
| `report_manager.py` | Daily report generator |
| `auth.py` | Kotak Neo login |
| `session_manager.py` | Session keepalive (ping every 25 min, auto re-login) |
| `telegram_notifier.py` | Telegram trade alerts |

## Output files
| File | Contents |
|------|----------|
| `reports/trade_log.csv` | All trades cumulative |
| `reports/report_YYYYMMDD.txt` | Daily summary report |
| `capital.json` | Capital state (survives restarts) |
| `logs/algo_YYYYMMDD.log` | Detailed system log |

## Guards
| Guard | Value |
|-------|-------|
| Max daily loss | Rs 15,000 — stops new entries |
| Expiry day cutoff | 2:30 PM — no new entries |
| Square-off | 3:25 PM — hard exit |
| No-tick circuit | 5 min silence → Telegram alert |
| Session keepalive | Ping every 25 min, auto re-login |
| Same-direction block | No re-entry in same direction while trade is open |

## Paper vs Live
Default is **PAPER TRADE** — no real orders placed.
Set `PAPER_TRADE = False` in `config.py` when ready for live.
