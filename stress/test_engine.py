"""Engine edge cases, on synthetic bars in a throwaway database.

    python3 -m unittest stress/test_engine.py -v      (from the daytrader folder)

Positions are inserted directly and the watchlist is emptied unless a test needs entries, so the
strategies' own signals don't interfere. A failing test here is a real engine bug.
"""
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import engine  # noqa: E402
import market  # noqa: E402
from market import ET  # noqa: E402

DAY1, DAY2 = date(2026, 10, 6), date(2026, 10, 7)  # Tue, Wed
EARLY = date(2026, 11, 27)  # day after Thanksgiving, 13:00 close


def ts(d, hh, mm):
    return int(datetime.combine(d, time(hh, mm), ET).timestamp())


def day_bars(d, price=100.0, overrides=None, drop=()):
    """Flat 5-min bars for a full session (or to 13:00 on an early close). overrides: {"HH:MM": dict(o,h,l,c)}."""
    out, t = [], datetime.combine(d, market.OPEN, ET)
    close = datetime.combine(d, market.close_time(d), ET)
    while t < close:
        key = t.strftime("%H:%M")
        b = {"ts": int(t.timestamp()), "o": price, "h": price, "l": price, "c": price, "v": 1000}
        b.update((overrides or {}).get(key, {}))
        if key not in drop:
            out.append(b)
        t += timedelta(minutes=5)
    return out


class Base(unittest.TestCase):
    bot_id = "breakout"  # its exit() never fires, so only the engine's own exits act

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.eng = engine.Engine(os.path.join(self.dir.name, "t.db"), live=False)
        with self.eng.db:
            self.eng.db.execute("DELETE FROM bots WHERE id != ?", (self.bot_id,))
        # no commission or regulatory fees, no slippage: P&L checks are exact
        self.eng.set_account({"type": "margin", "commission_per_share": 0, "commission_min": 0, "slippage_pct": 0})
        self.eng.set_watchlist([])
        self._p = [mock.patch.object(engine, "SEC_FEE", 0.0), mock.patch.object(engine, "FINRA_TAF", 0.0)]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()
        self.eng.db.close()
        self.dir.cleanup()

    def bot(self):
        return self.eng.bots()[0]

    def hold(self, ticker, side, qty, entry, stop, target=None, entry_ts=None):
        with self.eng.db:
            self.eng.db.execute("UPDATE bots SET cash = cash - ? WHERE id=?", (side * qty * entry, self.bot_id))
            self.eng.db.execute("INSERT INTO positions(bot_id,ticker,side,qty,entry_price,entry_ts,stop,target,fees,why) "
                                "VALUES(?,?,?,?,?,?,?,?,0,'test')",
                                (self.bot_id, ticker, side, qty, entry, entry_ts or ts(DAY1, 9, 30), stop, target))

    def trades(self):
        return [dict(r) for r in self.eng.db.execute("SELECT * FROM trades ORDER BY id")]

    def after_close(self, d):
        return ts(d, 16, 1)


