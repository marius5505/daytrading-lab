"""Development-window stress battery for the 15 algorithms + the pre-registered selection rule.
Writes results.md and results.json in this folder. Does not touch the holdout (the last 10 days in
common.HOLD); keep those for one final check after the rules are frozen.

    python3 stress/algos/run_algos.py
"""
import json
import math
import os

import common as C
from common import algos, h

OUT, RAW, COUNT = [], {}, {"engine_runs": 0}


def out(s=""):
    print(s)
    OUT.append(s)


def run(*a, **k):
    COUNT["engine_runs"] += 1
    return C.run(*a, **k)


def f(x, fmt="{:+.1f}"):
    return "–" if x is None else fmt.format(x)


def battery(key):
    a = algos.ALGOS[key]
    second = algos.SECOND_SET[key]
    r = {"name": a["name"], "second_set": second}
    base = run(key, C.TECH)
    r["live"] = C.summary(base)
    r["brief"] = C.summary(run(key, C.TECH, model="brief"))
    r["halves"] = C.halves(base)
    r["weekly"] = C.weekly(base)
    r["tickers_tech"] = C.per_ticker(base, C.TECH)
    sec = run(key, C.SETS[second])
    r["second"] = C.summary(sec)
    r["tickers_second"] = C.per_ticker(sec, C.SETS[second])
    if second != "NONTECH":
        r["nontech_info"] = C.summary(run(key, C.NONTECH))
    if second != "HOT":
        hot = run(key, C.HOT)
        r["hot_info"] = C.summary(hot)
    else:
        hot = sec
    r["next_open"] = C.summary(run(key, C.TECH, next_open=True))
    r["slip2"] = C.summary(run(key, C.TECH, slippage=0.02))
    r["mc"] = C.monte_carlo(base, C.DEV)
    # sensitivity
    r["grid"] = {}
    d = a["defaults"]
    for p in a["key"]:
        cells = []
        for s in (-0.5, -0.25, 0.0, 0.25, 0.5):
            v = C.variant(d[p], s)
            if s == 0:
                sm = r["live"]
            else:
                sm = C.summary(run(key, C.TECH, params={p: v}))
            cells.append({"step": s, "value": v, "ret": sm["ret"], "trades": sm["trades"], "gross_bp": sm["gross_bp"]})
        g0 = r["live"]["gross_bp"]
        nb = [c for c in cells if abs(c["step"]) == 0.25]
        if g0 is None or g0 <= 0:
            flag = "no edge"
        elif all(c["gross_bp"] is not None and c["gross_bp"] >= 0.5 * g0 for c in nb):
            flag = "plateau"
        else:
            flag = "spike"
        r["grid"][p] = {"cells": cells, "flag": flag, "net_pos_25": all(c["ret"] > 0 for c in nb)}
    # gates
    hv = r["halves"]
    r["gates"] = {
        "G1 ≥30 trades": r["live"]["trades"] >= 30,
        "G2 net>0": r["live"]["ret"] > 0,
        "G3 both halves": hv[0] > 0 and hv[2] > 0,
        "G4 second set": r["second"]["ret"] > 0,
        "G5 next open": r["next_open"]["ret"] > 0,
        "G6 plateau": r["live"]["ret"] > 0 and all(g["net_pos_25"] for g in r["grid"].values()),
    }
    r["passed"] = sum(r["gates"].values())
    r["pass"] = all(r["gates"].values())
    return r, base, hot


