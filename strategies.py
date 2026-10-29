"""The three bot strategies, evaluated on completed 5-minute bars.

Each strategy has:
  entry(p, bars, ind, i, ctx) -> None or {"side": 1|-1, "stop": float, "target": float|None, "why": str}
  exit(p, bars, ind, i, pos)  -> None or reason string (exit at bar i's close)
Stops, targets and the end-of-day flatten are handled by the engine for every bot.
"""
from datetime import datetime, time

import market
from market import ET


# ---------- indicators ----------
def ema(vals, n):
    out, k, e = [], 2 / (n + 1), None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(vals, n):
    out, ag, al = [None], None, None
    for i in range(1, len(vals)):
        d = vals[i] - vals[i - 1]
        g, l = max(d, 0), max(-d, 0)
        if ag is None:
            ag, al = g, l
        else:
            ag, al = (ag * (n - 1) + g) / n, (al * (n - 1) + l) / n
        out.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    return out


def atr(bars, n):
    out, a = [], None
    for i, b in enumerate(bars):
        tr = b["h"] - b["l"] if i == 0 else max(b["h"] - b["l"], abs(b["h"] - bars[i - 1]["c"]), abs(b["l"] - bars[i - 1]["c"]))
        a = tr if a is None else (a * (n - 1) + tr) / n
        out.append(a)
    return out


def session_vwap(bars):
    out, day, pv, vol = [], None, 0.0, 0.0
    for b in bars:
        d = datetime.fromtimestamp(b["ts"], ET).date()
        if d != day:
            day, pv, vol = d, 0.0, 0.0
        tp = (b["h"] + b["l"] + b["c"]) / 3
        pv += tp * b["v"]
        vol += b["v"]
        out.append(pv / vol if vol else tp)
    return out


def sma(vals, n):
    out, s = [], 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        out.append(s / min(i + 1, n))
    return out


def indicators(bars):
    c = [b["c"] for b in bars]
    return {
        "ema9": ema(c, 9), "ema20": ema(c, 20), "ema50": ema(c, 50), "ema200": ema(c, 200),
        "rsi2": rsi(c, 2), "atr": atr(bars, 14), "vwap": session_vwap(bars),
        "vol20": sma([b["v"] for b in bars], 20),
        "et": [datetime.fromtimestamp(b["ts"], ET) for b in bars],
    }


