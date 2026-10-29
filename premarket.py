"""Premarket report: gappers, premarket volume, news and market context, built before the US open.

The report is saved to reports/YYYY-MM-DD.json and .md. Its "in_play" list is what the Gapper bot trades
that day. Only individual US-listed stocks qualify: funds and ETFs are excluded by design.

    python3 premarket.py     # build today's report now and print it
"""
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta

import market
from market import ET

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(HERE, "reports")

# Liquid US-listed individual stocks across sectors. No ETFs, funds or Chinese VIE structures.
UNIVERSE = """
AAPL MSFT NVDA AMD META GOOGL AMZN TSLA AVGO ORCL CRM ADBE INTC QCOM MU TXN AMAT LRCX KLAC MRVL ARM SMCI PLTR
SNOW CRWD PANW NET DDOG SHOP UBER ABNB COIN HOOD SOFI PYPL NFLX DIS SPOT RBLX DELL HPQ IBM CSCO ANET
WMT COST TGT HD LOW NKE SBUX MCD CMG LULU KO PEP PG
UNH LLY JNJ PFE MRK ABBV AMGN GILD MRNA VRTX REGN ISRG
JPM BAC C WFC GS MS SCHW V MA AXP BLK
XOM CVX OXY SLB COP CAT DE BA GE LMT RTX UPS FDX F GM RIVN LCID AAL DAL UAL CCL
""".split()
CONTEXT = [("ES=F", "S&P 500 futures"), ("NQ=F", "Nasdaq 100 futures"), ("^VIX", "VIX"),
           ("^TNX", "US 10-year yield")]

# Stocks in play: the filters from Zarattini, Barbon & Aziz (2024), applied to the premarket.
MIN_PRICE = 5.0
MIN_AVG_VOLUME = 1_000_000
MIN_GAP_PCT = 2.0
MIN_PM_RVOL = 2.0        # premarket volume vs this stock's usual premarket volume (once 5+ reports exist)
MIN_PM_PCT_ADV = 3.0     # fallback until then: premarket volume as % of average daily volume (a judgment call)
BASELINE_REPORTS = 5
IN_PLAY_MAX = 5
NASDAQ_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 Chrome/126 Safari/537.36",
                  "Accept": "application/json"}


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20, context=market._SSL) as r:
        return json.load(r)


def _scan(symbol, now):
    """Premarket price, gap and average daily volume from Yahoo. Yahoo's premarket bars carry no volume,
    so premarket volume comes from Nasdaq (_pm_volume) for the stocks that matter."""
    d = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=5m&range=5d&includePrePost=true")
    res = d["chart"]["result"][0]
    meta, q = res["meta"], res["indicators"]["quote"][0]
    if meta.get("instrumentType") != "EQUITY" or meta.get("exchangeName") not in market.US_EXCHANGES:
        return None
    today = now.date()
    reg_vol, last_pm, prev_close = {}, None, None
    for i, ts in enumerate(res.get("timestamp") or []):
        c, v = q["close"][i], q["volume"][i] or 0
        if c is None:
            continue
        t = datetime.fromtimestamp(ts, ET)
        day = t.date()
        if t.time() < market.OPEN:
            if day == today:
                last_pm = c
        elif t.time() < market.close_time(day):
            reg_vol[day] = reg_vol.get(day, 0) + v
            if day < today:
                prev_close = c
    prev_close = meta.get("previousClose") or meta.get("chartPreviousClose") if prev_close is None else prev_close
    past_days = [x for x in reg_vol if x < today]
    if not prev_close or not past_days:
        return None
    price = last_pm or prev_close
    avg_vol = sum(reg_vol[x] for x in past_days) / len(past_days)
    return {
        "symbol": symbol, "name": meta.get("longName") or meta.get("shortName") or symbol,
        "prev_close": prev_close, "price": price, "gap_pct": (price / prev_close - 1) * 100,
        "avg_volume": avg_vol, "pm_volume": None, "pm_pct_adv": None, "pm_rvol": None,
    }


def _pm_volume(symbol, now):
    """Today's premarket share volume from Nasdaq's extended-trading page, or None."""
    try:
        d = _get(f"https://api.nasdaq.com/api/quote/{symbol}/extended-trading?markettype=pre&assetclass=stocks",
                 NASDAQ_HEADERS)["data"]
        updated = " ".join(d.get("lastUpdateInfo") or [])
        if f"{now:%b} {now.day}, {now.year}" not in updated:
            return None  # still yesterday's premarket
        return int(float(d["infoTable"]["rows"][0]["volume"].replace(",", "")))
    except Exception:
        return None