def tables():
    keys = list(RAW)
    out("## 1. Summary (TECH, development 49 days, live model unless stated)\n")
    out("Live model = IBKR per-share commission with $0.35 minimum + SEC/FINRA, cash account (long only, T+1), "
        "50% of equity per position, max 2 positions, 0.01% slippage. Brief = 0.05% commission + 0.01% slippage per side, "
        "same account rules. Gross bp = per trade, after slippage, before commission. Score = lower 95% bound of gross bp "
        "minus live commission bp. P(profit) = 21-day day-resampling Monte Carlo.\n")
    gates = list(next(iter(RAW.values()))["gates"])
    out("| Algo | Net live | Trades | Gross bp | Cost bp | Score | Net brief | P(profit 30d) | " + " | ".join(g.split(" ")[0] for g in gates) + " | Gates |")
    out("|---|---|---|---|---|---|---|---|" + "---|" * len(gates) + "---|")
    for k in keys:
        r = RAW[k]
        L = r["live"]
        out(f"| {r['name']} | {L['ret']:+.2f}% | {L['trades']} | {f(L['gross_bp'])} | {f(L['cost_bp'])} | {f(L['score'])} | "
            f"{r['brief']['ret']:+.2f}% | {r['mc']['p_profit']:.0f}% | " +
            " | ".join("✓" if r["gates"][g] else "✗" for g in gates) + f" | {r['passed']}/6 |")
    out("\nGates: G1 ≥ 30 trades; G2 net > 0; G3 net > 0 in both dev halves; G4 net > 0 on the second set "
        "(NONTECH; HOT for A8 and S15); G5 net > 0 with next-bar-open fills; G6 net > 0 at both ±25% of every key "
        "parameter.\n")

    out("## 2. Costs, execution and stock sets\n")
    out("| Algo | Breakeven comm./side | Net brief | Next open | 2× slippage | 2nd set (which) | NONTECH info | HOT info |")
    out("|---|---|---|---|---|---|---|---|")
    for k in keys:
        r = RAW[k]
        be = r["live"]["breakeven_comm_side_pct"]
        be_s = "none (gross ≤ 0)" if be is None or be <= 0 else f"{be:.3f}%"
        nt = r.get("nontech_info", r["second"])
        hot = r.get("hot_info", r["second"])
        out(f"| {r['name']} | {be_s} | {r['brief']['ret']:+.2f}% | {r['next_open']['ret']:+.2f}% ({r['next_open']['trades']}) | "
            f"{r['slip2']['ret']:+.2f}% | {r['second']['ret']:+.2f}% ({r['second']['trades']}, {r['second_set']}, gross {f(r['second']['gross_bp'])}) | "
            f"{nt['ret']:+.2f}% ({nt['trades']}) | {hot['ret']:+.2f}% ({hot['trades']}, gross {f(hot['gross_bp'])}) |")
    out()

    out("## 3. Stability: dev halves ($ P&L on $1,000) and weeks\n")
    out(f"First half {C.DEV[0]}..{C.FIRST_HALF_END} (24 days), second half {C.DEV[24]}..{C.DEV[-1]} (25 days).\n")
    weeks = [w for w, _ in next(iter(RAW.values()))["weekly"]]
    out("| Algo | 1st half (n) | 2nd half (n) | " + " | ".join(w[5:] for w in weeks) + " | Weeks + |")
    out("|---|---|---|" + "---|" * len(weeks) + "---|")
    for k in keys:
        r = RAW[k]
        a, na, z, nz = r["halves"]
        wk = [v for _, v in r["weekly"]]
        out(f"| {r['name']} | {a:+.2f} ({na}) | {z:+.2f} ({nz}) | " + " | ".join(f"{v:+.1f}" for v in wk) +
            f" | {sum(1 for v in wk if v > 0)}/{len(wk)} |")
    out()

    out("## 4. Per ticker ($ P&L, trades), live model\n")
    out("| Algo | " + " | ".join(C.TECH) + " | second set |")
    out("|---|" + "---|" * len(C.TECH) + "---|")
    for k in keys:
        r = RAW[k]
        sec = ", ".join(f"{t} {v[0]:+.1f} ({v[1]})" for t, v in r["tickers_second"].items() if v[1])
        out(f"| {r['name']} | " + " | ".join(f"{r['tickers_tech'][t][0]:+.1f} ({r['tickers_tech'][t][1]})" for t in C.TECH) +
            f" | {sec or '–'} |")
    out()

    out("## 5. Parameter sensitivity (live model, TECH dev): net return (trades) [gross bp]\n")
    out("| Algo | Parameter | −50% | −25% | default | +25% | +50% | Flag |")
    out("|---|---|---|---|---|---|---|---|")
    for k in keys:
        r = RAW[k]
        for p, g in r["grid"].items():
            cells = []
            for c in g["cells"]:
                s = f"{c['value']}: {c['ret']:+.2f}% ({c['trades']}) [{f(c['gross_bp'])}]"
                cells.append(f"**{s}**" if c["step"] == 0 else s)
            out(f"| {r['name']} | {p} | " + " | ".join(cells) + f" | {g['flag']} |")
    out()

    out("## 6. Monte Carlo, 21 trading days (10,000 runs, whole dev days resampled, live model)\n")
    out("| Algo | P(profit) | Median | 5th pct | 95th pct | Median max DD | 95th pct max DD |")
    out("|---|---|---|---|---|---|---|")
    for k in keys:
        m = RAW[k]["mc"]
        out(f"| {RAW[k]['name']} | {m['p_profit']:.1f}% | {m['median']:+.2f}% | {m['p5']:+.2f}% | {m['p95']:+.2f}% | "
            f"{m['dd_median']:.1f}% | {m['dd95']:.1f}% |")
    out()


