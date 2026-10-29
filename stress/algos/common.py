"""Runner for the 15 algorithms: the unchanged live engine (via stress/harness.StressEngine) in a throwaway database.

The algorithms are added to engine.STRATEGIES in this process only. A running server is a separate process and is
not touched; nothing here reads or writes state/.
"""
import math
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STRESS = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(STRESS))
sys.path.insert(0, STRESS)
sys.path.insert(0, HERE)

import engine  # noqa: E402
import market  # noqa: E402
import harness as h  # noqa: E402
import data  # noqa: E402
import algos  # noqa: E402

engine.STRATEGIES.update(algos.ALGOS)  # this process only

ET = market.ET
TECH, NONTECH = data.TECH, data.NONTECH
HOT = ["AMD", "NVDA", "TSLA", "MU", "AVGO", "SMCI", "MRVL", "ARM", "PLTR", "SNDK"]
SETS = {"TECH": TECH, "NONTECH": NONTECH, "HOT": HOT}
DAYS = sorted({data.day_of(b) for b in data.full_days(data.load("AMD"))})  # the 59 days
HOLDOUT = DAYS[-10]
DEV = [d for d in DAYS if d < HOLDOUT]
HOLD = [d for d in DAYS if d >= HOLDOUT]
MID = DEV[len(DEV) // 2 - 1]  # first half = DEV[:24], second half = DEV[24:]
FIRST_HALF_END = DEV[23]

_BARS, _DAILY = {}, {}


def bars5(t):
    if t not in _BARS:
        keep = set(DAYS)
        _BARS[t] = [b for b in data.full_days(data.load(t)) if data.day_of(b) in keep]
    return _BARS[t]


def daily_all(last_day=DAYS[-1]):
    key = last_day
    if key not in _DAILY:
        out = {}
        for t in sorted(set(TECH + NONTECH + HOT + ["SPY"])):
            out[t] = algos.daily_from_hourly(data.load_hourly(t), last_day)
        _DAILY[key] = out
    return _DAILY[key]


class SwingCore(engine.Engine):
    """Engine._process_bar (as of the 2026-10-12 universe() change), unchanged except that strategies with defaults["swing"] skip the end-of-day flatten
    and the missed-flatten rule (they hold overnight by design)."""

    def _process_bar(self, t, bars, ind, i, now_ts):
        bar = bars[i]
        d = ind["et"][i].date()
        flat_min = market.close_time(d).hour * 60 + market.close_time(d).minute - 5
        end = engine._bar_end(ind, i)
        at_flatten = end.hour * 60 + end.minute >= flat_min
        bar_close_ts = bar["ts"] + engine.BAR_SECONDS
        if t == engine.BENCHMARK:
            return
        day_start = int(datetime.combine(d, market.OPEN, ET).timestamp())
        for bot in self.bots():
            s, p = engine.STRATEGIES[bot["strategy"]], bot["params"]
            swing = bool(s["defaults"].get("swing"))
            pos = self.db.execute("SELECT * FROM positions WHERE bot_id=? AND ticker=?", (bot["id"], t)).fetchone()
            if pos:
                pos = dict(pos)
                if not swing and datetime.fromtimestamp(pos["entry_ts"], ET).date() < d:
                    self._close(bot, pos, bar["o"], bar["ts"], "Missed flatten")
                    continue
                fill, reason = self._exit_check(pos, bar)
                if fill is None:
                    reason = s["exit"](p, bars, ind, i, pos)
                    if reason:
                        fill = bar["c"]
                if fill is None and at_flatten and not swing:
                    fill, reason = bar["c"], "End-of-day flatten"
                if fill is not None:
                    self._close(bot, pos, fill, bar_close_ts, reason)
                continue
            if bot["status"] != "running" or at_flatten or t not in self.universe(s, d):
                continue
            trades_today = self.db.execute(
                "SELECT COUNT(*) n FROM trades WHERE bot_id=? AND ticker=? AND entry_ts>=?",
                (bot["id"], t, day_start)).fetchone()["n"]
            sig = s["entry"](p, bars, ind, i, {"trades_today": trades_today})
            if sig and self._allowed(bot, sig, d):
                self._open(bot, t, sig, bar["c"], bar_close_ts, d)


class AlgoEngine(h.StressEngine, SwingCore):
    pass


MODELS = {
    "live": dict(commission=None),     # IBKR per-share, $0.35 min, SEC/FINRA
    "brief": dict(commission=0.0005),  # 0.05% per side
}


def run(key, tickers, window="dev", model="live", params=None, next_open=False, slippage=0.01):
    """Trades of one algorithm. window: 'dev' (bars cut at the holdout), 'holdout' (all bars, entries only on
    holdout days, so indicators are warm), or 'all'. Positions still open at the end are closed at the last close."""
    if window == "dev":
        bars = {t: [b for b in bars5(t) if data.day_of(b) < HOLDOUT] for t in tickers}
        spy = [b for b in bars5("SPY") if data.day_of(b) < HOLDOUT]
        allow = None
    else:
        bars = {t: list(bars5(t)) for t in tickers}
        spy = list(bars5("SPY"))
        allow = {(t, d) for t in tickers for d in HOLD} if window == "holdout" else None
    path = os.path.join(tempfile.mkdtemp(prefix="algos_"), "run.db")
    eng = AlgoEngine(path, commission=MODELS[model]["commission"], research=False, next_open=next_open, allow_days=allow)
    with eng.db:
        eng.db.execute("DELETE FROM bots WHERE id != ?", (key,))
    if params:
        eng.set_params(key, params)
    eng.set_account({"type": "cash", "slippage_pct": slippage})
    eng.set_watchlist(tickers)
    algos.prepare(bars, spy, daily_all())
    end = max(b[-1]["ts"] for b in bars.values()) + 3600
    eng.step(bars, end)
    bot = eng.bots()[0]
    for pos in eng.positions():
        last = bars[pos["ticker"]][-1]
        eng._close(bot, pos, last["c"], last["ts"] + 300, "End of data")
    trades = [dict(r) for r in eng.db.execute("SELECT * FROM trades ORDER BY exit_ts, id")]
    eng.db.close()
    os.remove(path)
    return trades


# ---------- metrics ----------
def day(ts):
    return datetime.fromtimestamp(ts, ET).date()


def gross_list(trades):
    return [(t["pnl"] + t["fees"]) / (t["qty"] * t["entry_price"]) * 1e4 for t in trades]


def score(trades):
    g = gross_list(trades)
    n = len(g)
    if n < 2:
        return None
    m = sum(g) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in g) / (n - 1))
    return m - 1.645 * sd / math.sqrt(n) - h.cost_bp(trades)


