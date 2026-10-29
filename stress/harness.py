"""Run one bot through the unchanged live engine on cached bars, in a throwaway database.

The live modules (engine.py, strategies.py, market.py) are imported, never edited. Everything a test needs
to vary is done in a subclass here:
  - commission: a flat fraction per side (the brief's model, e.g. 0.0005), or None for the engine's own
    account fee model (IBKR per-share + SEC/FINRA)
  - research=True: account rules off (shorts allowed, no PDT, no T+1 settlement), the research baseline
    setup. research=False: the engine's account rules apply as configured.
  - next_open=True: a signal on bar i fills at bar i+1's open instead of bar i's close
"""
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import engine  # noqa: E402
import market  # noqa: E402
from strategies import STRATEGIES  # noqa: E402
import data  # noqa: E402

ET = market.ET
START = engine.START_CASH


class StressEngine(engine.Engine):
    def __init__(self, path, commission=0.0005, research=True, next_open=False, allow_days=None):
        self.commission, self.research, self.next_open = commission, research, next_open
        self.allow_days = allow_days  # optional set of (ticker, date) on which entries are allowed
        self.pending, self._filling = defaultdict(dict), False
        super().__init__(path, live=False)

    def fee(self, qty, price, sell):
        if self.commission is None:
            return super().fee(qty, price, sell)
        return qty * price * self.commission

    def _allowed(self, bot, sig, d):
        return True if self.research else super()._allowed(bot, sig, d)

    def _open(self, bot, t, sig, price, ts, d):
        if self.allow_days is not None and (t, d) not in self.allow_days:
            return
        if self.next_open and not self._filling:
            self.pending[bot["id"]].setdefault(t, (sig, d))
            return
        return super()._open(bot, t, sig, price, ts, d)

    def _process_bar(self, t, bars, ind, i, now_ts):
        if self.next_open:
            d = ind["et"][i].date()
            for bot in self.bots():
                pend = self.pending[bot["id"]].pop(t, None)
                if not pend or pend[1] != d:
                    continue
                if self.db.execute("SELECT 1 FROM positions WHERE bot_id=? AND ticker=?", (bot["id"], t)).fetchone():
                    continue
                self._filling = True
                try:
                    super()._open(bot, t, pend[0], bars[i]["o"], bars[i]["ts"], d)
                finally:
                    self._filling = False
        super()._process_bar(t, bars, ind, i, now_ts)


def run(bot_id, tickers, params=None, commission=0.0005, slippage_pct=0.01, research=True, next_open=False,
        account=None, allow_days=None):
    """Returns (trades, open_positions_at_end). Trades are dicts as stored by the engine."""
    path = os.path.join(tempfile.mkdtemp(prefix="stress_"), "run.db")
    eng = StressEngine(path, commission=commission, research=research, next_open=next_open,
                       allow_days=allow_days)
    with eng.db:
        eng.db.execute("DELETE FROM bots WHERE id != ?", (bot_id,))
    if params:
        eng.set_params(bot_id, params)
    acct = dict(account or {})
    if research:
        acct.setdefault("type", "margin")  # account rules are bypassed by _allowed; this skips T+1 sizing
    acct.setdefault("slippage_pct", slippage_pct)
    eng.set_account(acct)
    eng.set_watchlist(tickers)
    bars = {t: data.full_days(data.load(t)) for t in tickers}
    end = max(b[-1]["ts"] for b in bars.values()) + 3600
    eng.step(bars, end)
    trades = [dict(r) for r in eng.db.execute("SELECT * FROM trades ORDER BY exit_ts, id")]
    open_pos = eng.positions()
    eng.db.close()
    os.remove(path)
    return trades, open_pos


# ---------- metrics ----------
def day(ts):
    return datetime.fromtimestamp(ts, ET).date()


def metrics(trades, start=START):
    n = len(trades)
    pnl = [t["pnl"] for t in trades]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p <= 0]
    eq, peak, mdd = start, start, 0.0
    for p in pnl:
        eq += p
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak * 100)
    fees = sum(t["fees"] for t in trades)
    reasons = defaultdict(int)
    for t in trades:
        reasons[t["exit_reason"].split(" (")[0] if not t["exit_reason"].startswith("RSI") else "RSI exit"] += 1
    return {
        "trades": n, "ret": sum(pnl) / start * 100, "win": len(wins) / n * 100 if n else None,
        "pf": sum(wins) / -sum(losses) if losses and sum(losses) < 0 else None, "mdd": mdd, "fees": fees,
        "gross_ret": (sum(pnl) + fees) / start * 100,
        "avg_trade_bp": (sum(t["pnl_pct"] for t in trades) / n * 100) if n else None,  # basis points of notional
        "overnight": sum(1 for t in trades if day(t["entry_ts"]) != day(t["exit_ts"])),
        "reasons": dict(reasons),
        "long_pnl": sum(t["pnl"] for t in trades if t["side"] == 1),
        "short_pnl": sum(t["pnl"] for t in trades if t["side"] == -1),
    }


def gross_bp(trades):
    """Average gross P&L per trade (before commission, after slippage) in basis points of entry notional."""
    if not trades:
        return None
    return sum((t["pnl"] + t["fees"]) / (t["qty"] * t["entry_price"]) for t in trades) / len(trades) * 1e4


def cost_bp(trades):
    """Average commission per trade (both sides) in basis points of entry notional."""
    if not trades:
        return None
    return sum(t["fees"] / (t["qty"] * t["entry_price"]) for t in trades) / len(trades) * 1e4


def fmt_pf(x):
    return "–" if x is None else f"{x:.2f}"


def row(label, m):
    win = "–" if m["win"] is None else f"{m['win']:.0f}"
    return (f"| {label} | {m['ret']:+.2f}% | {m['trades']} | {win} | {fmt_pf(m['pf'])} | {m['mdd']:.1f}% | "
            f"${m['fees']:.0f} | {m['gross_ret']:+.2f}% |")


HEADER = "| Run | Return | Trades | Win % | PF | Max DD | Fees | Before fees |\n|---|---|---|---|---|---|---|---|"
BOTS = list(STRATEGIES)
