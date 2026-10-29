"""Simulated broker and bot runner. One SQLite file holds every bot's cash, positions, trades and equity."""
import json
import math
import sqlite3
import threading
import time as _time
from datetime import datetime, timedelta

import market
from market import ET, BAR_SECONDS
from strategies import STRATEGIES, indicators, _bar_end

# A simulated small cash account at a US broker with IBKR-style fees. Individual stocks only (no funds or ETFs);
# change any of these from the dashboard's Settings.
ACCOUNT_DEFAULTS = {
    # "cash": no shorting, no Pattern Day Trader limit, buys only with settled cash (T+1).
    # "margin": no day-trade limit (the Pattern Day Trader rule and its $25k minimum were removed: SEC approval
    # 2026-04-14, IBKR from 2026-06-04); short sales still need Reg T's $2,000 minimum equity. No leverage is used.
    "type": "cash",
    # IBKR Pro Tiered for US stocks (approximate): per share, minimum per order, cap as % of trade value.
    "commission_per_share": 0.0035, "commission_min": 0.35, "commission_max_pct": 1.0,
    "slippage_pct": 0.01,
    "tax_pct": 0.0,  # your income tax on gains, a rough estimate only; set it in Settings
}
MARGIN_MIN_EQUITY = 2000.0  # Reg T minimum for short sales and borrowing
SEC_FEE = 0.0000278   # US SEC fee on the value of every sale
FINRA_TAF = 0.000166  # FINRA trading activity fee per share sold, max $8.30
STALE_ENTRY_SECONDS = 600  # live mode: never open a trade on a bar that closed more than 10 min ago
START_CASH = 1000.0
DEFAULT_WATCHLIST = ["AMD", "NVDA", "TSLA", "META", "AAPL", "MSFT", "GOOGL", "AMZN", "NFLX", "JPM"]
BENCHMARK = "SPY"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bots(id TEXT PRIMARY KEY, strategy TEXT, name TEXT, status TEXT, params TEXT,
  start_cash REAL, cash REAL, started_at TEXT);
CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY, bot_id TEXT, ticker TEXT, side INTEGER, qty REAL,
  entry_price REAL, entry_ts INTEGER, stop REAL, target REAL, fees REAL, why TEXT);
CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, bot_id TEXT, ticker TEXT, side INTEGER, qty REAL,
  entry_price REAL, exit_price REAL, entry_ts INTEGER, exit_ts INTEGER, pnl REAL, pnl_pct REAL, fees REAL,
  why TEXT, exit_reason TEXT);