class Exits(Base):
    def test_gap_through_stop_fills_at_open(self):
        self.hold("AAA", 1, 2, 100, stop=99)
        bars = day_bars(DAY1, overrides={"11:00": dict(o=97, h=97.5, l=96, c=97)})
        self.eng.step({"AAA": bars}, self.after_close(DAY1))
        t = self.trades()[0]
        self.assertEqual(t["exit_reason"], "Stop (gap)")
        self.assertAlmostEqual(t["exit_price"], 97)
        self.assertAlmostEqual(t["pnl"], -6)

    def test_short_gap_through_stop_fills_at_open(self):
        self.hold("AAA", -1, 2, 100, stop=101)
        bars = day_bars(DAY1, overrides={"11:00": dict(o=103, h=104, l=102.5, c=103)})
        self.eng.step({"AAA": bars}, self.after_close(DAY1))
        t = self.trades()[0]
        self.assertEqual((t["exit_reason"], t["exit_price"], t["pnl"]), ("Stop (gap)", 103, -6))

    def test_bar_touching_stop_and_target_takes_the_stop(self):
        self.hold("AAA", 1, 1, 100, stop=99, target=101)
        bars = day_bars(DAY1, overrides={"11:00": dict(o=100, h=101.5, l=98.5, c=100)})
        self.eng.step({"AAA": bars}, self.after_close(DAY1))
        t = self.trades()[0]
        self.assertEqual((t["exit_reason"], t["exit_price"]), ("Stop", 99))

    def test_missing_midday_bars_still_apply_the_stop_on_the_next_bar(self):
        self.hold("AAA", 1, 1, 100, stop=99)
        bars = day_bars(DAY1, overrides={"11:15": dict(o=98, h=98, l=97, c=97.5)}, drop=("11:00", "11:05", "11:10"))
        self.eng.step({"AAA": bars}, self.after_close(DAY1))
        t = self.trades()[0]
        self.assertEqual((t["exit_reason"], t["exit_price"], t["exit_ts"]), ("Stop (gap)", 98, ts(DAY1, 11, 20)))

    def test_flatten_at_1555(self):
        self.hold("AAA", 1, 1, 100, stop=90)
        self.eng.step({"AAA": day_bars(DAY1)}, self.after_close(DAY1))
        t = self.trades()[0]
        self.assertEqual((t["exit_reason"], t["exit_ts"]), ("End-of-day flatten", ts(DAY1, 15, 55)))

    def test_missing_last_bars_close_at_next_open_in_replay(self):
        """Yahoo sometimes leaves bars null (fetch_bars drops them). If the 15:50 and 15:55 bars are missing,
        the flatten can't run on them; the position must still not be carried through the next session."""
        self.hold("AAA", 1, 1, 100, stop=90)
        d1 = day_bars(DAY1, drop=("15:50", "15:55"))
        d2 = day_bars(DAY2, price=95)  # overnight gap down
        self.eng.step({"AAA": d1 + d2}, self.after_close(DAY2))
        t = self.trades()[0]
        self.assertEqual((t["exit_reason"], t["exit_price"], t["exit_ts"]), ("Missed flatten", 95, ts(DAY2, 9, 30)))

    def test_early_close_day_flattens_at_1255(self):
        self.hold("AAA", 1, 1, 100, stop=90, entry_ts=ts(EARLY, 9, 30))
        bars = day_bars(EARLY)
        self.assertEqual(datetime.fromtimestamp(bars[-1]["ts"], ET).time(), time(12, 55))
        self.eng.step({"AAA": bars}, ts(EARLY, 13, 1))
        t = self.trades()[0]
        self.assertEqual((t["exit_reason"], t["exit_ts"]), ("End-of-day flatten", ts(EARLY, 12, 55)))

    def test_early_close_day_no_entries_in_last_bars(self):
        sig = {"side": 1, "stop": 99, "target": None, "why": "test"}
        self.eng.set_watchlist(["AAA"])
        with mock.patch.dict(engine.STRATEGIES[self.bot_id], {"entry": lambda *a: sig}):
            self.eng.step({"AAA": day_bars(EARLY)}, ts(EARLY, 13, 1))
        late = [t for t in self.trades() if t["entry_ts"] >= ts(EARLY, 12, 55)]
        self.assertEqual(late, [])
        self.assertEqual(self.eng.positions(), [])


