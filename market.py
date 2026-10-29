"""US market calendar and free 5-minute bars from Yahoo's public chart API (no key, no account)."""
import json
import ssl
import time as _time
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OPEN = time(9, 30)
CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
BAR_SECONDS = 300

try:  # python.org builds ship without a CA bundle
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL = ssl.create_default_context()


def _get_json(url, tries=2, pause=1.0):
    """GET a Yahoo URL as JSON. A failure other than 404 is retried once: Yahoo sometimes answers a good request
    with a one-off 400 or 5xx (AAPL, 2026-10-14 15:13 ET), and the tick would otherwise skip that symbol."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=20, context=_SSL) as r:
                return json.load(r)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            if i == tries - 1 or getattr(e, "code", None) == 404:
                raise
            _time.sleep(pause)

# NYSE full-day closures and 13:00 early closes. Extend each December.
HOLIDAYS = {
    date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}
EARLY_CLOSES = {date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)}


def now_et():
    return datetime.now(ET)


def is_trading_day(d):
    return d.weekday() < 5 and d not in HOLIDAYS


def close_time(d):
    return EARLY_CLOSE if d in EARLY_CLOSES else CLOSE


def trading_days_back(d, n):
    """The date of the n-th most recent trading day, counting d itself as the first."""
    count = 0
    while True:
        if is_trading_day(d):
            count += 1
            if count == n:
                return d
        d -= timedelta(days=1)


def trading_days_after(start, d):
    """How many trading days fall after start, up to and including d."""
    count = 0
    while d > start:
        count += is_trading_day(d)
        d -= timedelta(days=1)
    return count


def nth_trading_day_after(start, n):
    """The date of the n-th trading day after start."""
    d = start
    while n > 0:
        d += timedelta(days=1)
        n -= is_trading_day(d)
    return d


US_EXCHANGES = {"NMS", "NGM", "NCM", "NYQ", "ASE", "PCX", "BTS", "NAS", "NYS"}


def instrument_info(symbol):
    """Yahoo's description of a symbol: (instrument type, exchange code, name)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d"
    meta = _get_json(url)["chart"]["result"][0]["meta"]
    return meta.get("instrumentType"), meta.get("exchangeName"), meta.get("longName") or meta.get("shortName") or symbol


def market_open(dt=None):
    dt = dt or now_et()
    return is_trading_day(dt.date()) and OPEN <= dt.time() < close_time(dt.date())


def next_open(dt=None):
    dt = dt or now_et()
    d = dt.date()
    if dt.time() >= OPEN:
        d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return datetime.combine(d, OPEN, ET)


def fetch_bars(symbol, range_="5d"):
    """Completed and forming 5-min regular-session bars, oldest first.

    Each bar: dict(ts=bar start epoch, o, h, l, c, v). Rows Yahoo leaves null are dropped."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?interval=5m&range={range_}&includePrePost=false")
    data = _get_json(url)
    res = data["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    bars = []
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c):
            continue
        dt = datetime.fromtimestamp(ts, ET)
        if not (OPEN <= dt.time() < close_time(dt.date())):
            continue
        bars.append({"ts": ts, "o": o, "h": h, "l": l, "c": c, "v": v or 0})
    return bars


def fetch_daily(symbol, range_="2y"):
    """Daily bars, oldest first: dict(d=date, o, h, l, c). Includes today's unfinished bar while the market is open."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range={range_}"
    res = _get_json(url)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c = (q[k][i] for k in ("open", "high", "low", "close"))
        if None not in (o, h, l, c):
            out.append({"d": datetime.fromtimestamp(ts, ET).date(), "o": o, "h": h, "l": l, "c": c})
    return out


def completed(bars, now_ts):
    return [b for b in bars if b["ts"] + BAR_SECONDS <= now_ts]
