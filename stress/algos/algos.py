"""The 15 pre-registered algorithms (see HYPOTHESES.md), in the format of strategies.STRATEGIES.

    entry(p, bars, ind, i, ctx) -> None or {"side": 1, "stop", "target"|None, "why"}
    exit(p, bars, ind, i, pos)  -> None or a reason string (exit at bar i's close)

All are long only. Several need information the live engine does not pass to a strategy (other tickers' bars, SPY,
daily history longer than the 5-min window). In this research setup `prepare()` builds that context once per run from
the cached data; every feature uses only data available at the time of the bar. See REPORT.md for what a live
version would need.

Swing algorithms (S13-S15) have `"swing": True` in their defaults; the runner's engine subclass then skips the 15:55
flatten for them. They make all decisions on the bar that ends 10 minutes before the close (15:50).
"""
import math
from datetime import datetime

from market import ET, close_time

CTX = {"info": {}, "daily": {}, "dix": {}, "spy": None, "universe": [], "cache": {}}
COMMON = {"risk_pct": 100.0, "max_position_pct": 50.0, "max_positions": 2}


# ---------- context ----------
class Info:
    """Per-ticker intraday bookkeeping for one run."""

    def __init__(self, t, bars):
        self.t, self.bars = t, bars
        n = len(bars)
        self.date, self.j, self.start, self.endm = [None] * n, [0] * n, [0] * n, [0] * n
        self.days = {}  # date -> (first index, last index)
        self.sigma = [0.0] * n
        self.ts_index = {}
        cur, s = None, 0
        pv = v = pv2 = 0.0
        for i, b in enumerate(bars):
            dt = datetime.fromtimestamp(b["ts"], ET)
            d = dt.date()
            if d != cur:
                cur, s = d, i
                pv = v = pv2 = 0.0
            self.date[i], self.start[i], self.j[i] = d, s, i - s
            self.endm[i] = dt.hour * 60 + dt.minute + 5
            self.days[d] = (s, i)
            self.ts_index[b["ts"]] = i
            tp = (b["h"] + b["l"] + b["c"]) / 3
            pv += tp * b["v"]
            v += b["v"]
            pv2 += tp * tp * b["v"]
            if v:
                vw = pv / v
                self.sigma[i] = math.sqrt(max(0.0, pv2 / v - vw * vw))
        self.day_list = sorted(self.days)
        self.vwap = []
        cur = None
        for i, b in enumerate(bars):
            if self.date[i] != cur:
                cur, pv, v = self.date[i], 0.0, 0.0
            tp = (b["h"] + b["l"] + b["c"]) / 3
            pv += tp * b["v"]
            v += b["v"]
            self.vwap.append(pv / v if v else tp)

    def decision_bar(self, i):
        """True on the bar that ends 10 minutes before the close (15:50 on a normal day)."""
        c = close_time(self.date[i])
        return self.endm[i] == c.hour * 60 + c.minute - 10

    def day_bars(self, d, upto=None):
        s, e = self.days[d]
        return self.bars[s:(e if upto is None else upto) + 1]


def daily_from_hourly(hourly, last_day):
    """Daily OHLCV bars from regular-session 1-hour bars, complete days up to last_day."""
    out, cur = [], None
    for b in hourly:
        d = datetime.fromtimestamp(b["ts"], ET).date()
        if d > last_day:
            break
        if d != cur:
            cur = d
            out.append({"d": d, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]})
        else:
            x = out[-1]
            x["h"], x["l"], x["c"], x["v"] = max(x["h"], b["h"]), min(x["l"], b["l"]), b["c"], x["v"] + b["v"]
    return out


def prepare(bars_by_ticker, spy_bars, daily_by_ticker):
    """Register one run's bars (the same list objects the engine will see) and daily history."""
    CTX["info"] = {id(b): Info(t, b) for t, b in bars_by_ticker.items()}
    CTX["by_t"] = {t: CTX["info"][id(b)] for t, b in bars_by_ticker.items()}
    CTX["spy"] = Info("SPY", spy_bars)
    CTX["daily"] = daily_by_ticker
    CTX["dix"] = {t: {x["d"]: k for k, x in enumerate(v)} for t, v in daily_by_ticker.items()}
    CTX["universe"] = [t for t in bars_by_ticker if t != "SPY"]
    CTX["cache"] = {}


