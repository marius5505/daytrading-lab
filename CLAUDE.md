# Day-Trading Lab

Paper-trading bots on real US prices (Yahoo 5-min bars). Python 3 standard library only; SQLite in `state/`.
Paper money only: never add code that places real orders.

## Layout

- `strategies.py`: indicators, every bot's `entry(p, bars, ind, i, ctx)` / `exit(p, bars, ind, i, pos)`, and the
  `STRATEGIES` registry. New bots go here.
- `engine.py`: paper broker (sizing, fees, T+1 settlement, stops, database). `replay.py` and `server.py` both use it,
  so replay and live trade the same way.
- `market.py` calendar and price fetch, `scans.py` morning scans, `premarket.py` 09:00 report.
- `server.py` + `dashboard.html`: live loop and dashboard on http://localhost:8787.
- `stress/`: engine unit tests, the stress harness, and the 15-algorithm research battery in `stress/algos/`.

## Commands

```bash
python3 stress/test_engine.py       # run after any engine change
python3 replay.py                   # 60-day scoreboard for every bot
python3 stress/data.py              # cache bars for the stress tests
python3 stress/algos/run_algos.py
```

## Rules for research

- Fix the pass/fail rules before running a test; don't change them after seeing results.
- Don't tune on the holdout (`stress/algos/common.HOLD`, the last 10 days).
- Compare every bot with the Control bot, and count all fees and slippage.
- Stress tests run in a throwaway database; never write to `state/` from research code.
- When porting an algorithm from `stress/algos/` to `strategies.py`, check that both take the same trades.
