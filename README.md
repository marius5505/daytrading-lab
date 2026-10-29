# Day-Trading Lab

A small lab for building and testing your own day-trading bots **with fake money on real prices**.

Each bot starts with $1,000 of paper cash and trades US stocks on real 5-minute bars from Yahoo Finance, with
realistic broker fees, slippage and cash-account rules. A local dashboard shows how every bot is doing, and a replay
tool tells you within seconds how a bot would have done over the last 60 days.

> **Paper trading only. Nothing here places real orders, and nothing here is financial advice.** Most of the ideas
> in this repo lose money after costs, and that is one of the main things it teaches.

## What you need

- Python 3.9 or newer. Everything runs on the standard library.
- Optional: `pip install certifi` if you get SSL errors fetching prices (common on macOS), and `pip install numpy`
  for one of the stress-test algorithms.
- An internet connection (prices come from Yahoo's public chart API, no key needed).

## Quick start

```bash
python3 replay.py          # replay the last 60 days for every bot and print a scoreboard
python3 replay.py 30d      # just the last 30 days
python3 server.py          # run the bots live (paper) and open http://localhost:8787
python3 premarket.py       # print today's premarket report (gappers, volume, news)
```

`server.py` trades during US market hours (09:30-16:00 New York time) and builds the premarket report at 09:00 New
York time, so leave the computer awake. It keeps its data in `state/` (a SQLite database), which git ignores. Set
`DAYTRADER_PORT` to use another port.

## How it fits together

| File | What it does |
|---|---|
| `strategies.py` | **The bots.** Indicators, each bot's entry and exit rules, and the `STRATEGIES` registry. |
| `engine.py` | Paper broker: positions, sizing, fees, cash settlement (T+1), stops, the SQLite database. |
| `market.py` | Market calendar (holidays, half days) and the Yahoo price fetcher. |
| `scans.py` | Morning scans that pick stocks for some bots (gappers, unusual volume, weekly losers). |
| `premarket.py` | The 09:00 premarket report. |
| `server.py` | Runs the engine on a loop and serves the dashboard and its small JSON API. |
| `dashboard.html` | The dashboard: bots, trades, equity curves, settings. |
| `replay.py` | Runs the same engine over past bars, so a replay trades exactly like live. |
| `stress/` | Stress tests: the engine's unit tests and a harness to test many algorithms on many stocks. |

## Write your own bot

A bot is two functions and an entry in `STRATEGIES` in `strategies.py`.

```python
def dip_entry(p, bars, ind, i, ctx):
    """Called on every new 5-min bar for each ticker. Return None or a signal."""
    c = bars[i]["c"]
    if ind["rsi2"][i] < p["rsi_entry"] and c > ind["ema200"][i]:
        return {"side": 1,                          # 1 = buy (long), -1 = short (margin accounts only)
                "stop": c - 2 * ind["atr"][i],     # stop-loss price (also sets position size)
                "target": None,                     # optional take-profit price
                "why": f"RSI(2) {ind['rsi2'][i]:.0f} dip"}  # shown on the dashboard
    return None


def dip_exit(p, bars, ind, i, pos):
    """Called on every bar while a position is open. Return a reason string to sell, or None to hold."""
    if ind["rsi2"][i] > p["rsi_exit"]:
        return "RSI(2) recovered"
    return None


STRATEGIES["dip"] = {
    "name": "My Dip Bot",
    "blurb": "One line that explains the idea on the dashboard.",
    "entry": dip_entry, "exit": dip_exit,
    "defaults": {**COMMON, "rsi_entry": 10, "rsi_exit": 70},  # editable in the dashboard
}
```

- `bars` are the ticker's 5-min bars (`ts`, `o`, `h`, `l`, `c`, `v`), `i` is the bar that just closed.
- `ind` holds ready-made indicators from `indicators()`: `ema9/20/50/200`, `rsi2`, `atr`, `vwap`, `vol20`, `et`
  (bar times in New York). Add your own there.
- `ctx` tells the entry function about today, for example `ctx["trades_today"]`.
- `COMMON` sets risk per trade (1% of equity at the stop), the largest position (25%) and how many at once (4).
- Optional keys: `"tickers"` / `"universe"` to trade a fixed list or a scan instead of the watchlist, `"swing": True`
  to hold overnight, `"retired": True` to hide a bot but keep it for replay.

The engine handles the rest: sizing, fees, stops and targets, closing day trades before the bell, and the cash
account's settlement rules. Then run `python3 replay.py` and see how it does.

## Testing an idea properly

```bash
python3 stress/test_engine.py       # unit tests for the engine (fees, settlement, stops, ...)
python3 stress/data.py              # download 60 days of 5-min bars into stress/cache/ (once)
python3 stress/algos/run_algos.py   # the 15-algorithm battery; writes results.md and results.json
```

`stress/harness.py` is the library behind the battery: it runs any bot through the unchanged engine on cached bars
in a throwaway database, with switches for the fee model, next-bar fills and account rules. Use it to build your own
tests; `stress/algos/common.py` shows how.

The rules of thumb this lab was built on, learned the hard way:

1. **Decide the pass/fail rules before you run the test, never after.** Write down what "works" means (profit
   after costs, number of trades, both halves of the period profitable, ...) and don't move the bar when a
   favourite idea misses it.
2. **Keep a holdout.** The stress battery tunes nothing on the last 10 days (`common.HOLD`). Only an idea that
   passed on the earlier days gets one look at the holdout. If you peek and tweak, it's no longer a holdout.
3. **Always run a Control bot.** `Control` makes the same kind of trade on a random stock. If your bot can't beat a
   random pick, it has no edge, only luck.
4. **Count every cost.** The engine charges IBKR-style tiered commissions ($0.0035 per share, $0.35 minimum, capped
   at 1% of the trade), SEC and FINRA fees on sales, and 0.01% slippage. Lots of strategies look great without these.
5. **Respect the account rules.** By default this is a cash account: long only, and you can only buy with settled
   cash (T+1). That limits how often a small account can trade.
6. **Paper-trade live before believing anything.** Run the bots side by side for 30 days without changing them,
   then review. Tuning during the run spoils the comparison.
7. **Check that the live code trades like the research code.** When you port an algorithm from `stress/algos/` to
   `strategies.py`, replay both on the same days and make sure they take the same trades.
8. **Expect most ideas to fail.** Of the 15 algorithms in `stress/algos/`, none passed the stress test. That isn't a
   bug in the lab; finding a real edge is hard.

## The bots included

| Bot | Idea |
|---|---|
| Breakout | Trades the first move out of the opening range. |
| Gapper | Buys a stock gapping up on heavy premarket volume when it breaks its first 5-min high. |
| ORB In Play | Buys the 2 rising stocks with the most unusual opening volume and holds to the close. |
| Weekly Loser | Buys the week's biggest loser, betting on a bounce. |
| Control | The yardstick: Weekly Loser's trade on a random stock. |
| Gap Fade | Buys a gap-down stock that climbs back above its open. |
| Gap and Go | Buys the 2 biggest gap-ups still holding above the open and VWAP at 10:00. |
| VWAP Band | Buys a stock stretched 2 standard deviations below VWAP; sells at VWAP. |
| Midday Squeeze | Buys the afternoon break of a tight midday range. |
| NR4 Breakout | After the quietest day of 4, buys the break of that day's high. |
| Swing RSI(2) | Buys a sharp 2-day dip in an uptrend at the close; holds up to 10 days. |

Retired, but kept for replay: Pullback and VWAP Trend.

## Using it with Claude Code (or another AI assistant)

`CLAUDE.md` explains the project to an AI coding assistant. A good way to learn is to describe a trading idea in
plain words, ask the assistant to write it as a bot in `strategies.py`, then replay and stress-test it together.

## License

MIT, see `LICENSE`.