def _info(bars):
    return CTX["info"][id(bars)]


def _cached(key, fn):
    c = CTX["cache"]
    if key not in c:
        c[key] = fn()
    return c[key]


# ---------- daily features (prior days only) ----------
def prior(t, d, n):
    """The last n complete daily bars strictly before day d."""
    k = CTX["dix"][t].get(d)
    if k is None:
        k = sum(1 for x in CTX["daily"][t] if x["d"] < d)
    return CTX["daily"][t][max(0, k - n):k]


def prev_close(t, d):
    p = prior(t, d, 1)
    return p[-1]["c"] if p else None


def atr_d(t, d, n=14):
    def f():
        p = prior(t, d, n + 1)
        if len(p) < n + 1:
            return None
        trs = [max(p[k]["h"] - p[k]["l"], abs(p[k]["h"] - p[k - 1]["c"]), abs(p[k]["l"] - p[k - 1]["c"])) for k in range(1, len(p))]
        return sum(trs) / len(trs)
    return _cached(("atr", t, d, n), f)


def adr_pct(t, d, n=20):
    def f():
        p = prior(t, d, n)
        return sum(x["h"] / x["l"] - 1 for x in p) / len(p) * 100 if len(p) == n else None
    return _cached(("adr", t, d, n), f)


def ret_n(t, d, n):
    p = prior(t, d, n + 1)
    return p[-1]["c"] / p[0]["c"] - 1 if len(p) == n + 1 else None


def beta(t, d, n=60):
    def f():
        a, m = prior(t, d, n + 1), prior("SPY", d, n + 1)
        if len(a) < n + 1 or len(m) < n + 1:
            return 1.0
        ra = [a[k]["c"] / a[k - 1]["c"] - 1 for k in range(1, len(a))]
        rm = [m[k]["c"] / m[k - 1]["c"] - 1 for k in range(1, len(m))]
        ma, mm = sum(ra) / n, sum(rm) / n
        cov = sum((x - ma) * (y - mm) for x, y in zip(ra, rm))
        var = sum((y - mm) ** 2 for y in rm)
        return cov / var if var else 1.0
    return _cached(("beta", t, d, n), f)


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


def _spy_at(ts):
    s = CTX["spy"]
    return s.ts_index.get(ts)


def _sig(stop, why, target=None):
    return {"side": 1, "stop": stop, "target": target, "why": why}


# ---------- A1 ORB-5 on stocks in play ----------
def _rvol_first(t, d, lookback=14):
    info = CTX["by_t"][t]
    k = info.day_list.index(d)
    if k < lookback:
        return None
    hist = [info.bars[info.days[x][0]]["v"] for x in info.day_list[k - lookback:k]]
    avg = sum(hist) / lookback
    return info.bars[info.days[d][0]]["v"] / avg if avg else None


def a1_entry(p, bars, ind, i, ctx):
    x = _info(bars)
    if x.j[i] != 0 or ctx["trades_today"]:
        return None
    d, b = x.date[i], bars[i]

    def ranked():
        cands = []
        for t in CTX["universe"]:
            o = CTX["by_t"][t]
            if d not in o.days:
                continue
            fb = o.bars[o.days[d][0]]
            rv = _rvol_first(t, d)
            if rv is not None and rv >= p["rvol_min"] and fb["c"] > fb["o"]:
                cands.append((rv, t))
        return [t for _, t in sorted(cands, reverse=True)[:2]]
    if x.t not in _cached(("a1", d, p["rvol_min"]), ranked):
        return None
    a = atr_d(x.t, d)
    if not a:
        return None
    return _sig(b["c"] - p["stop_atr"] * a, f"ORB-5: green first bar, RVOL {_rvol_first(x.t, d):.1f}")


def none_exit(p, bars, ind, i, pos):
    return None