def summary(trades):
    m = h.metrics(trades)
    g = h.gross_bp(trades)
    return {"ret": m["ret"], "trades": m["trades"], "win": m["win"], "pf": m["pf"], "mdd": m["mdd"], "fees": m["fees"],
            "gross_bp": g, "cost_bp": h.cost_bp(trades), "net_bp": (m["avg_trade_bp"] if trades else None),
            "breakeven_comm_side_pct": (g / 2 / 100 if g is not None else None), "score": score(trades),
            "overnight": m["overnight"], "reasons": m["reasons"], "pnl": sum(t["pnl"] for t in trades)}


def halves(trades):
    a = [t for t in trades if day(t["entry_ts"]) <= FIRST_HALF_END]
    z = [t for t in trades if day(t["entry_ts"]) > FIRST_HALF_END]
    return sum(t["pnl"] for t in a), len(a), sum(t["pnl"] for t in z), len(z)


def weekly(trades):
    w = defaultdict(float)
    for t in trades:
        w[day(t["entry_ts"]).isocalendar()[:2]] += t["pnl"]
    weeks = sorted({d.isocalendar()[:2] for d in DEV})
    return [(f"{a}-W{b:02d}", w.get((a, b), 0.0)) for a, b in weeks]


def per_ticker(trades, tickers):
    by = defaultdict(list)
    for t in trades:
        by[t["ticker"]].append(t)
    return {t: (sum(x["pnl"] for x in by[t]), len(by[t])) for t in tickers}


def monte_carlo(trades, days, n_runs=10_000, window=21, seed=7):
    import numpy as np
    rng = np.random.default_rng(seed)
    daily, eq = defaultdict(float), h.START
    for t in trades:
        daily[day(t["exit_ts"])] += t["pnl"] / eq
        eq += t["pnl"]
    dr = np.array([daily.get(d, 0.0) for d in days])
    curve = np.cumprod(1 + rng.choice(dr, size=(n_runs, window)), axis=1)
    final = (curve[:, -1] - 1) * 100
    peak = np.maximum.accumulate(np.concatenate([np.ones((n_runs, 1)), curve], axis=1), axis=1)[:, 1:]
    dd = ((peak - curve) / peak).max(axis=1) * 100
    return {"p_profit": float((final > 0).mean() * 100), "median": float(np.median(final)),
            "p5": float(np.percentile(final, 5)), "p95": float(np.percentile(final, 95)),
            "dd_median": float(np.median(dd)), "dd95": float(np.percentile(dd, 95))}


def variant(default, step):
    v = default * (1 + step)
    if isinstance(default, int):
        return max(1, int(round(v)))
    return round(v, 4)
