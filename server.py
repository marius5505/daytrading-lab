"""Day-Trading Lab: runs the paper-trading bots during US market hours and serves the dashboard on http://localhost:8787."""
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import market
import premarket
import scans
from engine import BENCHMARK, STALE_ENTRY_SECONDS, Engine
from market import ET
from strategies import STRATEGIES

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, "state")
os.makedirs(STATE_DIR, exist_ok=True)
PORT = int(os.environ.get("DAYTRADER_PORT", "8787"))
TEST_DAYS = 30

logging.basicConfig(filename=os.path.join(STATE_DIR, "daytrader.log"), level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("daytrader")
engine = Engine(os.path.join(STATE_DIR, "daytrader.db"))
health = {"last_tick": None, "last_error": None, "last_error_at": None, "fetch_ms": None}
report_state = {"building": False}
REPORT_TIME = (9, 0)  # New York time; the computer must be awake by then


# ---------- premarket report ----------
def build_report():
    if report_state["building"]:
        return
    report_state["building"] = True
    try:
        r = premarket.build(engine.setting("watchlist"))
        r["in_play"] = stocks_only(r["in_play"], "Premarket report")
        premarket.save(r)
        engine.set_setting("in_play_today", {"date": r["date"], "tickers": r["in_play"]})
        with engine.lock, engine.db:
            engine.event(f"Premarket report: {'in play ' + ', '.join(r['in_play']) if r['in_play'] else 'nothing in play'}"
                         f", {len(r['gappers'])} gappers" + (" (built after the open)" if r["late"] else ""), "report", "gapper")
        log.info("premarket report built: in play %s", r["in_play"])
    except Exception as e:
        log.exception("premarket report failed")
        health["last_error"], health["last_error_at"] = f"Premarket report failed: {e}", int(time.time())
    finally:
        report_state["building"] = False


def report_due(now):
    """At 09:00 ET, unless today's report exists and was built at 09:00 or later. A report built earlier (the
    dashboard's Refresh before 09:00) is rebuilt: early premarket volume is too thin to pick stocks in play."""
    d = now.date()
    if not (market.is_trading_day(d) and (now.hour, now.minute) >= REPORT_TIME
            and now.time() < market.close_time(d) and not report_state["building"]):
        return False
    r = premarket.load(d.isoformat())
    return r is None or r.get("generated_et", "00:00") < "%02d:%02d" % REPORT_TIME


# ---------- stock scans for the ORB In Play, Weekly Loser and Control bots (scans.py) ----------
scan_state = {"running": False, "tried": 0, "archived": None}


def todays(key, d):
    ip = engine.setting(f"{key}_today") or {}
    return ip.get("tickers", []) if ip.get("date") == d.isoformat() else []


def morning_scan():
    scan_state["running"], scan_state["tried"] = True, time.time()
    try:
        d = market.now_et().date()
        r = scans.morning(d)
        open_ts = int(datetime.combine(d, market.OPEN, ET).timestamp())
        for key in ("loser", "control"):
            r[key] = stocks_only(r[key], "Scan")
            for t in r[key]:
                engine.seed_cursor(t, open_ts - 1)  # its 09:30 bar is traded even if the scan finishes after the open
            engine.set_setting(f"{key}_today", {"date": d.isoformat(), "tickers": r[key]})
        engine.set_setting("orb_pool", {"date": d.isoformat(), "avg_first_volume": r["orb_pool"]})
        w = r["worst"]
        with engine.lock, engine.db:
            engine.event(f"Scan: worst 5-day loser {w['ticker']} {w['ret_pct']:+.1f}%" if w else "Scan: no data",
                         "report", "loser")
            engine.event(f"Scan: control pick {', '.join(r['control']) or 'none'}", "report", "control")
        log.info("morning scan: loser %s, control %s, orb pool %d", r["loser"], r["control"], len(r["orb_pool"]))
    except Exception as e:
        log.exception("morning scan failed")
        health["last_error"], health["last_error_at"] = f"Morning scan failed: {e}", int(time.time())
    finally:
        scan_state["running"] = False


def scan_due(now):
    d = now.date()
    return (market.is_trading_day(d) and (now.hour, now.minute) >= (9, 10) and now.time() < market.close_time(d)
            and (engine.setting("loser_today") or {}).get("date") != d.isoformat()
            and not scan_state["running"] and time.time() - scan_state["tried"] >= 300)


def orb_scan(now):
    """Right after the first 5-min bar: pick ORB In Play's stocks. Runs inside tick() so the picks trade this tick."""
    d, iso = now.date(), now.date().isoformat()
    open_ts = int(datetime.combine(d, market.OPEN, ET).timestamp())
    pool = engine.setting("orb_pool") or {}
    if (engine.setting("orb_today") or {}).get("date") == iso or pool.get("date") != iso or now.timestamp() < open_ts + 300:
        return
    picks = []
    if now.timestamp() <= open_ts + 300 + STALE_ENTRY_SECONDS:  # later than that the first bar is too old to trade
        picks = stocks_only(scans.orb(d, pool["avg_first_volume"]), "Scan")
        for t in picks:
            engine.seed_cursor(t, open_ts - 1)  # so the 09:30 bar of a stock seen for the first time is processed
    engine.set_setting("orb_today", {"date": iso, "tickers": picks})
    with engine.lock, engine.db:
        engine.event(f"Scan: unusual opening volume {', '.join(picks) or 'none (or too late today)'}", "report", "orb")


def archive_bars():
    scan_state["running"] = True
    try:
        n = scans.archive(sorted(set(premarket.UNIVERSE) | set(engine.setting("watchlist")) | {BENCHMARK}))
        scan_state["archived"] = market.now_et().date()
        log.info("archived 5-min bars of %d stocks to history/", n)
    except Exception:
        log.exception("bar archive failed")
    finally:
        scan_state["running"] = False


def archive_due(now):
    d = now.date()
    after = datetime.combine(d, market.close_time(d), ET) + timedelta(minutes=20)
    return market.is_trading_day(d) and now >= after and scan_state["archived"] != d and not scan_state["running"]


# ---------- trading loop ----------
def in_session_window(now):
    d = now.date()
    if not market.is_trading_day(d):
        return False
    start = datetime.combine(d, market.OPEN, ET)
    end = datetime.combine(d, market.close_time(d), ET) + timedelta(minutes=8)  # catch the last bar + flatten
    return start <= now < end


def tick():
    now = market.now_et()
    try:
        orb_scan(now)
    except Exception as e:
        log.exception("orb scan failed")
        health["last_error"], health["last_error_at"] = f"ORB scan failed: {e}", int(time.time())
    picked = {t for s in STRATEGIES.values() if s.get("universe") for t in todays(s["universe"], now.date())}
    picked |= {t for s in STRATEGIES.values() for t in s.get("tickers", [])}
    tickers = set(engine.setting("watchlist")) | picked | {p["ticker"] for p in engine.positions()} | {BENCHMARK}
    started = time.time()
    bars = {}
    for t in sorted(tickers):
        try:
            bars[t] = market.fetch_bars(t, "1mo")  # a month, so EMA200 on 5-min bars is warmed up like in replay
        except Exception as e:  # one bad symbol shouldn't stop the others
            log.warning("fetch %s failed: %s", t, e)
            health["last_error"], health["last_error_at"] = f"Couldn't fetch {t}: {e}", int(time.time())
    health["fetch_ms"] = int((time.time() - started) * 1000)
    n = engine.step(bars, int(now.timestamp()))
    engine.snapshot(int(now.timestamp()))
    health["last_tick"] = int(time.time())
    if n:
        log.info("processed %d new bars", n)


def loop():
    while True:
        now = market.now_et()
        if report_due(now):
            threading.Thread(target=build_report, daemon=True).start()
        if scan_due(now):
            threading.Thread(target=morning_scan, daemon=True).start()
        elif archive_due(now):
            threading.Thread(target=archive_bars, daemon=True).start()
        if in_session_window(now):
            try:
                tick()
            except Exception as e:
                log.exception("tick failed")
                health["last_error"], health["last_error_at"] = str(e), int(time.time())
            time.sleep(60 - time.time() % 60 + 5)  # just after each minute, when Yahoo has the closed bar
        else:
            time.sleep(20)


# ---------- dashboard data ----------
def downsample(rows, n=400):
    if len(rows) <= n:
        return rows
    k = len(rows) / n
    return [rows[int(i * k)] for i in range(n)] + [rows[-1]]


def state():
    with engine.lock:
        return _state()


def _state():
    now = market.now_et()
    start = datetime.fromisoformat(engine.setting("test_start")).date()
    day = market.trading_days_after(start, now.date())  # the start day was partial; day 1 is the next trading day
    bots = []
    for b in engine.bots():
        s = STRATEGIES[b["strategy"]]
        pos, eq = [], engine.equity_of(b)
        for p in engine.positions(b["id"]):
            px = engine.last_price.get(p["ticker"], p["entry_price"])
            pos.append({**p, "price": px, "unrealized": p["side"] * (px - p["entry_price"]) * p["qty"] - p["fees"],
                        # the size cap usually binds first, so this is often well below the risk_pct setting
                        "risk_pct": p["qty"] * abs(p["entry_price"] - p["stop"]) / eq * 100 if eq > 0 else None})
        bots.append({"id": b["id"], "name": b["name"], "strategy": b["strategy"], "blurb": s["blurb"], "status": b["status"],
                     "params": b["params"], "defaults": s["defaults"], "stats": engine.stats(b), "positions": pos,
                     "started_at": b["started_at"]})
    trades = [dict(r) for r in engine.db.execute("SELECT * FROM trades ORDER BY exit_ts DESC LIMIT 300")]
    events = [dict(r) for r in engine.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 80")]
    equity = {b["id"]: downsample([[r["ts"], r["equity"]] for r in engine.db.execute(
        "SELECT ts, equity FROM equity WHERE bot_id=? ORDER BY ts", (b["id"],))]) for b in bots}
    bench = downsample([[r["ts"], r["price"]] for r in engine.db.execute("SELECT ts, price FROM benchmark ORDER BY ts")])
    d = now.date()
    return {
        "now": int(now.timestamp()),
        "market": {"open": market.market_open(now), "next_open": int(market.next_open(now).timestamp()),
                   "closes_at": int(datetime.combine(d, market.close_time(d), ET).timestamp()) if market.is_trading_day(d) else None},
        "test": {"start": start.isoformat(), "day": max(1, day), "days": TEST_DAYS,
                 "end": market.nth_trading_day_after(start, TEST_DAYS).isoformat()},
        "health": health, "bots": bots, "trades": trades, "events": events, "equity": equity, "benchmark": bench,
        "watchlist": engine.setting("watchlist"), "account": engine.account(),
        "premarket": latest_report(), "report_building": report_state["building"],
    }


def latest_report():
    if not os.path.isdir(premarket.REPORT_DIR):
        return None
    days = sorted(f[:10] for f in os.listdir(premarket.REPORT_DIR) if f.endswith(".json"))
    return premarket.load(days[-1]) if days else None


def check_tickers(tickers):
    """Only US-listed individual stocks: the engine trades the US session only, and the lab is built around
    single stocks rather than funds. Returns (clean list, list of problems)."""
    ok, problems = [], []
    for t in tickers:
        t = t.strip().upper()
        if not t or t in ok:
            continue
        try:
            kind, exch, name = market.instrument_info(t)
        except Exception:
            problems.append(f"{t}: not found")
            continue
        if kind != "EQUITY":
            problems.append(f"{t} is {'an ETF' if kind == 'ETF' else 'a ' + str(kind).lower()}, not an individual stock")
        elif exch not in market.US_EXCHANGES:
            problems.append(f"{t} is listed on {exch}, outside the US session. Use the company's US listing if it has one")
        else:
            ok.append(t)
        time.sleep(0.2)
    return ok, problems


_checked_stocks = set()


def stocks_only(tickers, source):
    """The scans' and report's picks, minus anything that isn't a US-listed individual stock (check_tickers),
    so a fund can't be traded even if one ends up in premarket.UNIVERSE. A failed lookup also skips the stock."""
    keep = []
    for t in tickers:
        if t not in _checked_stocks:
            ok, problems = check_tickers([t])
            if not ok:
                log.warning("%s: skipped %s", source, problems)
                with engine.lock, engine.db:
                    engine.event(f"{source}: skipped {'; '.join(problems) or t} (individual US stocks only)", "report")
                continue
            _checked_stocks.add(t)
        keep.append(t)
    return keep


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if self.path == "/api/state":
            return self._send(200, state())
        self._send(404, {"error": "Not found"})

    def do_POST(self):
        # Only accept requests from the dashboard itself (blocks other websites posting to localhost).
        origin = self.headers.get("Origin")
        if origin and not re.match(rf"^http://(localhost|127\.0\.0\.1):{PORT}$", origin):
            return self._send(403, {"error": "Forbidden"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "Body must be JSON"})
        m = re.match(r"^/api/bots/(\w+)/(pause|resume|reset|params)$", self.path)
        try:
            if m and m.group(1) in STRATEGIES:
                bot, action = m.groups()
                if action == "pause": engine.set_status(bot, "paused")
                elif action == "resume": engine.set_status(bot, "running")
                elif action == "reset": engine.reset_bot(bot)
                else: engine.set_params(bot, body.get("params", {}))
            elif self.path == "/api/watchlist":
                ok, problems = check_tickers(body.get("tickers", []))
                if problems:
                    return self._send(400, {"error": "Not saved. " + "; ".join(problems) + "."})
                if not ok:
                    return self._send(400, {"error": "Add at least one stock."})
                engine.set_watchlist(ok)
            elif self.path == "/api/account":
                engine.set_account(body.get("account", {}))
            elif self.path == "/api/premarket/refresh":
                threading.Thread(target=build_report, daemon=True).start()
            elif self.path == "/api/restart-test":
                engine.restart_test()
            else:
                return self._send(404, {"error": "Not found"})
        except (ValueError, TypeError) as e:
            return self._send(400, {"error": f"Invalid value: {e}"})
        self._send(200, {"ok": True})


def main():
    threading.Thread(target=loop, daemon=True).start()
    log.info("Day-Trading Lab listening on http://localhost:%d", PORT)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