# ---------- A2 gap-down fade ----------
def a2_entry(p, bars, ind, i, ctx):
    x = _info(bars)
    if not (1 <= x.j[i] <= 11) or ctx["trades_today"]:
        return None
    d = x.date[i]
    pc = prev_close(x.t, d)
    if not pc:
        return None
    s = x.start[i]
    o = bars[s]["o"]
    gap = (o / pc - 1) * 100
    if not (-p["gap_max"] <= gap <= -p["gap_min"]):
        return None
    c = bars[i]["c"]
    if c <= o or any(q["c"] > o for q in bars[s + 1:i]):  # first close above the open from 09:40
        return None
    low = min(b["l"] for b in bars[s:i + 1])
    return _sig(low, f"Gap {gap:+.1f}% reclaimed the open", target=pc)


# ---------- A3 gap-and-go ----------
def _gap(t, d):
    o = CTX["by_t"][t]
    pc = prev_close(t, d)
    if d not in o.days or not pc:
        return None
    return (o.bars[o.days[d][0]]["o"] / pc - 1) * 100


def a3_entry(p, bars, ind, i, ctx):
    x = _info(bars)
    n = int(p["confirm_bars"])
    if x.j[i] != n - 1 or ctx["trades_today"]:
        return None
    d, s, c = x.date[i], x.start[i], bars[i]["c"]
    g = _gap(x.t, d)
    if g is None or not (p["gap_min"] <= g <= 8.0):
        return None
    if not (c > bars[s]["o"] and c > x.vwap[i]):
        return None

    def ranked():
        cands = []
        for t in CTX["universe"]:
            o = CTX["by_t"][t]
            gg = _gap(t, d)
            if gg is None or not (p["gap_min"] <= gg <= 8.0) or d not in o.days:
                continue
            k = o.days[d][0] + n - 1
            if k > o.days[d][1]:
                continue
            if o.bars[k]["c"] > o.bars[o.days[d][0]]["o"] and o.bars[k]["c"] > o.vwap[k]:
                cands.append((gg, t))
        return [t for _, t in sorted(cands, reverse=True)[:2]]
    if x.t not in _cached(("a3", d, p["gap_min"], n), ranked):
        return None
    return _sig(min(b["l"] for b in bars[s:i + 1]), f"Gap {g:+.1f}% held for {n * 5} min")


# ---------- A4 VWAP sigma-band reversion ----------
def a4_entry(p, bars, ind, i, ctx):
    x = _info(bars)
    if not (10 * 60 + 30 <= x.endm[i] <= 14 * 60 + 30) or ctx["trades_today"]:
        return None
    b, sg, vw = bars[i], x.sigma[i], x.vwap[i]
    if sg <= 0 or not (b["c"] < vw - p["band_sigma"] * sg and b["c"] > b["o"]):
        return None
    return _sig(b["c"] - p["stop_sigma"] * sg, f"{(vw - b['c']) / sg:.1f}σ below VWAP, green bar")


def a4_exit(p, bars, ind, i, pos):
    return "Back at VWAP" if bars[i]["c"] >= _info(bars).vwap[i] else None


# ---------- A5 VWAP trend hold ----------
def a5_entry(p, bars, ind, i, ctx):
    x = _info(bars)
    if not (9 * 60 + 30 + p["start_min"] <= x.endm[i] <= 14 * 60) or ctx["trades_today"]:
        return None
    b, s = bars[i], x.start[i]
    if not (b["c"] > x.vwap[i] and b["c"] > bars[s]["o"]):
        return None
    k = _spy_at(b["ts"])
    if k is None or CTX["spy"].bars[k]["c"] <= CTX["spy"].vwap[k]:
        return None
    return _sig(b["c"] * 0.97, "Above VWAP and open, SPY above its VWAP")


def a5_exit(p, bars, ind, i, pos):
    return "Closed below VWAP band" if bars[i]["c"] < _info(bars).vwap[i] * (1 - p["buffer_pct"] / 100) else None


# ---------- A6 midday squeeze breakout ----------
def a6_entry(p, bars, ind, i, ctx):
    x = _info(bars)
    if not (13 * 60 + 35 <= x.endm[i] <= 15 * 60) or ctx["trades_today"]:
        return None
    d, s = x.date[i], x.start[i]
    a = atr_d(x.t, d)
    if not a:
        return None
    n = int(p["range_bars"])
    end_j = 47  # the bar ending 13:30

    def rng():
        if s + end_j >= len(bars) or x.date[s + end_j] != d:
            return None
        r = bars[s + end_j - n + 1:s + end_j + 1]
        return max(q["h"] for q in r), min(q["l"] for q in r)
    hl = _cached(("a6", x.t, d, n), rng)
    if not hl:
        return None
    hi, lo = hl
    if (hi - lo) > p["width_atr"] * a:
        return None
    c = bars[i]["c"]
    if c > hi and all(q["c"] <= hi for q in bars[s + end_j + 1:i]):
        return _sig(lo, f"Broke midday squeeze high {hi:.2f}")
    return None


