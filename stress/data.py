"""Fetch 60 days of 5-min bars once per ticker and cache them in stress/cache/ (never touches state/).

    python3 stress/data.py            # fetch anything not cached yet
"""
import json
import os
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import market  # noqa: E402

CACHE = os.path.join(HERE, "cache")
TECH = ["AMD", "NVDA", "TSLA", "META", "AAPL", "MSFT"]
NONTECH = ["JPM", "XOM", "KO", "UNH", "CAT", "WMT", "PG", "HD"]
ALL = TECH + NONTECH + ["SPY"]


def load(t):
    path = os.path.join(CACHE, f"{t}.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    bars = market.fetch_bars(t, "60d")
    time.sleep(0.3)
    os.makedirs(CACHE, exist_ok=True)
    with open(path, "w") as f:
        json.dump(bars, f)
    return bars


def day_of(b):
    return datetime.fromtimestamp(b["ts"], market.ET).date()


def full_days(bars):
    """Drop the last day if it is still in progress (fetched during the session)."""
    if not bars:
        return bars
    last = day_of(bars[-1])
    close = datetime.combine(last, market.close_time(last), market.ET).timestamp()
    if bars[-1]["ts"] + market.BAR_SECONDS < close:
        bars = [b for b in bars if day_of(b) != last]
    return bars


if __name__ == "__main__":
    for t in ALL:
        bars = full_days(load(t))
        days = sorted({day_of(b) for b in bars})
        per_day = len(bars) / len(days)
        print(f"{t:5} {len(bars):5} bars  {len(days)} days  {days[0]}..{days[-1]}  {per_day:.1f} bars/day")


def load_hourly(t):
    """~730 days of 1-hour regular-session bars (Yahoo's limit for 1h), cached like the 5-min bars."""
    path = os.path.join(CACHE, f"{t}_1h.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    import urllib.request
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?interval=1h&range=730d&includePrePost=false")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30, context=market._SSL) as r:
        res = json.load(r)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    bars = []
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c):
            continue
        dt = datetime.fromtimestamp(ts, market.ET)
        if not (market.OPEN <= dt.time() < market.close_time(dt.date())):
            continue
        bars.append({"ts": ts, "o": o, "h": h, "l": l, "c": c, "v": v or 0})
    time.sleep(0.3)
    with open(path, "w") as f:
        json.dump(bars, f)
    return bars
