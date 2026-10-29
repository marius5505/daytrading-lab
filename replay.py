"""Run the live engine, unchanged, over recent real 5-min history in a throwaway database.

    python3 replay.py            # last 60 days (Yahoo's 5-min limit)
    python3 replay.py 30d

This is a sanity check of the code and a rough read of each bot, not a validated backtest:
60 days is one market regime and a small sample.
"""
import os
import sys
import tempfile
import time

import market
from engine import Engine, BENCHMARK, DEFAULT_WATCHLIST


def main(range_="60d"):
    path = os.path.join(tempfile.mkdtemp(), "replay.db")
    eng = Engine(path, live=False)
    tickers = DEFAULT_WATCHLIST + [BENCHMARK]
    bars = {}
    for t in tickers:
        bars[t] = market.fetch_bars(t, range_)
        time.sleep(0.3)
    days = sorted({market.datetime.fromtimestamp(b["ts"], market.ET).date() for b in bars[tickers[0]]})
    print(f"Replaying {len(days)} trading days ({days[0]} to {days[-1]}) on {', '.join(DEFAULT_WATCHLIST)}\n")
    # Feed one day at a time so equity snapshots land at each close.
    for d in days:
        end = int(market.datetime.combine(d, market.CLOSE, market.ET).timestamp()) + 60
        eng.step({t: [b for b in bs if b["ts"] < end] for t, bs in bars.items()}, end)
        eng.snapshot(end)
    spy = bars[BENCHMARK]
    print(f"{'Bot':<11}{'Return':>9}{'Trades':>8}{'Win %':>7}{'PF':>6}{'Max DD':>8}{'Fees':>8}")
    for b in eng.bots():
        s = eng.stats(b)
        pf = f"{s['profit_factor']:.2f}" if s["profit_factor"] is not None else "–"
        wr = f"{s['win_rate']:.0f}" if s["win_rate"] is not None else "–"
        print(f"{b['name']:<11}{s['return_pct']:>8.2f}%{s['trades']:>8}{wr:>7}{pf:>6}{s['max_drawdown_pct']:>7.1f}%{s['fees']:>8.2f}")
    print(f"\nSPY buy-and-hold over the same window: {(spy[-1]['c'] / spy[0]['o'] - 1) * 100:.2f}%")
    print(f"Database kept at {path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "60d")