# ---------- A7 NR-n daily breakout ----------
def a7_entry(p, bars, ind, i, ctx):
    x = _info(bars)
    if not (9 * 60 + 45 <= x.endm[i] <= 14 * 60) or ctx["trades_today"]:
        return None
    d = x.date[i]
    n = int(p["nr_n"])
    pr = prior(x.t, d, n)
    if len(pr) < n:
        return None
    y = pr[-1]
    if (y["h"] - y["l"]) > min(q["h"] - q["l"] for q in pr):
        return None
    c, s = bars[i]["c"], x.start[i]
    if c > y["h"] and all(q["c"] <= y["h"] for q in bars[s:i]):
        return _sig(y["l"], f"NR{n} day, broke yesterday's high {y['h']:.2f}")
    return None


# ---------- A8 focus-group trend-day long ----------
def focus(t, d, adr_min=4.0):
    a, r, m = adr_pct(t, d), ret_n(t, d, 20), ret_n("SPY", d, 20)
    return a is not None and r is not None and m is not None and a >= adr_min and r > m


def a8_entry(p, bars, ind, i, ctx):
    x = _info(bars)
    if not (10 * 60 <= x.endm[i] <= 13 * 60) or ctx["trades_today"]:
        return None
    d = x.date[i]
    if not focus(x.t, d, p["adr_min"]):
        return None
    b, s = bars[i], x.start[i]
    if not (b["c"] >= bars[s]["o"] * (1 + p["move_min"] / 100) and b["c"] > x.vwap[i] and ind["ema9"][i] > ind["ema20"][i]):
        return None
    adr = adr_pct(x.t, d)
    return _sig(b["c"] * (1 - p["stop_adr"] * adr / 100), f"Focus stock (ADR {adr:.1f}%) trending up")


def a8_exit(p, bars, ind, i, pos):
    return "Closed below VWAP" if bars[i]["c"] < _info(bars).vwap[i] else None


# ---------- A9 relative-strength leader ----------
def a9_entry(p, bars, ind, i, ctx):
    x = _info(bars)
    n = int(p["entry_bar"])
    if x.j[i] != n or ctx["trades_today"]:
        return None
    d = x.date[i]

    def leader():
        k = _spy_at(bars[i]["ts"])
        spc = prev_close("SPY", d)
        if k is None or not spc:
            return None
        sr = CTX["spy"].bars[k]["c"] / spc - 1
        if sr <= 0:
            return None
        best = None
        for t in CTX["universe"]:
            o = CTX["by_t"][t]
            pc = prev_close(t, d)
            if d not in o.days or not pc or o.days[d][0] + n > o.days[d][1]:
                continue
            ex = o.bars[o.days[d][0] + n]["c"] / pc - 1 - sr
            if best is None or ex > best[0]:
                best = (ex, t)
        return best
    best = _cached(("a9", d, n), leader)
    if not best or best[1] != x.t or best[0] * 100 < p["ex_min"]:
        return None
    return _sig(min(q["l"] for q in bars[x.start[i]:i + 1]), f"RS leader, {best[0] * 100:+.1f}% vs SPY")


# ---------- A10 residual reversal ----------
def _resid(bars, i):
    x = _info(bars)
    s = x.start[i]
    k = _spy_at(bars[i]["ts"])
    if k is None:
        return None
    sp = CTX["spy"]
    so = sp.bars[sp.start[k]]["o"]
    return (bars[i]["c"] / bars[s]["o"] - 1) - beta(x.t, x.date[i]) * (sp.bars[k]["c"] / so - 1)