class LiveCatchUp(Base):
    def setUp(self):
        super().setUp()
        self.eng.live = True

    def test_sleep_applies_exits_but_opens_only_on_fresh_bars(self):
        bars = day_bars(DAY1, overrides={"10:30": dict(o=100, h=100, l=98, c=98.5)})
        upto = lambda now: [b for b in bars if b["ts"] < now]
        self.eng.set_watchlist(["AAA"])
        now1 = ts(DAY1, 10, 1)
        self.eng.step({"AAA": upto(now1)}, now1)  # first sight: cursor starts here, history isn't traded
        self.hold("AAA", 1, 1, 100, stop=99)
        sig = lambda p, b, ind, i, ctx: {"side": 1, "stop": b[i]["c"] - 1, "target": None, "why": "test"}
        now2 = ts(DAY1, 12, 0)  # the Mac slept from 10:01 to 12:00
        with mock.patch.dict(engine.STRATEGIES[self.bot_id], {"entry": sig}):
            self.eng.step({"AAA": upto(now2)}, now2)
        t = self.trades()[0]
        self.assertEqual((t["exit_reason"], t["exit_price"], t["exit_ts"]), ("Stop", 99, ts(DAY1, 10, 35)))
        pos = self.eng.positions()
        self.assertEqual(len(pos), 1)
        self.assertGreaterEqual(pos[0]["entry_ts"], now2 - engine.STALE_ENTRY_SECONDS)

    def test_ticker_that_fails_to_fetch_is_still_flat_by_the_close(self):
        """A fetch failure means no bars for that ticker this tick (server.tick catches it). If it lasts past
        the close, the position is closed at the last known price 3 minutes after the close."""
        self.eng.set_watchlist(["AAA", "BBB"])
        a, b = day_bars(DAY1), day_bars(DAY1)
        now = ts(DAY1, 10, 1)
        self.eng.step({"AAA": [x for x in a if x["ts"] < now], "BBB": [x for x in b if x["ts"] < now]}, now)
        self.hold("AAA", 1, 1, 100, stop=90)
        for now in range(ts(DAY1, 10, 2), ts(DAY1, 16, 3), 60):  # AAA fails all afternoon, BBB fine
            self.eng.step({"BBB": [x for x in b if x["ts"] < now]}, now)
        self.assertEqual(len(self.eng.positions()), 1, "still open before 16:03")
        self.eng.step({"BBB": b}, ts(DAY1, 16, 3))
        t = self.trades()[0]
        self.assertEqual((t["exit_reason"], t["exit_ts"]), ("Flatten (closing bars missing)", ts(DAY1, 16, 0)))

    def test_closing_bars_that_arrive_late_are_used_before_the_fallback(self):
        """Mac asleep over the close: on wake the 5-min bars are there, so the normal 15:55 flatten applies."""
        self.eng.set_watchlist(["AAA"])
        a = day_bars(DAY1)
        now = ts(DAY1, 10, 1)
        self.eng.step({"AAA": [x for x in a if x["ts"] < now]}, now)
        self.hold("AAA", 1, 1, 100, stop=90)
        self.eng.step({"AAA": a}, ts(DAY2, 9, 31))
        t = self.trades()[0]
        self.assertEqual((t["exit_reason"], t["exit_ts"]), ("End-of-day flatten", ts(DAY1, 15, 55)))


class Bookkeeping(Base):
    def test_short_cash_and_pnl_with_real_fees(self):
        for p in self._p:
            p.stop()
        self._p = []
        self.eng.set_account({"type": "margin", "commission_per_share": 0.0035, "commission_min": 0.35,
                              "slippage_pct": 0.01})
        bot = self.bot()
        self.eng.last_price["AAA"] = 100.0
        self.eng._open(bot, "AAA", {"side": -1, "stop": 101, "target": None, "why": "t"}, 100.0, ts(DAY1, 10, 0), DAY1)
        pos = self.eng.positions()[0]
        qty, entry = pos["qty"], pos["entry_price"]
        self.assertAlmostEqual(entry, 100 * (1 - 0.0001))  # a short sells, slippage against the bot
        self.assertLessEqual(qty * entry, 250 + 1e-6)       # 25% cap
        open_fee = pos["fees"]
        self.assertAlmostEqual(open_fee, 0.35 + qty * entry * engine.SEC_FEE + qty * engine.FINRA_TAF)
        self.assertAlmostEqual(self.bot()["cash"], 1000 + qty * entry - open_fee)
        self.eng.last_price["AAA"] = 98.0
        self.assertAlmostEqual(self.eng.equity_of(self.bot()), 1000 + qty * (entry - 98) - open_fee)
        self.eng._close(self.bot(), pos, 95.0, ts(DAY1, 11, 0), "test")
        t = self.trades()[0]
        exit_fill = 95 * 1.0001
        self.assertAlmostEqual(t["exit_price"], exit_fill)
        self.assertAlmostEqual(t["fees"], open_fee + 0.35)  # buying to cover: no SEC/FINRA fee
        self.assertAlmostEqual(t["pnl"], qty * (entry - exit_fill) - t["fees"])
        self.assertAlmostEqual(self.bot()["cash"], 1000 + t["pnl"])
        self.assertEqual(self.eng.positions(), [])

    def test_cash_account_refuses_shorts(self):
        self.eng.set_account({"type": "cash"})
        self.assertFalse(self.eng._allowed(self.bot(), {"side": -1}, DAY1))
        self.assertTrue(self.eng._allowed(self.bot(), {"side": 1}, DAY1))

    def test_cash_account_buys_only_with_settled_cash(self):
        self.eng.set_account({"type": "cash"})
        self.eng.last_price["AAA"] = 100.0
        sig = {"side": 1, "stop": 99, "target": None, "why": "t"}
        for k in range(6):  # buy $250, sell, repeat: $1,000 of settled cash allows four
            self.eng._open(self.bot(), "AAA", sig, 100.0, ts(DAY1, 10, k), DAY1)
            for p in self.eng.positions():
                self.eng._close(self.bot(), p, 100.0, ts(DAY1, 10, k), "t")
        self.assertEqual(len(self.trades()), 4)
        self.eng._open(self.bot(), "AAA", sig, 100.0, ts(DAY2, 10, 0), DAY2)  # next day it's settled again
        self.assertEqual(len(self.eng.positions()), 1)