def _pm_baseline(symbol, today):
    """Average premarket volume in earlier on-time reports, if there are enough of them."""
    vols = []
    for f in sorted(os.listdir(REPORT_DIR)) if os.path.isdir(REPORT_DIR) else []:
        if not f.endswith(".json") or f[:10] >= today.isoformat():
            continue
        try:
            with open(os.path.join(REPORT_DIR, f)) as fh:
                r = json.load(fh)
        except (OSError, ValueError):
            continue
        if r.get("late"):
            continue
        for row in r.get("pm_volumes", {}).items():
            if row[0] == symbol and row[1]:
                vols.append(row[1])
    return sum(vols[-10:]) / len(vols[-10:]) if len(vols) >= BASELINE_REPORTS else None


def _news(symbol, since_ts):
    try:
        d = _get(f"https://query2.finance.yahoo.com/v1/finance/search?q={symbol}&quotesCount=0&newsCount=6")
    except Exception:
        return []
    out = []
    for n in d.get("news", []):
        if n.get("providerPublishTime", 0) >= since_ts:
            out.append({"title": n.get("title"), "publisher": n.get("publisher"), "link": n.get("link"),
                        "ts": n.get("providerPublishTime")})
    return out[:3]


# SEC EDGAR: free, official and immediate (earnings, guidance and deals are filed before the articles appear).
SEC_HEADERS = {"User-Agent": "DayTradingLab/1.0 personal research script", "Accept": "application/json"}
SEC_FORMS = ("8-K", "8-K/A", "6-K")  # 6-K: the 8-K equivalent for foreign companies
SEC_ITEMS = {"1.01": "Material agreement", "1.02": "Agreement ended", "1.03": "Bankruptcy", "2.01": "Acquisition or sale done",
             "2.02": "Earnings results", "2.03": "New debt", "2.05": "Restructuring", "2.06": "Impairment",
             "3.01": "Delisting notice", "3.02": "Share sale", "4.01": "Auditor change", "4.02": "Restatement",
             "5.02": "Executive or board change", "5.07": "Shareholder vote", "7.01": "Investor update", "8.01": "Other news"}
_ciks = {}


def _cik(symbol):
    """SEC company number for a ticker (cached for the life of the process)."""
    if symbol not in _ciks:
        d = _get(f"https://efts.sec.gov/LATEST/search-index?keysTyped={symbol}", SEC_HEADERS)
        _ciks[symbol] = next((int(h["_id"]) for h in d["hits"]["hits"]
                              if symbol in (h["_source"].get("tickers") or "").split(", ")), None)
    return _ciks[symbol]