def a10_entry(p, bars, ind, i, ctx):
    x = _info(bars)
    if not (11 * 60 <= x.endm[i] <= 14 * 60 + 30) or ctx["trades_today"]:
        return None
    adr = adr_pct(x.t, x.date[i])
    b = bars[i]
    if not adr or b["c"] <= b["o"]:
        return None
    r = _resid(bars, i)
    if r is None or r * 100 > -p["entry_adr"] * adr:
        return None
    return _sig(b["c"] * (1 - p["stop_adr"] * adr / 100), f"Residual {r * 100:+.1f}% vs SPY")


def a10_exit(p, bars, ind, i, pos):
    adr = adr_pct(_info(bars).t, _info(bars).date[i])
    r = _resid(bars, i)
    return "Residual reverted" if r is not None and adr and r * 100 >= -p["exit_adr"] * adr else None


# ---------- A11 half-hour periodicity ----------
def _best_slot(t, d, lookback, thr):
    def f():
        o = CTX["by_t"][t]
        k = o.day_list.index(d)
        if k < lookback:
            return None
        sums = [0.0] * 12
        for dd in o.day_list[k - lookback:k]:
            s, e = o.days[dd]
            if e - s + 1 < 78:
                return None
            for sl in range(1, 12):
                sums[sl] += o.bars[s + 6 * sl + 5]["c"] / o.bars[s + 6 * sl - 1]["c"] - 1
        best = max(range(1, 12), key=lambda sl: sums[sl])
        m = sums[best] / lookback * 1e4
        return best if m >= thr else None
    return _cached(("a11", t, d, lookback, thr), f)


def a11_entry(p, bars, ind, i, ctx):
    x = _info(bars)
    if ctx["trades_today"]:
        return None
    sl = _best_slot(x.t, x.date[i], int(p["lookback"]), p["thr_bp"])
    if sl is None or x.j[i] != 6 * sl - 1:
        return None
    return _sig(bars[i]["c"] * 0.99, f"Best half-hour slot {sl}")


def a11_exit(p, bars, ind, i, pos):
    x = _info(bars)
    sl = _best_slot(x.t, x.date[i], int(p["lookback"]), p["thr_bp"])
    return "Slot end" if sl is None or x.j[i] >= 6 * sl + 5 else None


# ---------- A12 weekly-loser intraday reversal ----------
def a12_entry(p, bars, ind, i, ctx):
    x = _info(bars)
    if x.j[i] != 0 or ctx["trades_today"]:
        return None
    d, n = x.date[i], int(p["lookback"])

    def loser():
        rs = [(ret_n(t, d, n), t) for t in CTX["universe"] if d in CTX["by_t"][t].days]
        rs = [r for r in rs if r[0] is not None]
        return min(rs) if rs else None
    w = _cached(("a12", d, n), loser)
    a = atr_d(x.t, d)
    if not w or w[1] != x.t or w[0] * 100 > -p["loss_min"] or not a:
        return None
    return _sig(bars[i]["c"] - p["stop_atr"] * a, f"Weekly loser {w[0] * 100:+.1f}% over {n} days")


# ---------- swing helpers ----------
def _days_held(x, pos, i):
    d0 = datetime.fromtimestamp(pos["entry_ts"], ET).date()
    return sum(1 for d in x.day_list if d0 < d <= x.date[i])


# ---------- S13 Connors daily RSI(2) ----------
def s13_entry(p, bars, ind, i, ctx):
    x = _info(bars)
    if not x.decision_bar(i) or ctx["trades_today"]:
        return None
    c = bars[i]["c"]
    hist = [q["c"] for q in prior(x.t, x.date[i], 200)]
    if len(hist) < 199:
        return None
    closes = hist[-199:] + [c]
    if c <= sum(closes) / 200:
        return None
    r = rsi_wilder(hist[-50:] + [c], 2)
    if r is None or r >= p["rsi_entry"]:
        return None
    return _sig(c * 0.80, f"Daily RSI(2) {r:.0f} above SMA200")


def s13_exit(p, bars, ind, i, pos):
    x = _info(bars)
    if not x.decision_bar(i) or _days_held(x, pos, i) < 1:
        return None
    n = int(p["sma_exit"])
    c = bars[i]["c"]
    hist = [q["c"] for q in prior(x.t, x.date[i], n - 1)] + [c]
    if c > sum(hist) / len(hist):
        return f"Close above SMA{n}"
    return "10-day time stop" if _days_held(x, pos, i) >= 10 else None