class ScanPicks(Base):
    """The Weekly Loser / ORB / Control bots trade stocks that scans.py picks around the open (setting
    "<universe>_today"). A stock picked at 09:35 and seen for the first time must still trade its 09:30 bar."""
    bot_id = "loser"

    def setUp(self):
        super().setUp()
        self.eng.live = True
        days, d = [], DAY1
        while len(days) < 16:  # 15 complete days for the 14-day daily ATR, plus DAY1
            if market.is_trading_day(d):
                days.append(d)
            d -= timedelta(days=1)
        self.bars = []
        for d in sorted(days):
            for b in day_bars(d):
                b.update(h=101.0, l=99.0)
                self.bars.append(b)
        self.eng.set_setting("loser_today", {"date": DAY1.isoformat(), "tickers": ["AAA"]})
        self.now = ts(DAY1, 9, 36)
        self.upto = [b for b in self.bars if b["ts"] + 300 <= self.now]

    def test_universe_is_todays_pick_only(self):
        s = engine.STRATEGIES["loser"]
        self.assertEqual(self.eng.universe(s, DAY1), ["AAA"])
        self.assertEqual(self.eng.universe(s, DAY2), [])

    def test_seeded_pick_trades_the_first_bar(self):
        self.eng.seed_cursor("AAA", ts(DAY1, 9, 30) - 1)
        self.eng.step({"AAA": self.upto}, self.now)
        pos = self.eng.positions()
        self.assertEqual(len(pos), 1)
        self.assertEqual((pos[0]["entry_ts"], pos[0]["stop"]), (ts(DAY1, 9, 35), 98.0))  # 100 - 1 x ATR of 2

    def test_unseeded_late_pick_does_not_trade_history(self):
        self.eng.step({"AAA": self.upto}, self.now)
        self.assertEqual(self.eng.positions(), [])

    def test_seed_does_not_move_an_existing_cursor(self):
        self.eng.step({"AAA": self.upto}, self.now)
        self.eng.seed_cursor("AAA", 0)
        self.assertEqual(self.eng.db.execute("SELECT last_ts FROM cursor WHERE ticker='AAA'").fetchone()[0], ts(DAY1, 9, 30))


class TestCalendar(unittest.TestCase):
    def test_test_window_counts_trading_days(self):
        start = date(2026, 10, 12)
        self.assertEqual(market.nth_trading_day_after(start, 30), date(2026, 11, 23))
        self.assertEqual(market.trading_days_after(start, date(2026, 10, 14)), 2)
        self.assertEqual(market.trading_days_after(start, date(2026, 11, 23)), 30)
        self.assertEqual(market.trading_days_after(start, start), 0)

    def test_skips_holidays(self):
        # Thanksgiving 2026-11-26 is closed: the trading day after 11-25 is 11-27
        self.assertEqual(market.nth_trading_day_after(date(2026, 11, 25), 1), date(2026, 11, 27))
        self.assertEqual(market.trading_days_after(date(2026, 11, 25), date(2026, 11, 30)), 2)


if __name__ == "__main__":
    unittest.main()