CREATE TABLE IF NOT EXISTS equity(bot_id TEXT, ts INTEGER, equity REAL);
CREATE INDEX IF NOT EXISTS equity_bot_ts ON equity(bot_id, ts);
CREATE TABLE IF NOT EXISTS cursor(ticker TEXT PRIMARY KEY, last_ts INTEGER);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts INTEGER, level TEXT, bot_id TEXT, msg TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS benchmark(ts INTEGER PRIMARY KEY, price REAL);
CREATE TABLE IF NOT EXISTS day_bp(bot_id TEXT, day TEXT, start_cash REAL, used REAL, PRIMARY KEY(bot_id, day));
"""


class Engine:
    def __init__(self, path, live=True):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self.live = live
        self.last_price = {}
        self.peers = {}
        self._init_defaults()

    # ---------- setup ----------
    def _init_defaults(self):
        with self.lock, self.db:
            now = datetime.now(ET).isoformat()
            have = {r["id"] for r in self.db.execute("SELECT id FROM bots")}
            for key, s in STRATEGIES.items():
                if key not in have and not (self.live and s.get("retired")):
                    self.db.execute("INSERT INTO bots VALUES(?,?,?,?,?,?,?,?)",
                                    (key, key, s["name"], "running", json.dumps(s["defaults"]), START_CASH, START_CASH, now))
            if self.setting("watchlist") is None:
                self.set_setting("watchlist", DEFAULT_WATCHLIST)
            if self.setting("account") is None:
                self.set_setting("account", ACCOUNT_DEFAULTS)
            if self.setting("test_start") is None:
                self.set_setting("test_start", datetime.now(ET).date().isoformat())

    def setting(self, key):
        r = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else None

    def set_setting(self, key, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value)))

    def account(self):
        a = {**ACCOUNT_DEFAULTS, **(self.setting("account") or {})}
        if a["type"] == "margin_pdt":  # saved before the PDT rule was removed
            a["type"] = "margin"
        return a

    def set_account(self, values):
        clean = {}
        for k, d in ACCOUNT_DEFAULTS.items():
            if k not in values:
                continue
            v = values[k]
            if k == "type":
                if v not in ("cash", "margin"):
                    raise ValueError("account type must be cash or margin")
                clean[k] = v
            else:
                clean[k] = float(v)
                if clean[k] < 0:
                    raise ValueError(f"{k} can't be negative")
        self.set_setting("account", {**self.account(), **clean})
        with self.lock, self.db:
            a = self.account()
            self.event(f"Account rules: {'cash account' if a['type'] == 'cash' else 'margin account'}, "
                       f"commission ${a['commission_per_share']}/share (min ${a['commission_min']}), "
                       f"slippage {a['slippage_pct']}%", "control")

    def fee(self, qty, price, sell):
        """Broker commission plus US regulatory fees on sales."""
        a = self.account()
        value = qty * price
        c = max(a["commission_min"], a["commission_per_share"] * qty)
        c = min(c, value * a["commission_max_pct"] / 100)
        if sell:
            c += value * SEC_FEE + min(8.30, qty * FINRA_TAF)
        return c

    def event(self, msg, level="info", bot_id=None, ts=None):
        self.db.execute("INSERT INTO events(ts,level,bot_id,msg) VALUES(?,?,?,?)",
                        (ts or int(_time.time()), level, bot_id, msg))

    def universe(self, strategy, d):
        """The stocks a bot may open trades in: the watchlist, or today's list for its universe (setting
        "<universe>_today": in_play from the premarket report; orb, loser and control from scans.py), or the fixed
        list in the strategy's "tickers"."""
        if strategy.get("tickers"):
            return strategy["tickers"]
        u = strategy.get("universe")
        if u:
            ip = self.setting(f"{u}_today") or {}
            return ip.get("tickers", []) if ip.get("date") == d.isoformat() else []
        return self.setting("watchlist")

    def seed_cursor(self, ticker, ts):
        """For a stock picked after the open: start its bars at ts instead of 'now', so the bar that triggered
        the pick is still processed. No effect on a stock the engine has seen before."""
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO cursor VALUES(?,?)", (ticker, ts))

    def bots(self):
        """Live: retired strategies' bots are left out (their trades stay in the database)."""
        out = []
        for r in self.db.execute("SELECT * FROM bots ORDER BY rowid"):
            b = dict(r)
            if self.live and STRATEGIES[b["strategy"]].get("retired"):
                continue
            b["params"] = {**STRATEGIES[b["strategy"]]["defaults"], **json.loads(b["params"])}
            out.append(b)
        return out

    def positions(self, bot_id=None):
        q, a = "SELECT * FROM positions", ()
        if bot_id:
            q, a = q + " WHERE bot_id=?", (bot_id,)
        return [dict(r) for r in self.db.execute(q, a)]

    def equity_of(self, bot):
        e = bot["cash"]
        for p in self.positions(bot["id"]):
            e += p["side"] * p["qty"] * self.last_price.get(p["ticker"], p["entry_price"])
        return e

    # ---------- the main step ----------
    def step(self, bars_by_ticker, now_ts):
        """Process every completed bar not yet seen, in time order across tickers."""
        with self.lock, self.db:
            work, prepared = [], {}
            for t, bars in bars_by_ticker.items():
                if not bars:
                    continue
                ind = indicators(bars)
                ind["ticker"] = t  # for strategies that look up the stock's daily history
                prepared[t] = (bars, ind)
                cur = self.db.execute("SELECT last_ts FROM cursor WHERE ticker=?", (t,)).fetchone()
                last = cur["last_ts"] if cur else None
                if last is None and self.live:
                    # First sight of this ticker: start from now, never trade history.
                    done = [b for b in bars if b["ts"] + BAR_SECONDS <= now_ts]
                    last = done[-1]["ts"] if done else 0
                    self.db.execute("INSERT OR REPLACE INTO cursor VALUES(?,?)", (t, last))
                for i, b in enumerate(bars):
                    if b["ts"] + BAR_SECONDS <= now_ts and (last is None or b["ts"] > last):
                        work.append((b["ts"], t, i))
            work.sort()
            self.peers = prepared  # every stock's bars this step, for strategies that rank stocks against each other
            for ts, t, i in work:
                bars, ind = prepared[t]
                self.last_price[t] = bars[i]["c"]
                self._process_bar(t, bars, ind, i, now_ts)
                self.db.execute("INSERT OR REPLACE INTO cursor VALUES(?,?)", (t, ts))
            # mark to the freshest price, forming bar included
            for t, (bars, _) in prepared.items():
                self.last_price[t] = bars[-1]["c"]
            if self.live:
                self._flatten_overdue(now_ts)
            return len(work)

    def _flatten_overdue(self, now_ts):
        """Live only: if a position's closing bars still haven't arrived 3 minutes after the close (Yahoo left
        them empty, or the ticker won't fetch), close it at the last known price so nothing is held overnight."""
        for bot in self.bots():
            if STRATEGIES[bot["strategy"]].get("swing"):
                continue  # holds overnight by design
            for pos in self.positions(bot["id"]):
                d = datetime.fromtimestamp(pos["entry_ts"], ET).date()
                close_ts = int(datetime.combine(d, market.close_time(d), ET).timestamp())
                if now_ts >= close_ts + 180:
                    self._close(bot, pos, self.last_price.get(pos["ticker"], pos["entry_price"]), close_ts,
                                "Flatten (closing bars missing)")

    def snapshot(self, ts):
        with self.lock, self.db:
            for b in self.bots():
                self.db.execute("INSERT INTO equity VALUES(?,?,?)", (b["id"], ts, round(self.equity_of(b), 4)))
            if BENCHMARK in self.last_price:
                self.db.execute("INSERT OR REPLACE INTO benchmark VALUES(?,?)", (ts, self.last_price[BENCHMARK]))

    def _process_bar(self, t, bars, ind, i, now_ts):
        bar = bars[i]
        d = ind["et"][i].date()
        flat_min = market.close_time(d).hour * 60 + market.close_time(d).minute - 5
        end = _bar_end(ind, i)
        at_flatten = end.hour * 60 + end.minute >= flat_min
        bar_close_ts = bar["ts"] + BAR_SECONDS
        stale = self.live and now_ts - bar_close_ts > STALE_ENTRY_SECONDS
        if t == BENCHMARK:
            return
        day_start = int(datetime.combine(d, market.OPEN, ET).timestamp())
        for bot in self.bots():
            s, p = STRATEGIES[bot["strategy"]], bot["params"]
            swing = bool(s.get("swing"))  # swing bots hold overnight: no end-of-day flatten
            pos = self.db.execute("SELECT * FROM positions WHERE bot_id=? AND ticker=?", (bot["id"], t)).fetchone()
            if pos:
                pos = dict(pos)
                if not swing and datetime.fromtimestamp(pos["entry_ts"], ET).date() < d:
                    # yesterday's closing bars never arrived, so the flatten didn't run: close at this bar's open
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
            if bot["status"] != "running" or stale or at_flatten or t not in self.universe(s, d):
                continue
            trades_today = self.db.execute(
                "SELECT COUNT(*) n FROM trades WHERE bot_id=? AND ticker=? AND entry_ts>=?",
                (bot["id"], t, day_start)).fetchone()["n"]
            sig = s["entry"](p, bars, ind, i, {"trades_today": trades_today, "peers": self.peers})
            if sig and self._allowed(bot, sig, d):
                self._open(bot, t, sig, bar["c"], bar_close_ts, d)

    def _allowed(self, bot, sig, d):
        """Account rules for a small cash account (see ACCOUNT_DEFAULTS)."""
        a = self.account()
        if sig["side"] == 1:
            return True
        # Short sales: never in a cash account; in a margin account only with Reg T's $2,000 minimum equity.
        return a["type"] == "margin" and self.equity_of(bot) >= MARGIN_MIN_EQUITY

    def _buying_power(self, bot, d):
        """Cash account: buys only with cash that was settled at the start of the day (T+1).
        Sales today settle tomorrow, so their proceeds can't fund new buys today."""
        row = self.db.execute("SELECT start_cash, used FROM day_bp WHERE bot_id=? AND day=?", (bot["id"], d.isoformat())).fetchone()
        if not row:
            start = bot["cash"] + sum(p["side"] * p["qty"] * p["entry_price"] for p in self.positions(bot["id"]) if p["side"] == -1)
            self.db.execute("INSERT INTO day_bp VALUES(?,?,?,0)", (bot["id"], d.isoformat(), start))
            return start
        return row["start_cash"] - row["used"]

    @staticmethod
    def _exit_check(pos, bar):
        """Stop first if a bar spans both levels (conservative); gaps fill at the open."""
        stop, tgt, side = pos["stop"], pos["target"], pos["side"]
        if side == 1:
            if bar["o"] <= stop: return bar["o"], "Stop (gap)"
            if bar["l"] <= stop: return stop, "Stop"
            if tgt and bar["o"] >= tgt: return bar["o"], "Target (gap)"
            if tgt and bar["h"] >= tgt: return tgt, "Target"
        else:
            if bar["o"] >= stop: return bar["o"], "Stop (gap)"
            if bar["h"] >= stop: return stop, "Stop"
            if tgt and bar["o"] <= tgt: return bar["o"], "Target (gap)"
            if tgt and bar["l"] <= tgt: return tgt, "Target"
        return None, None

    def _open(self, bot, t, sig, price, ts, d):
        p, a = bot["params"], self.account()
        side = sig["side"]
        fill = price * (1 + a["slippage_pct"] / 100 * side)
        open_pos = self.positions(bot["id"])
        if len(open_pos) >= p["max_positions"]:
            return
        eq = self.equity_of(bot)
        dist = abs(fill - sig["stop"])
        if eq <= 0 or dist <= 0 or (side == 1 and sig["stop"] >= fill) or (side == -1 and sig["stop"] <= fill):
            return
        qty = eq * p["risk_pct"] / 100 / dist
        qty = min(qty, eq * p["max_position_pct"] / 100 / fill)
        gross = sum(x["qty"] * self.last_price.get(x["ticker"], x["entry_price"]) for x in open_pos)
        qty = min(qty, max(0.0, eq - gross) / fill)
        if a["type"] == "cash":
            qty = min(qty, max(0.0, self._buying_power(bot, d) - a["commission_min"]) / fill)
        qty = math.floor(qty * 10000) / 10000  # round down: never spend more than the cash available
        if qty * fill < 10:
            return
        fee = self.fee(qty, fill, sell=side == -1)
        with self.db:
            if a["type"] == "cash":
                self.db.execute("UPDATE day_bp SET used = used + ? WHERE bot_id=? AND day=?", (qty * fill + fee, bot["id"], d.isoformat()))
            self.db.execute("UPDATE bots SET cash = cash - ? WHERE id=?", (side * qty * fill + fee, bot["id"]))
            self.db.execute("INSERT INTO positions(bot_id,ticker,side,qty,entry_price,entry_ts,stop,target,fees,why) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (bot["id"], t, side, qty, fill, ts, sig["stop"], sig.get("target"), fee, sig["why"]))
            self.event(f"{'Bought' if side == 1 else 'Shorted'} {qty:g} {t} at {fill:.2f}: {sig['why']}", "trade", bot["id"], ts)
        bot["cash"] -= side * qty * fill + fee

    def _close(self, bot, pos, price, ts, reason):
        side, qty = pos["side"], pos["qty"]
        fill = price * (1 - self.account()["slippage_pct"] / 100 * side)
        fee = self.fee(qty, fill, sell=side == 1)
        fees = pos["fees"] + fee
        pnl = side * (fill - pos["entry_price"]) * qty - fees
        with self.db:
            self.db.execute("UPDATE bots SET cash = cash + ? WHERE id=?", (side * qty * fill - fee, bot["id"]))
            self.db.execute("DELETE FROM positions WHERE id=?", (pos["id"],))
            self.db.execute("INSERT INTO trades(bot_id,ticker,side,qty,entry_price,exit_price,entry_ts,exit_ts,pnl,pnl_pct,fees,why,exit_reason) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (bot["id"], pos["ticker"], side, qty, pos["entry_price"], fill, pos["entry_ts"], ts, pnl,
                             pnl / (pos["entry_price"] * qty) * 100, fees, pos["why"], reason))
            self.event(f"Closed {pos['ticker']} at {fill:.2f} ({reason}): {'+' if pnl >= 0 else '−'}${abs(pnl):.2f}",
                       "trade", bot["id"], ts)
        bot["cash"] += side * qty * fill - fee

    # ---------- controls ----------
    def set_status(self, bot_id, status):
        with self.lock, self.db:
            self.db.execute("UPDATE bots SET status=? WHERE id=?", (status, bot_id))
            self.event(f"Bot {'resumed' if status == 'running' else 'paused'}", "control", bot_id)

    def set_params(self, bot_id, params):
        with self.lock, self.db:
            row = self.db.execute("SELECT strategy FROM bots WHERE id=?", (bot_id,)).fetchone()
            defaults = STRATEGIES[row["strategy"]]["defaults"]
            clean = {}
            for k, v in params.items():
                if k not in defaults:
                    continue
                d = defaults[k]
                clean[k] = bool(v) if isinstance(d, bool) else (str(v) if isinstance(d, str) else type(d)(float(v)))
            self.db.execute("UPDATE bots SET params=? WHERE id=?", (json.dumps(clean), bot_id))
            changed = ", ".join(f"{k}={v}" for k, v in clean.items() if v != defaults[k])
            self.event("Settings changed: " + (changed or "back to defaults"), "control", bot_id)

    def reset_bot(self, bot_id):
        with self.lock, self.db:
            for tbl in ("positions", "trades", "equity", "day_bp"):
                self.db.execute(f"DELETE FROM {tbl} WHERE bot_id=?", (bot_id,))
            self.db.execute("UPDATE bots SET cash=start_cash, started_at=? WHERE id=?", (datetime.now(ET).isoformat(), bot_id))
            self.event("Bot reset to $1,000", "control", bot_id)

    def restart_test(self):
        with self.lock, self.db:
            for b in self.bots():
                for tbl in ("positions", "trades", "equity", "day_bp"):
                    self.db.execute(f"DELETE FROM {tbl} WHERE bot_id=?", (b["id"],))
                self.db.execute("UPDATE bots SET cash=start_cash, started_at=? WHERE id=?", (datetime.now(ET).isoformat(), b["id"]))
            self.db.execute("DELETE FROM benchmark")
            self.set_setting("test_start", datetime.now(ET).date().isoformat())
            self.event("30-day test restarted", "control")

    def set_watchlist(self, tickers):
        tickers = [t.strip().upper() for t in tickers if t.strip()][:12]
        self.set_setting("watchlist", tickers)
        with self.lock, self.db:
            self.event("Watchlist: " + ", ".join(tickers), "control")

    # ---------- reporting ----------
    def stats(self, bot):
        trades = [dict(r) for r in self.db.execute("SELECT * FROM trades WHERE bot_id=? ORDER BY exit_ts", (bot["id"],))]
        wins = [t["pnl"] for t in trades if t["pnl"] > 0]
        losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
        eq = self.equity_of(bot)
        curve = [r["equity"] for r in self.db.execute("SELECT equity FROM equity WHERE bot_id=? ORDER BY ts", (bot["id"],))]
        peak, mdd = bot["start_cash"], 0.0
        for e in curve + [eq]:
            peak = max(peak, e)
            mdd = max(mdd, (peak - e) / peak * 100)
        today = int(datetime.combine(datetime.now(ET).date(), market.OPEN, ET).timestamp())
        before = self.db.execute("SELECT equity FROM equity WHERE bot_id=? AND ts<? ORDER BY ts DESC LIMIT 1",
                                 (bot["id"], today)).fetchone()
        base = before["equity"] if before else bot["start_cash"]
        return {
            "equity": eq, "return_pct": (eq / bot["start_cash"] - 1) * 100, "today_pnl": eq - base,
            "trades": len(trades), "win_rate": len(wins) / len(trades) * 100 if trades else None,
            "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
            "avg_win": sum(wins) / len(wins) if wins else None, "avg_loss": sum(losses) / len(losses) if losses else None,
            "max_drawdown_pct": mdd, "fees": sum(t["fees"] for t in trades),
            "net_pnl": sum(t["pnl"] for t in trades),
            "est_tax": max(0.0, sum(t["pnl"] for t in trades)) * self.account()["tax_pct"] / 100,
            "wash_sale_losses": self._wash_sales(trades), "losses": len(losses),
        }

    @staticmethod
    def _wash_sales(trades):
        """Losing trades where the same stock was bought again within 30 days before or after the
        losing sale. For US tax those losses are deferred into the new position, not deductible now."""
        n = 0
        for t in trades:
            if t["pnl"] >= 0:
                continue
            if any(o is not t and o["ticker"] == t["ticker"] and abs(o["entry_ts"] - t["exit_ts"]) <= 30 * 86400 for o in trades):
                n += 1
        return n