def _filings(symbol, since_ts):
    """The company's 8-K/6-K filings since since_ts, shaped like news items, newest first."""
    try:
        cik = _cik(symbol)
        r = _get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", SEC_HEADERS)["filings"]["recent"] if cik else None
    except Exception:
        return []
    out = []
    for form, acc, doc, items, at in zip(*(r[k] for k in ("form", "accessionNumber", "primaryDocument", "items",
                                                          "acceptanceDateTime"))) if r else ():
        ts = int(datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp())
        if form not in SEC_FORMS or ts < since_ts:
            continue
        what = [SEC_ITEMS[i] for i in items.split(",") if i in SEC_ITEMS]
        out.append({"title": f"SEC {form}: {', '.join(what) or 'company filing'}", "publisher": "SEC EDGAR",
                    "link": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}", "ts": ts})
    return sorted(out, key=lambda n: -n["ts"])[:2]


def build(watchlist, now=None):
    now = now or market.now_et()
    rows, failed = [], []
    for s in sorted(set(UNIVERSE) | set(watchlist)):
        try:
            r = _scan(s, now)
            if r:
                rows.append(r)
        except Exception:
            failed.append(s)
        time.sleep(0.15)

    liquid = [r for r in rows if r["price"] >= MIN_PRICE and r["avg_volume"] >= MIN_AVG_VOLUME]
    gappers = sorted([r for r in liquid if abs(r["gap_pct"]) >= MIN_GAP_PCT], key=lambda r: -abs(r["gap_pct"]))

    for r in gappers[:25] + [r for r in rows if r["symbol"] in watchlist]:
        if r["pm_volume"] is None:
            r["pm_volume"] = _pm_volume(r["symbol"], now)
            time.sleep(0.2)
            if r["pm_volume"] is not None:
                r["pm_pct_adv"] = r["pm_volume"] / r["avg_volume"] * 100
                base = _pm_baseline(r["symbol"], now.date())
                r["pm_rvol"] = r["pm_volume"] / base if base else None

    def active(r):
        if r["pm_rvol"] is not None:
            return r["pm_rvol"] >= MIN_PM_RVOL
        return (r["pm_pct_adv"] or 0) >= MIN_PM_PCT_ADV

    # Long-only account: the Gapper bot can only use gap-ups. Rank by gap x premarket activity.
    in_play = sorted([r for r in gappers if r["gap_pct"] > 0 and active(r)],
                     key=lambda r: -(r["gap_pct"] * (r["pm_rvol"] or r["pm_pct_adv"] / MIN_PM_PCT_ADV)))[:IN_PLAY_MAX]

    since = int((now - timedelta(hours=18)).timestamp())
    for r in gappers[:15] + [r for r in rows if r["symbol"] in watchlist]:
        if "news" not in r:
            r["news"] = _filings(r["symbol"], since) + _news(r["symbol"], since)  # official filings first
            time.sleep(0.15)

    context = []
    for sym, label in CONTEXT:
        try:
            m = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=5m&range=1d&includePrePost=true")["chart"]["result"][0]["meta"]
            price, prev = m.get("regularMarketPrice"), m.get("chartPreviousClose") or m.get("previousClose")
            context.append({"symbol": sym, "label": label, "price": price,
                            "change_pct": (price / prev - 1) * 100 if price and prev else None})
        except Exception:
            context.append({"symbol": sym, "label": label, "price": None, "change_pct": None})

    return {
        "date": now.date().isoformat(), "generated_at": int(now.timestamp()),
        "generated_et": now.strftime("%H:%M"), "late": now.time() >= market.OPEN,
        "context": context, "gappers": gappers[:25], "in_play": [r["symbol"] for r in in_play],
        "in_play_rows": in_play, "watchlist": [r for r in rows if r["symbol"] in watchlist],
        "scanned": len(rows), "failed": failed,
        "pm_volumes": {r["symbol"]: r["pm_volume"] for r in rows if r["pm_volume"] is not None},
        "rules": {"min_price": MIN_PRICE, "min_avg_volume": MIN_AVG_VOLUME, "min_gap_pct": MIN_GAP_PCT,
                  "min_pm_rvol": MIN_PM_RVOL, "min_pm_pct_adv": MIN_PM_PCT_ADV, "max_in_play": IN_PLAY_MAX},
    }


def _fmt_vol(v):
    if v is None:
        return "–"
    return f"{v / 1e6:.1f}M" if v >= 1e6 else f"{v / 1e3:.0f}k"


def _activity(x):
    if x.get("pm_rvol") is not None:
        return f"{x['pm_rvol']:.1f}x usual premarket"
    if x.get("pm_pct_adv") is not None:
        return f"{x['pm_pct_adv']:.1f}% of daily volume"
    return "no volume data"


def to_markdown(r):
    L = [f"# Premarket report {r['date']}", "",
         f"Built {r['generated_et']} ET{' (after the open)' if r['late'] else ''}. "
         f"Scanned {r['scanned']} US-listed stocks.", "", "## Market", ""]
    for c in r["context"]:
        ch = f"{c['change_pct']:+.2f}%" if c["change_pct"] is not None else "–"
        L.append(f"- {c['label']}: {c['price'] if c['price'] is not None else '–'} ({ch})")
    L += ["", "## In play today (Gapper bot trades these)", ""]
    if r["in_play"]:
        for x in r["in_play_rows"]:
            L.append(f"- **{x['symbol']}** {x['gap_pct']:+.1f}% gap, premarket volume {_fmt_vol(x['pm_volume'])} "
                     f"({_activity(x)})" + (f": {x['news'][0]['title']}" if x.get("news") else ""))
    else:
        L.append("- Nothing qualifies (gap up ≥ 2% with unusual premarket volume). The Gapper bot sits out today.")
    L += ["", "## Biggest gaps", "", "| Stock | Gap | Price | PM volume | PM activity | News |", "|---|---|---|---|---|---|"]
    for x in r["gappers"]:
        news = x["news"][0]["title"].replace("|", "/") if x.get("news") else ""
        L.append(f"| {x['symbol']} | {x['gap_pct']:+.1f}% | {x['price']:.2f} | {_fmt_vol(x['pm_volume'])} | {_activity(x)} | {news} |")
    L += ["", "## Bot watchlist", ""]
    for x in sorted(r["watchlist"], key=lambda x: -abs(x["gap_pct"])):
        L.append(f"- {x['symbol']}: {x['gap_pct']:+.1f}%, premarket {_fmt_vol(x['pm_volume'])} ({_activity(x)})"
                 + (f". {x['news'][0]['title']}" if x.get("news") else ""))
    if r["failed"]:
        L += ["", f"Couldn't fetch: {', '.join(r['failed'])}"]
    return "\n".join(L) + "\n"


def save(r):
    os.makedirs(REPORT_DIR, exist_ok=True)
    base = os.path.join(REPORT_DIR, r["date"])
    with open(base + ".json", "w") as f:
        json.dump(r, f, indent=1)
    with open(base + ".md", "w") as f:
        f.write(to_markdown(r))
    return base


def load(day):
    try:
        with open(os.path.join(REPORT_DIR, f"{day}.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


if __name__ == "__main__":
    import sys
    sys.path.insert(0, HERE)
    from engine import DEFAULT_WATCHLIST
    rep = build(DEFAULT_WATCHLIST)
    print(to_markdown(rep))