def _bar_end(ind, i):
    """Clock time when bar i completes (bar start + 5 min)."""
    t = ind["et"][i]
    m = t.hour * 60 + t.minute + 5
    return time(m // 60, m % 60)


def _hm(s):
    h, m = s.split(":")
    return time(int(h), int(m))


# ---------- 1. Pullback: RSI(2) dip-buying inside an intraday uptrend (Connors-style) ----------
def pullback_entry(p, bars, ind, i, ctx):
    if i < 200 or not (_hm(p["first_entry"]) <= _bar_end(ind, i) <= _hm(p["last_entry"])):
        return None
    if ctx["trades_today"] >= p["max_trades_per_ticker_day"]:
        return None
    c = bars[i]["c"]
    if ind["ema50"][i] > ind["ema200"][i] and c > ind["ema200"][i] and ind["rsi2"][i] < p["rsi_entry"]:
        return {"side": 1, "stop": c - p["atr_stop"] * ind["atr"][i], "target": None,
                "why": f"RSI(2) {ind['rsi2'][i]:.0f} dip in uptrend"}
    return None


def pullback_exit(p, bars, ind, i, pos):
    if ind["rsi2"][i] > p["rsi_exit"]:
        return f"RSI(2) recovered to {ind['rsi2'][i]:.0f}"
    return None


# ---------- 2. Breakout: opening-range breakout, range-width stop, R-multiple target ----------
def breakout_entry(p, bars, ind, i, ctx):
    t = ind["et"][i]
    end = _bar_end(ind, i)
    range_end_min = 9 * 60 + 30 + p["range_minutes"]
    if end.hour * 60 + end.minute <= range_end_min or end > _hm(p["last_entry"]):
        return None
    if ctx["trades_today"] >= 1:
        return None
    day = t.date()
    rng = [bars[j] for j in range(max(0, i - 80), i)
           if ind["et"][j].date() == day and ind["et"][j].hour * 60 + ind["et"][j].minute < range_end_min]
    if len(rng) < p["range_minutes"] // 5:
        return None
    hi, lo = max(b["h"] for b in rng), min(b["l"] for b in rng)
    c, prev = bars[i]["c"], bars[i - 1]["c"]
    if (hi - lo) / c > p["max_range_pct"] / 100:
        return None
    if c > hi >= prev:
        return {"side": 1, "stop": lo, "target": c + p["r_multiple"] * (c - lo), "why": f"Broke {p['range_minutes']}-min high {hi:.2f}"}
    if p["allow_shorts"] and c < lo <= prev:
        return {"side": -1, "stop": hi, "target": c - p["r_multiple"] * (hi - c), "why": f"Broke {p['range_minutes']}-min low {lo:.2f}"}
    return None


def breakout_exit(p, bars, ind, i, pos):
    if p.get("flat_at") and _bar_end(ind, i) >= _hm(p["flat_at"]):
        return f"Breakout window over ({p['flat_at']} ET)"
    return None


# ---------- 3. VWAP trend: VWAP reclaim/loss with EMA momentum and volume confirmation ----------
def vwap_entry(p, bars, ind, i, ctx):
    if i < 30 or not (_hm(p["first_entry"]) <= _bar_end(ind, i) <= _hm(p["last_entry"])):
        return None
    if ctx["trades_today"] >= p["max_trades_per_ticker_day"]:
        return None
    if ind["et"][i - 1].date() != ind["et"][i].date():
        return None
    b, prev = bars[i], bars[i - 1]
    vw, pvw, a = ind["vwap"][i], ind["vwap"][i - 1], ind["atr"][i]
    loud = b["v"] > p["volume_mult"] * ind["vol20"][i - 1]
    if loud and prev["c"] <= pvw and b["c"] > vw and ind["ema9"][i] > ind["ema20"][i]:
        return {"side": 1, "stop": b["c"] - p["atr_stop"] * a, "target": b["c"] + p["atr_target"] * a, "why": "Reclaimed VWAP on volume"}
    if p["allow_shorts"] and loud and prev["c"] >= pvw and b["c"] < vw and ind["ema9"][i] < ind["ema20"][i]:
        return {"side": -1, "stop": b["c"] + p["atr_stop"] * a, "target": b["c"] - p["atr_target"] * a, "why": "Lost VWAP on volume"}
    return None


def vwap_exit(p, bars, ind, i, pos):
    vw = ind["vwap"][i]
    if pos["side"] == 1 and bars[i]["c"] < vw:
        return "Closed back below VWAP"
    if pos["side"] == -1 and bars[i]["c"] > vw:
        return "Closed back above VWAP"
    return None


# ---------- 4. Gapper: 5-min opening-range breakout on the premarket report's stocks in play ----------
# After Zarattini, Barbon & Aziz (2024), adapted to a cash account: long only, no leverage, and a stop at the
# opening bar's low instead of their 10%-of-ATR stop (which needs up to 4x leverage to size).
def gapper_entry(p, bars, ind, i, ctx):
    if ctx["trades_today"] >= 1:
        return None
    day = ind["et"][i].date()
    j = i
    while j > 0 and ind["et"][j - 1].date() == day:
        j -= 1
    first = bars[j]
    if j == i or (ind["et"][j].hour, ind["et"][j].minute) != (9, 30) or _bar_end(ind, i) > _hm(p["last_entry"]):
        return None
    if p["require_green_open"] and first["c"] <= first["o"]:
        return None
    c = bars[i]["c"]
    if c > first["h"] and (i == j + 1 or bars[i - 1]["c"] <= first["h"]):
        return {"side": 1, "stop": first["l"], "target": None,
                "why": f"In play: broke the opening 5-min high {first['h']:.2f}"}
    return None


def gapper_exit(p, bars, ind, i, pos):
    return None



# ---------- 5-7. Opening trades on stocks picked by scans.py over the premarket universe ----------
# The scans pick the stocks (setting "<universe>_today"); these functions only time the entry on the first
# 5-min bar and set the stop from a 14-day daily ATR built from the bot's own 5-min bars.
def daily_atr(bars, ind, i, n=14):
    """Average true range of the last n complete days before bar i's day (simple mean, as in stress/algos)."""
    day = ind["et"][i].date()
    days = []
    for k in range(i - 1, -1, -1):
        d = ind["et"][k].date()
        if d == day:
            continue
        b = bars[k]
        if not days or days[-1]["d"] != d:
            if len(days) == n + 1:
                break
            days.append({"d": d, "h": b["h"], "l": b["l"], "c": b["c"]})
        else:
            x = days[-1]
            x["h"], x["l"] = max(x["h"], b["h"]), min(x["l"], b["l"])
    days.reverse()
    if len(days) < n + 1:
        return None
    trs = [max(days[k]["h"] - days[k]["l"], abs(days[k]["h"] - days[k - 1]["c"]), abs(days[k]["l"] - days[k - 1]["c"]))
           for k in range(1, len(days))]
    return sum(trs) / len(trs)


def _first_bar(ind, i):
    t = ind["et"][i]
    return (t.hour, t.minute) == (9, 30)


def open_entry(p, bars, ind, i, ctx, why):
    """Buy at the close of the first 5-min bar; stop stop_atr x daily ATR below. Green bar required if green_open."""
    if not _first_bar(ind, i) or ctx["trades_today"]:
        return None
    b = bars[i]
    if p.get("green_open") and b["c"] <= b["o"]:
        return None
    a = daily_atr(bars, ind, i)
    if not a:
        return None
    return {"side": 1, "stop": b["c"] - p["stop_atr"] * a, "target": None, "why": why}


def orb_entry(p, bars, ind, i, ctx):
    return open_entry(p, bars, ind, i, ctx, "Most unusual opening volume, green first bar")


def loser_entry(p, bars, ind, i, ctx):
    return open_entry(p, bars, ind, i, ctx, "Worst 5-day loser in the universe")


def control_entry(p, bars, ind, i, ctx):
    return open_entry(p, bars, ind, i, ctx, "Random pick (control)")


def hold_exit(p, bars, ind, i, pos):
    return None


# ---------- 8-12. The next five of the 15 stress-tested algorithms (stress/algos, A2 A3 A4 A6 A7) ----------
# None passed the stress test; they run so the 30 days compare more ideas (a deliberate choice). Rules and
# defaults are the research versions', on the same six stocks (STUDY_TICKERS). When you port a research
# algorithm like this, check that the port trades the same as the research code. Daily figures come from the bot's own 5-min bars.
STUDY_TICKERS = ["AMD", "NVDA", "TSLA", "META", "AAPL", "MSFT"]  # stress/data.TECH


def _day_index(bars, ind):
    """Cached in ind (one per ticker per step): each bar's position in its day, and each day's (first, last) bar."""
    if "_j" not in ind:
        j, days, start = [], {}, 0
        for k in range(len(bars)):
            if k == 0 or ind["et"][k].date() != ind["et"][k - 1].date():
                start = k
            j.append(k - start)
            days[ind["et"][k].date()] = (start, k)
        ind["_j"], ind["_days"], ind["_daily"] = j, days, {}
    return ind["_j"], ind["_days"]


def _prior_days(bars, ind, i, n):
    """The last n complete days before bar i's day, as daily bars {d, o, h, l, c}."""
    _, days = _day_index(bars, ind)
    d = ind["et"][i].date()
    if d not in ind["_daily"]:
        out = []
        for dd in sorted(x for x in days if x < d):
            s, e = days[dd]
            seg = bars[s:e + 1]
            out.append({"d": dd, "o": seg[0]["o"], "h": max(b["h"] for b in seg), "l": min(b["l"] for b in seg),
                        "c": seg[-1]["c"]})
        ind["_daily"][d] = out
    return ind["_daily"][d][-n:] if n else []


def _atr_days(bars, ind, i, n=14):
    p = _prior_days(bars, ind, i, n + 1)
    if len(p) < n + 1:
        return None
    return sum(max(p[k]["h"] - p[k]["l"], abs(p[k]["h"] - p[k - 1]["c"]), abs(p[k]["l"] - p[k - 1]["c"]))
               for k in range(1, len(p))) / n


def _gap_pct(bars, ind, i):
    j, _ = _day_index(bars, ind)
    p = _prior_days(bars, ind, i, 1)
    return (bars[i - j[i]]["o"] / p[0]["c"] - 1) * 100 if p else None


def _end_min(ind, i):
    e = _bar_end(ind, i)
    return e.hour * 60 + e.minute


def gapfade_entry(p, bars, ind, i, ctx):
    """A2: gap down 1-6%; buy the first close back above the day's open (09:40-10:30); target yesterday's close."""
    j, _ = _day_index(bars, ind)
    if not (1 <= j[i] <= 11) or ctx["trades_today"]:
        return None
    g, prev = _gap_pct(bars, ind, i), _prior_days(bars, ind, i, 1)
    if g is None or not (-p["gap_max"] <= g <= -p["gap_min"]):
        return None
    s = i - j[i]
    o, c = bars[s]["o"], bars[i]["c"]
    if c <= o or any(q["c"] > o for q in bars[s + 1:i]):
        return None
    return {"side": 1, "stop": min(b["l"] for b in bars[s:i + 1]), "target": prev[0]["c"],
            "why": f"Gap {g:+.1f}% reclaimed the open"}


def gapgo_entry(p, bars, ind, i, ctx):
    """A3: gap up 1-8% that is above the open and VWAP after 30 min; the 2 biggest such gaps among the stocks."""
    j, _ = _day_index(bars, ind)
    n = int(p["confirm_bars"])
    if j[i] != n - 1 or ctx["trades_today"]:
        return None

    def holds(b, x, k):
        g = _gap_pct(b, x, k)
        ok = g is not None and p["gap_min"] <= g <= 8.0 and b[k]["c"] > b[k - n + 1]["o"] and b[k]["c"] > x["vwap"][k]
        return g if ok else None
    g = holds(bars, ind, i)
    if g is None:
        return None
    ts, cands = bars[i]["ts"], []
    for t, (b, x) in (ctx.get("peers") or {}).items():
        if t not in STUDY_TICKERS:
            continue
        k = next((k for k in range(len(b) - 1, -1, -1) if b[k]["ts"] == ts), None)  # same bar for that stock
        if k is not None and _day_index(b, x)[0][k] == n - 1:
            gg = holds(b, x, k)
            if gg is not None:
                cands.append((gg, b[k]["c"]))
    top = sorted(cands, reverse=True)[:2]
    if (g, bars[i]["c"]) not in top:
        return None
    return {"side": 1, "stop": min(b["l"] for b in bars[i - n + 1:i + 1]), "target": None,
            "why": f"Gap {g:+.1f}% held for {n * 5} min"}


def vwapband_entry(p, bars, ind, i, ctx):
    """A4: a green 5-min bar 2 standard deviations below the session VWAP, 10:30-14:30."""
    if not (10 * 60 + 30 <= _end_min(ind, i) <= 14 * 60 + 30) or ctx["trades_today"]:
        return None
    j, _ = _day_index(bars, ind)
    pv = v = pv2 = 0.0
    for q in bars[i - j[i]:i + 1]:
        tp = (q["h"] + q["l"] + q["c"]) / 3
        pv, v, pv2 = pv + tp * q["v"], v + q["v"], pv2 + tp * tp * q["v"]
    if not v:
        return None
    vw = pv / v
    sg = max(0.0, pv2 / v - vw * vw) ** 0.5
    b = bars[i]
    if sg <= 0 or not (b["c"] < vw - p["band_sigma"] * sg and b["c"] > b["o"]):
        return None
    return {"side": 1, "stop": b["c"] - p["stop_sigma"] * sg, "target": None,
            "why": f"{(vw - b['c']) / sg:.1f}σ below VWAP, green bar"}


def vwapband_exit(p, bars, ind, i, pos):
    return "Back at VWAP" if bars[i]["c"] >= ind["vwap"][i] else None


def squeeze_entry(p, bars, ind, i, ctx):
    """A6: the 11:30-13:30 range is narrow (<= 0.35 daily ATR); buy the first afternoon close above its high."""
    if not (13 * 60 + 35 <= _end_min(ind, i) <= 15 * 60) or ctx["trades_today"]:
        return None
    a = _atr_days(bars, ind, i)
    if not a:
        return None
    j, _ = _day_index(bars, ind)
    s, n, last = i - j[i], int(p["range_bars"]), 47  # bar 47 ends 13:30
    if j[i] <= last:
        return None
    r = bars[s + last - n + 1:s + last + 1]
    hi, lo = max(q["h"] for q in r), min(q["l"] for q in r)
    if hi - lo > p["width_atr"] * a:
        return None
    c = bars[i]["c"]
    if c > hi and all(q["c"] <= hi for q in bars[s + last + 1:i]):
        return {"side": 1, "stop": lo, "target": None, "why": f"Broke midday squeeze high {hi:.2f}"}
    return None


def nr4_entry(p, bars, ind, i, ctx):
    """A7: yesterday had the narrowest range of the last 4 days; buy the first close above yesterday's high."""
    if not (9 * 60 + 45 <= _end_min(ind, i) <= 14 * 60) or ctx["trades_today"]:
        return None
    n = int(p["nr_n"])
    pr = _prior_days(bars, ind, i, n)
    if len(pr) < n:
        return None
    y = pr[-1]
    if y["h"] - y["l"] > min(q["h"] - q["l"] for q in pr):
        return None
    j, _ = _day_index(bars, ind)
    c, s = bars[i]["c"], i - j[i]
    if c > y["h"] and all(q["c"] <= y["h"] for q in bars[s:i]):
        return {"side": 1, "stop": y["l"], "target": None, "why": f"NR{n} day, broke yesterday's high {y['h']:.2f}"}
    return None


# ---------- 13. Swing RSI(2): Connors' daily RSI(2) pullback, held overnight (stress/algos S13) ----------
# The one swing algorithm that did well in the stress test: 5 of 6 checks, positive on both stock sets, but only
# 8 trades in development and 2 per set in the holdout. Decisions on the bar that ends 10 minutes before the close.
# Daily history: Yahoo daily bars, fetched once per stock per day (DAILY["source"] replaces them in tests).
DAILY = {"source": None, "cache": {}}


def _daily_before(t, d, n):
    """The last n complete daily bars of stock t before day d ({d, o, h, l, c}); [] if they can't be fetched."""
    if DAILY["source"] is not None:
        rows = DAILY["source"].get(t, [])
    else:
        if (t, d) not in DAILY["cache"]:
            try:
                DAILY["cache"][(t, d)] = market.fetch_daily(t)
            except Exception:
                return []  # no trade; an open position keeps its stop and time stop
        rows = DAILY["cache"][(t, d)]
    rows = [r for r in rows if r["d"] < d]
    return rows[-n:]


def rsi_wilder(closes, n=2):
    ag = al = None
    for k in range(1, len(closes)):
        ch = closes[k] - closes[k - 1]
        g, l = max(ch, 0), max(-ch, 0)
        if ag is None:
            ag, al = g, l
        else:
            ag, al = (ag * (n - 1) + g) / n, (al * (n - 1) + l) / n
    if ag is None:
        return None
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def _decision_bar(ind, i):
    c = market.close_time(ind["et"][i].date())
    return _end_min(ind, i) == c.hour * 60 + c.minute - 10


def _days_held(bars, ind, i, pos):
    """Trading days since the entry day, up to and including bar i's day."""
    _, days = _day_index(bars, ind)
    d0, d = datetime.fromtimestamp(pos["entry_ts"], ET).date(), ind["et"][i].date()
    return sum(1 for x in days if d0 < x <= d)


def swing_entry(p, bars, ind, i, ctx):
    if not _decision_bar(ind, i) or ctx["trades_today"]:
        return None
    c = bars[i]["c"]
    hist = [q["c"] for q in _daily_before(ind["ticker"], ind["et"][i].date(), 200)]
    if len(hist) < 199:
        return None
    closes = hist[-199:] + [c]
    if c <= sum(closes) / 200:
        return None
    r = rsi_wilder(hist[-50:] + [c], 2)
    if r is None or r >= p["rsi_entry"]:
        return None
    return {"side": 1, "stop": c * (1 - p["stop_pct"] / 100), "target": None, "why": f"Daily RSI(2) {r:.0f} above SMA200"}


def swing_exit(p, bars, ind, i, pos):
    if not _decision_bar(ind, i):
        return None
    held = _days_held(bars, ind, i, pos)
    if held < 1:
        return None
    n, c = int(p["sma_exit"]), bars[i]["c"]
    hist = [q["c"] for q in _daily_before(ind["ticker"], ind["et"][i].date(), n - 1)] + [c]
    if len(hist) == n and c > sum(hist) / n:
        return f"Close above SMA{n}"
    return f"{p['max_days']}-day time stop" if held >= p["max_days"] else None


COMMON = {"risk_pct": 1.0, "max_position_pct": 25.0, "max_positions": 4}

STRATEGIES = {
    "pullback": {
        "retired": True,  # retired; kept for replay and research
        "name": "Pullback", "blurb": "Buys a sharp short dip in a watchlist stock that is trending up; sells when it bounces back.",
        "entry": pullback_entry, "exit": pullback_exit,
        "defaults": {**COMMON, "rsi_entry": 10, "rsi_exit": 70, "atr_stop": 2.0, "first_entry": "09:45",
                     "last_entry": "15:30", "max_trades_per_ticker_day": 3},
    },
    "breakout": {
        "name": "Breakout", "blurb": "Trades the first move out of the opening range on the watchlist, up or down, with a stop at the other side.",
        "entry": breakout_entry, "exit": breakout_exit,
        "defaults": {**COMMON, "range_minutes": 60, "r_multiple": 1.0, "max_range_pct": 4.0, "last_entry": "14:00",
                     "allow_shorts": True, "flat_at": ""},
    },
    "vwap": {
        "retired": True,  # retired; kept for replay and research
        "name": "VWAP Trend", "blurb": "Follows a watchlist stock across today's average price (VWAP) when heavy volume confirms the move.",
        "entry": vwap_entry, "exit": vwap_exit,
        "defaults": {**COMMON, "volume_mult": 1.3, "atr_stop": 1.5, "atr_target": 2.5, "first_entry": "09:45",
                     "last_entry": "15:30", "max_trades_per_ticker_day": 2, "allow_shorts": True},
    },
    "gapper": {
        "name": "Gapper", "blurb": "Buys a stock gapping up on heavy premarket trading (from the 09:00 report) when it breaks above its first 5-minute high.",
        "entry": gapper_entry, "exit": gapper_exit, "universe": "in_play",
        "defaults": {**COMMON, "max_position_pct": 50.0, "max_positions": 2, "last_entry": "11:00",
                     "require_green_open": True},
    },
    "orb": {
        "name": "ORB In Play", "blurb": "At 09:35 buys the 2 rising stocks with the most unusual volume at the open and holds to the close.",
        "entry": orb_entry, "exit": hold_exit, "universe": "orb",
        "defaults": {**COMMON, "risk_pct": 100.0, "max_position_pct": 50.0, "max_positions": 2, "stop_atr": 0.10,
                     "green_open": True},
    },
    "loser": {
        "name": "Weekly Loser", "blurb": "At 09:35 buys the stock that fell most over the past week (3% or more), betting on a bounce; holds to the close.",
        "entry": loser_entry, "exit": hold_exit, "universe": "loser",
        "defaults": {**COMMON, "risk_pct": 100.0, "max_position_pct": 50.0, "max_positions": 2, "stop_atr": 1.0,
                     "green_open": False},
    },
    "control": {
        "name": "Control", "blurb": "The yardstick: Weekly Loser's trade on a random stock. A bot that can't beat it shows no stock-picking skill.",
        "entry": control_entry, "exit": hold_exit, "universe": "control",
        "defaults": {**COMMON, "risk_pct": 100.0, "max_position_pct": 50.0, "max_positions": 2, "stop_atr": 1.0,
                     "green_open": False},
    },
    "gapfade": {
        "name": "Gap Fade", "blurb": "A tech stock that opened 1-6% lower: buys when it climbs back above its open, aims for yesterday's close.",
        "entry": gapfade_entry, "exit": hold_exit, "tickers": STUDY_TICKERS,
        "defaults": {**COMMON, "risk_pct": 100.0, "max_position_pct": 50.0, "max_positions": 2, "gap_min": 1.0, "gap_max": 6.0},
    },
    "gapgo": {
        "name": "Gap and Go", "blurb": "A tech stock that opened 1-8% higher and is still above its open and VWAP at 10:00: buys the 2 biggest gaps, holds to the close.",
        "entry": gapgo_entry, "exit": hold_exit, "tickers": STUDY_TICKERS,
        "defaults": {**COMMON, "risk_pct": 100.0, "max_position_pct": 50.0, "max_positions": 2, "gap_min": 1.0, "confirm_bars": 6},
    },
    "vwapband": {
        "name": "VWAP Band", "blurb": "Buys a tech stock stretched 2 standard deviations below today's VWAP on a green bar; sells back at VWAP.",
        "entry": vwapband_entry, "exit": vwapband_exit, "tickers": STUDY_TICKERS,
        "defaults": {**COMMON, "risk_pct": 100.0, "max_position_pct": 50.0, "max_positions": 2, "band_sigma": 2.0, "stop_sigma": 1.0},
    },
    "squeeze": {
        "name": "Midday Squeeze", "blurb": "A tech stock that traded in a narrow range 11:30-13:30: buys the afternoon break above that range.",
        "entry": squeeze_entry, "exit": hold_exit, "tickers": STUDY_TICKERS,
        "defaults": {**COMMON, "risk_pct": 100.0, "max_position_pct": 50.0, "max_positions": 2, "width_atr": 0.35, "range_bars": 24},
    },
    "nr4": {
        "name": "NR4 Breakout", "blurb": "After a tech stock's quietest day of the last 4, buys the break above that day's high.",
        "entry": nr4_entry, "exit": hold_exit, "tickers": STUDY_TICKERS,
        "defaults": {**COMMON, "risk_pct": 100.0, "max_position_pct": 50.0, "max_positions": 2, "nr_n": 4},
    },
    "swing": {
        "name": "Swing RSI(2)", "blurb": "Buys a tech stock in a long uptrend after a sharp 2-day dip, just before the close; holds overnight until it bounces (up to 10 days).",
        "entry": swing_entry, "exit": swing_exit, "tickers": STUDY_TICKERS, "swing": True,
        "defaults": {**COMMON, "risk_pct": 100.0, "max_position_pct": 50.0, "max_positions": 2, "rsi_entry": 10.0, "sma_exit": 5, "max_days": 10, "stop_pct": 20.0},
    },
}