# ---------- S14 trend-day close-strong, hold overnight ----------
def s14_entry(p, bars, ind, i, ctx):
    x = _info(bars)
    if not x.decision_bar(i) or ctx["trades_today"]:
        return None
    adr = adr_pct(x.t, x.date[i])
    if not adr:
        return None
    s, c = x.start[i], bars[i]["c"]
    today = bars[s:i + 1]
    hi, lo = max(q["h"] for q in today), min(q["l"] for q in today)
    move = (c / bars[s]["o"] - 1) * 100
    if hi <= lo or move < p["move_adr"] * adr or (c - lo) / (hi - lo) < 1 - p["top_frac"] or c <= x.vwap[i]:
        return None
    return _sig(c * (1 - adr / 100), f"Trend day {move:+.1f}%, closing near the high")


def s14_exit(p, bars, ind, i, pos):
    x = _info(bars)
    return "Next-day close" if x.decision_bar(i) and _days_held(x, pos, i) >= 1 else None


# ---------- S15 high-ADR 20-day breakout ----------
def s15_entry(p, bars, ind, i, ctx):
    x = _info(bars)
    if not x.decision_bar(i) or ctx["trades_today"]:
        return None
    d, c = x.date[i], bars[i]["c"]
    adr = adr_pct(x.t, d)
    n = int(p["lookback"])
    pr = prior(x.t, d, n)
    if not adr or adr < p["adr_min"] or len(pr) < n or c <= max(q["h"] for q in pr):
        return None
    return _sig(c * (1 - 2 * adr / 100), f"{n}-day high breakout, ADR {adr:.1f}%")


def s15_exit(p, bars, ind, i, pos):
    x = _info(bars)
    if not x.decision_bar(i) or _days_held(x, pos, i) < 1:
        return None
    c = bars[i]["c"]
    hist = [q["c"] for q in prior(x.t, x.date[i], 9)] + [c]
    if c < sum(hist) / len(hist):
        return "Close below SMA10"
    return "10-day time stop" if _days_held(x, pos, i) >= 10 else None


def _s(name, blurb, entry, exit_, key, **params):
    return {"name": name, "blurb": blurb, "entry": entry, "exit": exit_, "defaults": {**COMMON, **params}, "key": key}