def select():
    out("## 7. Selection (rule pre-registered in HYPOTHESES.md)\n")
    keys = list(RAW)
    passers = sorted([k for k in keys if RAW[k]["pass"]], key=lambda k: -(RAW[k]["live"]["score"] or -1e9))
    rest = sorted([k for k in keys if not RAW[k]["pass"]],
                  key=lambda k: (-RAW[k]["passed"], -(RAW[k]["live"]["score"] if RAW[k]["live"]["score"] is not None else -1e9)))
    order = passers + rest
    out(f"{len(passers)} of {len(keys)} pass all six gates.\n")
    out("| Rank | Algo | Pass | Gates | Score | Failed gates |")
    out("|---|---|---|---|---|---|")
    for n, k in enumerate(order, 1):
        r = RAW[k]
        failed = ", ".join(g.split(" ")[0] for g, v in r["gates"].items() if not v) or "–"
        out(f"| {n} | {r['name']} | {'PASS' if r['pass'] else 'fail'} | {r['passed']}/6 | {f(r['live']['score'])} | {failed} |")
    picks = order[:3]
    out(f"\nPicks: {', '.join(RAW[k]['name'] for k in picks)}.\n")
    return picks, len(passers)


def focus_test(pool):
    """Video idea 5: are 'focus group' trades (ADR20 ≥ 4%, 20-day return > SPY's) better?"""
    out("## 8. Video idea 5: focus-group trades vs the rest (pooled dev trades of all 15, TECH + HOT, live model)\n")
    C.algos.CTX["daily"] = C.daily_all()
    C.algos.CTX["dix"] = {t: {x["d"]: k for k, x in enumerate(v)} for t, v in C.algos.CTX["daily"].items()}
    C.algos.CTX["cache"] = {}
    seen, grp = set(), {True: [], False: []}
    for tr in pool:
        sig = (tr["bot_id"], tr["ticker"], tr["entry_ts"])
        if sig in seen:  # AMD/NVDA/TSLA trades appear in both TECH and HOT runs; count once
            continue
        seen.add(sig)
        g = (tr["pnl"] + tr["fees"]) / (tr["qty"] * tr["entry_price"]) * 1e4
        grp[algos.focus(tr["ticker"], C.day(tr["entry_ts"]))].append((g, tr["pnl"] > 0))
    res = {}
    out("| Group | Trades | Win % (net) | Gross bp/trade | SE | Gross − 16 bp |")
    out("|---|---|---|---|---|---|")
    for k, lab in ((True, "focus"), (False, "other")):
        xs = [g for g, _ in grp[k]]
        n = len(xs)
        m = sum(xs) / n if n else float("nan")
        sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else float("nan")
        res[lab] = (n, m, sd)
        out(f"| {lab} | {n} | {sum(w for _, w in grp[k]) / n * 100 if n else float('nan'):.0f} | {m:+.1f} | {sd / math.sqrt(n) if n else float('nan'):.1f} | {m - 16:+.1f} |")
    (n1, m1, s1), (n2, m2, s2) = res["focus"], res["other"]
    t = (m1 - m2) / math.sqrt(s1 ** 2 / n1 + s2 ** 2 / n2) if n1 > 1 and n2 > 1 else float("nan")
    p = math.erfc(abs(t) / math.sqrt(2)) / 2  # one-sided, normal approximation
    ok = (m1 - 16 > 0) and (m1 > m2) and p < 0.05
    out(f"\nWelch t = {t:.2f}, one-sided p ≈ {p:.3f}. Pre-registered pass (focus net of 16 bp > 0 and better than the "
        f"rest at p < 0.05): **{'PASS' if ok else 'FAIL'}**.\n")
    RAW["_focus"] = {"focus": res["focus"], "other": res["other"], "t": t, "p": p, "pass": ok}


if __name__ == "__main__":
    pool = []
    for k in algos.ALGOS:
        RAW[k], base, hot = battery(k)
        pool += [t for t in base] + [t for t in hot]
        print(k, "done", RAW[k]["passed"], "/6")
    tables()
    picks, npass = select()
    focus_test(pool)
    RAW["_meta"] = {"picks": picks, "n_pass": npass, "engine_runs": COUNT["engine_runs"],
                    "dev": [str(C.DEV[0]), str(C.DEV[-1])], "holdout": [str(C.HOLD[0]), str(C.HOLD[-1])]}
    out(f"Engine runs in this script: {COUNT['engine_runs']}.")
    with open(os.path.join(C.HERE, "results.md"), "w") as fh:
        fh.write("# 15 algorithms: development-window results (generated by run_algos.py)\n\n" + "\n".join(OUT) + "\n")
    with open(os.path.join(C.HERE, "results.json"), "w") as fh:
        json.dump(RAW, fh, indent=1, default=str)
