"""Daily scans over premarket.UNIVERSE that pick the stocks for the ORB In Play, Weekly Loser and Control bots,
plus an after-close archive of every universe stock's 5-min bars (Yahoo keeps only 60 days; the archive grows).

The rules are the ones stress-tested in stress/algos (A1 and A12), with their default settings:
  09:10  Weekly Loser: the stock with the lowest close-to-close return over the last 5 complete days, if <= -3%.
         Control: one stock drawn at random (seeded by the date) from the stocks with enough history.
         ORB pool: each stock's average first-5-min-bar volume over the last 14 days.
  09:35  ORB In Play: the 2 stocks whose first bar is green with the highest volume relative to that average (>= 1x).

    python3 scans.py     # run the 09:10 scan now and print it
"""
import json
import os
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import market
import premarket
from market import ET

HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY_DIR = os.path.join(HERE, "history")
LOSER_DAYS, LOSER_MIN_PCT = 5, 3.0
RVOL_DAYS, RVOL_MIN, ORB_TOP = 14, 1.0, 2
MIN_DAYS = 15  # complete days needed for the 14-day ATR the bots' stops use


def _fetch_all(symbols, range_, workers=6):
    def one(t):
        try:
            return t, market.fetch_bars(t, range_)
        except Exception:
            return t, None
    with ThreadPoolExecutor(workers) as ex:
        return {t: b for t, b in ex.map(one, symbols) if b}


def _days(bars, before):
    """{date: [bars]} for complete regular-session days before `before`, oldest first."""
    out = {}
    for b in bars:
        d = datetime.fromtimestamp(b["ts"], ET).date()
        if d < before:
            out.setdefault(d, []).append(b)
    return dict(sorted(out.items()))


def morning(d, universe=None, fetch=None):
    """The 09:10 scan for trading day d. Returns {"loser": [...], "control": [...], "orb_pool": {t: avg first-bar volume}}."""
    universe = universe or premarket.UNIVERSE
    data = fetch(universe) if fetch else _fetch_all(universe, "1mo")
    rets, eligible, pool = [], [], {}
    for t in universe:
        days = _days(data.get(t) or [], d)
        if len(days) < MIN_DAYS:
            continue
        eligible.append(t)
        closes = [v[-1]["c"] for v in days.values()]
        rets.append((closes[-1] / closes[-1 - LOSER_DAYS] - 1, t))
        firsts = [v[0]["v"] for v in list(days.values())[-RVOL_DAYS:]]
        if len(firsts) == RVOL_DAYS and sum(firsts):
            pool[t] = sum(firsts) / RVOL_DAYS
    worst = min(rets) if rets else None
    loser = [worst[1]] if worst and worst[0] * 100 <= -LOSER_MIN_PCT else []
    control = [random.Random(d.isoformat()).choice(sorted(eligible))] if eligible else []
    return {"loser": loser, "control": control, "orb_pool": pool,
            "worst": {"ticker": worst[1], "ret_pct": round(worst[0] * 100, 2)} if worst else None}


def orb(d, pool, fetch=None):
    """The 09:35 scan: the ORB_TOP stocks with a green first bar and the highest first-bar relative volume."""
    data = fetch(list(pool)) if fetch else _fetch_all(list(pool), "1d")
    open_ts = int(datetime.combine(d, market.OPEN, ET).timestamp())
    cands = []
    for t, avg in pool.items():
        first = next((b for b in data.get(t) or [] if b["ts"] == open_ts), None)
        if not first or not avg:
            continue
        rv = first["v"] / avg
        if rv >= RVOL_MIN and first["c"] > first["o"]:
            cands.append((rv, t))
    return [t for _, t in sorted(cands, reverse=True)[:ORB_TOP]]


def archive(symbols, fetch=None, now_ts=None):
    """Merge the last 5 days of completed 5-min bars into history/<ticker>.json. Returns the number of stocks saved."""
    os.makedirs(HISTORY_DIR, exist_ok=True)
    data = fetch(symbols) if fetch else _fetch_all(symbols, "5d", workers=4)
    now_ts = now_ts or int(datetime.now(ET).timestamp())
    n = 0
    for t, bars in data.items():
        path = os.path.join(HISTORY_DIR, f"{t}.json")
        old = []
        if os.path.exists(path):
            with open(path) as f:
                old = json.load(f)
        merged = {b["ts"]: b for b in old}
        merged.update({b["ts"]: b for b in market.completed(bars, now_ts)})
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump([merged[k] for k in sorted(merged)], f)
        os.replace(tmp, path)
        n += 1
    return n


if __name__ == "__main__":
    now = market.now_et()
    r = morning(now.date())
    print(json.dumps({k: v for k, v in r.items() if k != "orb_pool"}, indent=2), f"\nORB pool: {len(r['orb_pool'])} stocks")