ALGOS = {
    "a01_orb5": _s("A1 ORB-5 in play", "5-min opening range, green first bar, RVOL ≥ 1, top 2 by RVOL; tight ATR stop; hold to close.",
                   a1_entry, none_exit, ["rvol_min", "stop_atr"], rvol_min=1.0, stop_atr=0.10),
    "a02_gapfade": _s("A2 Gap-down fade", "Gap down 1–6%, buy the first reclaim of the open, target the prior close.",
                      a2_entry, none_exit, ["gap_min", "gap_max"], gap_min=1.0, gap_max=6.0),
    "a03_gapgo": _s("A3 Gap-and-go", "Gap up ≥ 1% that holds above open and VWAP for 30 min; top 2 by gap.",
                    a3_entry, none_exit, ["gap_min", "confirm_bars"], gap_min=1.0, confirm_bars=6),
    "a04_vwapband": _s("A4 VWAP σ-band reversion", "Buy a green bar 2σ below VWAP; exit at VWAP.",
                       a4_entry, a4_exit, ["band_sigma", "stop_sigma"], band_sigma=2.0, stop_sigma=1.0),
    "a05_vwaptrend": _s("A5 VWAP trend hold", "Above VWAP and the open with SPY above its VWAP; exit below VWAP band.",
                        a5_entry, a5_exit, ["buffer_pct", "start_min"], buffer_pct=0.25, start_min=30),
    "a06_squeeze": _s("A6 Midday squeeze breakout", "Narrow 11:30–13:30 range, buy the afternoon break of its high.",
                      a6_entry, none_exit, ["width_atr", "range_bars"], width_atr=0.35, range_bars=24),
    "a07_nr4": _s("A7 NR4 breakout", "After an NR4 day, buy the break of yesterday's high.",
                  a7_entry, none_exit, ["nr_n"], nr_n=4),
    "a08_focus": _s("A8 Focus-group trend day", "ADR ≥ 4% + relative momentum; trend-day long above VWAP.",
                    a8_entry, a8_exit, ["adr_min", "move_min", "stop_adr"], adr_min=4.0, move_min=1.0, stop_adr=0.5),
    "a09_rsleader": _s("A9 RS leader vs SPY", "At 10:30 buy the stock furthest ahead of SPY (≥ 1%) on an up day.",
                       a9_entry, none_exit, ["ex_min", "entry_bar"], ex_min=1.0, entry_bar=11),
    "a10_residual": _s("A10 Residual reversal", "Buy a stock lagging β×SPY by half its ADR; exit when the gap closes.",
                       a10_entry, a10_exit, ["entry_adr", "exit_adr", "stop_adr"], entry_adr=0.5, exit_adr=0.15, stop_adr=0.5),
    "a11_slot": _s("A11 Half-hour periodicity", "Trade each stock's best half-hour slot of the prior 20 days.",
                   a11_entry, a11_exit, ["lookback", "thr_bp"], lookback=20, thr_bp=10.0),
    "a12_loser": _s("A12 Weekly-loser reversal", "Buy the worst 5-day loser (≤ −3%) at 09:35, hold to close.",
                    a12_entry, none_exit, ["loss_min", "lookback", "stop_atr"], loss_min=3.0, lookback=5, stop_atr=1.0),
    "s13_connors": _s("S13 SWING Connors RSI(2)", "Daily RSI(2) < 10 above SMA200, buy 15:50, sell above SMA5.",
                      s13_entry, s13_exit, ["rsi_entry", "sma_exit"], rsi_entry=10.0, sma_exit=5, swing=True),
    "s14_trendnight": _s("S14 SWING Trend-day overnight", "Strong day closing near the high: hold to the next day's close.",
                         s14_entry, s14_exit, ["move_adr", "top_frac"], move_adr=0.5, top_frac=0.15, swing=True),
    "s15_adrbreak": _s("S15 SWING High-ADR 20-day breakout", "ADR ≥ 4% and a new 20-day high at 15:50; exit below SMA10.",
                       s15_entry, s15_exit, ["lookback", "adr_min"], lookback=20, adr_min=4.0, swing=True),
}
SECOND_SET = {k: ("HOT" if k in ("a08_focus", "s15_adrbreak") else "NONTECH") for k in ALGOS}


# ---------- live-ready A4: needs only the ticker's own 5-min bars (no prepare()/CTX) ----------
def vwapband_live_entry(p, bars, ind, i, ctx):
    et = ind["et"][i]
    endm = et.hour * 60 + et.minute + 5
    if not (10 * 60 + 30 <= endm <= 14 * 60 + 30) or ctx["trades_today"]:
        return None
    d, s = et.date(), i
    while s > 0 and ind["et"][s - 1].date() == d:
        s -= 1
    pv = v = pv2 = 0.0
    for q in bars[s:i + 1]:
        tp = (q["h"] + q["l"] + q["c"]) / 3
        pv, v, pv2 = pv + tp * q["v"], v + q["v"], pv2 + tp * tp * q["v"]
    if not v:
        return None
    vw = pv / v
    sg = math.sqrt(max(0.0, pv2 / v - vw * vw))
    b = bars[i]
    if sg <= 0 or not (b["c"] < vw - p["band_sigma"] * sg and b["c"] > b["o"]):
        return None
    return {"side": 1, "stop": b["c"] - p["stop_sigma"] * sg, "target": None,
            "why": f"{(vw - b['c']) / sg:.1f}σ below VWAP, green bar"}


def vwapband_live_exit(p, bars, ind, i, pos):
    return "Back at VWAP" if bars[i]["c"] >= ind["vwap"][i] else None


LIVE = {
    "vwapband": {
        "name": "VWAP Band", "blurb": "Buys a green 5-min bar 2σ below session VWAP (10:30–14:30); sells back at VWAP.",
        "entry": vwapband_live_entry, "exit": vwapband_live_exit,
        "defaults": {"risk_pct": 100.0, "max_position_pct": 50.0, "max_positions": 2,
                     "band_sigma": 2.0, "stop_sigma": 1.0},
    },
}
