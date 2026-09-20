#!/usr/bin/env python3
"""Which short-vol structure (if any) fits each index? -- validated on that
index's OWN implied vol, not India VIX.

Prereq: python scripts/build_index_iv_history.py   (downloads NSE bhavcopy and
writes data/historical/<INDEX>__IV30_deep.csv).

For each index this runs ONE jointly-deflated sweep of
    structure {condor, put_spread} x hold_days {5, 10, 20} x sd {1.0, 1.25, 1.5}
    x min_vrp {0, 1.5, 3}                                   = 54 trials
through the same §5 gates the NIFTY strategy had to clear (DSR, PBO, CPCV,
Monte-Carlo drawdown, cost model). Trials are counted honestly: the DSR/PBO
deflation sees all 54, so a winner picked from a wide search is held to a
higher bar. hold 5 is the weekly-expiry shape; 10/20 are the monthly-expiry
shapes that BANKNIFTY/FINNIFTY/MIDCPNIFTY are limited to since Nov 2024.

    python scripts/validate_index_strategies.py               # all indices
    python scripts/validate_index_strategies.py --only BANKNIFTY --nifty-control

Read short_vol_backtest.py's LIMITATIONS first: legs are Black-Scholes model
prices off one daily ATM IV, not traded prices. A PASS is evidence for paper
trading, not proof of live profit.
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from trading_platform.backtesting.short_vol_backtest import (  # noqa: E402
    DailyBar,
    ShortVolBacktester,
    evaluate_short_vol_gates,
    load_daily_closes,
)
from trading_platform.strategies.short_vol import ShortVolStrategy  # noqa: E402

HIST = REPO / "data" / "historical"
# Current real contract specs (lot from NSE bhavcopy NewBrdLotQty / the live
# instrument master; strike interval from the NSE contract specs).
SPECS = {
    "NIFTY": dict(lot=65, step=50, spread=1.0),
    "BANKNIFTY": dict(lot=30, step=100, spread=1.5),
    "FINNIFTY": dict(lot=60, step=50, spread=2.0),
    "MIDCPNIFTY": dict(lot=120, step=25, spread=3.0),
}
WING_PCT = 0.0125          # matches the live executor's SHORTVOL_WING_PCT
STRUCTURES = ("condor", "put_spread")
HOLDS = (5, 10, 20)
SDS = (1.0, 1.25, 1.5)
MIN_VRPS = (0.0, 1.5, 3.0)
TRADING_DAYS = 252.0


def load_bars(idx: str) -> list[DailyBar]:
    """Underlying closes: Angel One candles where we have them, else the
    put-call-parity spot recovered from the option chain itself."""
    deep, pipe = HIST / f"{idx}__ONE_DAY_deep.csv", HIST / f"{idx}__ONE_DAY_pipeline.csv"
    if deep.exists():
        by_day = {b.day: b for b in load_daily_closes(deep)}
        if pipe.exists():
            by_day.update({b.day: b for b in load_daily_closes(pipe)})
        return [by_day[d] for d in sorted(by_day)]
    return load_daily_closes(HIST / f"{idx}__PARITY_SPOT_deep.csv")


def load_iv(path: Path) -> dict[date, float]:
    return {b.day: b.close for b in load_daily_closes(path)}


def _one(args):
    idx, bars, iv, structure, hold, sd, vrp, spread, skew, wing_pct = args
    spec = SPECS[idx]
    strat = ShortVolStrategy(sd=sd, min_vrp=vrp, kelly_fraction=0.30, risk_budget=0.05)
    bt = ShortVolBacktester(underlying=idx, hold_days=hold, structure=structure,
                            spread_points=spread, strategy=strat, wing_pct=wing_pct,
                            skew_per_10pct=skew)
    bt.lot_size, bt.strike_step = spec["lot"], spec["step"]
    res = bt.run(bars, iv)
    res.params.update({"structure": structure, "hold_days": hold, "sd": sd, "min_vrp": vrp,
                       "spread_points": spread})
    return res


def sweep(idx, bars, iv, *, spread=None, skew=0.25, pool=None, wing_pct=WING_PCT):
    sp = SPECS[idx]["spread"] if spread is None else spread
    jobs = [(idx, bars, iv, st, h, sd, v, sp, skew, wing_pct)
            for st in STRUCTURES for h in HOLDS for sd in SDS for v in MIN_VRPS]
    return list(pool.map(_one, jobs)) if pool else [_one(j) for j in jobs]


def stats(res, years):
    vals = [v for _, v in res.equity_curve]
    peak, mdd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak)
    rets = res.daily_returns
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    sharpe = statistics.mean(rets) / sd * math.sqrt(TRADING_DAYS) if sd > 0 else 0.0
    cagr = (max(res.final_equity, 1.0) / res.starting_capital) ** (1 / years) - 1
    wins = sum(1 for t in res.trades if t.pnl > 0)
    return dict(cagr=cagr * 100, mdd=mdd * 100, sharpe=sharpe, n=len(res.trades),
                win=(wins / len(res.trades) * 100) if res.trades else 0.0)


def realized_vol(bars, i, window=20):
    c = [b.close for b in bars[max(0, i - window): i + 1]]
    if len(c) < window:
        return None
    r = [math.log(c[j] / c[j - 1]) for j in range(1, len(c))]
    return statistics.pstdev(r) * math.sqrt(TRADING_DAYS) * 100


def premium_profile(idx, bars, iv):
    """How rich is the vol premium on this index, in its own terms?"""
    vrp = []
    for i, b in enumerate(bars):
        v, rv = iv.get(b.day), realized_vol(bars, i)
        if v and rv:
            vrp.append((v, rv, v - rv))
    if not vrp:
        return "no overlapping IV/price data"
    n = len(vrp)
    return (f"{n} days | mean ATM-IV30 {statistics.mean(x[0] for x in vrp):5.2f}% | "
            f"mean realized {statistics.mean(x[1] for x in vrp):5.2f}% | "
            f"mean VRP {statistics.mean(x[2] for x in vrp):+5.2f} pts | "
            f"VRP>=3 on {sum(1 for x in vrp if x[2] >= 3) / n * 100:4.1f}% of days | "
            f"VRP<0 on {sum(1 for x in vrp if x[2] < 0) / n * 100:4.1f}%")


def report(label, idx, bars, iv, pool, out, *, skew=0.25, spread=None):
    bars = [b for b in bars if b.day in iv]
    if len(bars) < 250:
        out.append(f"\n## {label}: only {len(bars)} overlapping days -- too few to validate\n")
        return None
    years = (bars[-1].day - bars[0].day).days / 365.25
    out.append(f"\n## {label}\n")
    out.append(f"window {bars[0].day} .. {bars[-1].day} ({years:.1f}y, {len(bars)} days); "
               f"skew={skew}, spread={SPECS[idx]['spread'] if spread is None else spread} pts/leg, "
               f"lot {SPECS[idx]['lot']}, step {SPECS[idx]['step']}, wing {WING_PCT*100:.2f}% of spot")
    out.append("premium: " + premium_profile(idx, bars, iv))
    res = sweep(idx, bars, iv, pool=pool, skew=skew, spread=spread)
    gates = evaluate_short_vol_gates(res, strategy_id=f"short_vol_{idx.lower()}")
    out.append("\n| structure | hold | best CAGR% | max DD% | Sharpe | trades | win% | (sd, min_vrp) |")
    out.append("|---|---|---|---|---|---|---|---|")
    for st in STRUCTURES:
        for h in HOLDS:
            grp = [r for r in res if r.params["structure"] == st and r.params["hold_days"] == h]
            best = max(grp, key=lambda r: r.final_equity)
            s = stats(best, years)
            out.append(f"| {st} | {h} | {s['cagr']:.2f} | {s['mdd']:.1f} | {s['sharpe']:.2f} | "
                       f"{s['n']} | {s['win']:.0f} | ({best.params['sd']}, {best.params['min_vrp']}) |")
    best = max(res, key=lambda r: r.final_equity)
    bs = stats(best, years)
    out.append(f"\noverall best of {len(res)}: {best.params['structure']} hold {best.params['hold_days']} "
               f"sd {best.params['sd']} min_vrp {best.params['min_vrp']} -> CAGR {bs['cagr']:.2f}%, "
               f"maxDD {bs['mdd']:.1f}%, Sharpe {bs['sharpe']:.2f}, {bs['n']} trades, win {bs['win']:.0f}%")
    out.append("gates (deflated over all %d trials):" % len(res))
    for g in (gates.dsr, gates.pbo, gates.cpcv, gates.monte_carlo, gates.cost_model):
        if g is not None:
            out.append(f"  - {g.gate_name:16} {g.result.value.upper():5} {g.message}")
    out.append(f"  => all gates passed: **{gates.all_passed}**")
    return best, gates, years


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", help="restrict to these indices")
    ap.add_argument("--nifty-control", action="store_true",
                    help="also re-test NIFTY with India VIX (original) and with own-chain ATM IV")
    ap.add_argument("--skew", type=float, default=0.25)
    ap.add_argument("--out", default=str(REPO / "audit_results" / "2026-09-19" / "index_strategy_fit.md"))
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    out: list[str] = ["# Per-index short-vol strategy fit (own-chain implied vol)\n",
                      "Generated by scripts/validate_index_strategies.py. See that file's docstring "
                      "and short_vol_backtest.py's LIMITATIONS before reading any number here."]
    idxs = [i for i in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY") if not args.only or i in args.only]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        if args.nifty_control:
            vix_path = HIST / "INDIAVIX__ONE_DAY_deep.csv"
            bars = load_bars("NIFTY")
            report("NIFTY control A: India VIX as IV (the original validation input)", "NIFTY",
                   bars, load_iv(vix_path), pool, out, skew=args.skew)
        for idx in idxs:
            ivp = HIST / f"{idx}__IV30_deep.csv"
            if not ivp.exists():
                out.append(f"\n## {idx}: no IV history ({ivp.name} missing)\n")
                continue
            report(f"{idx}: own-chain ATM IV30", idx, load_bars(idx), load_iv(ivp), pool, out, skew=args.skew)
    text = "\n".join(out)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    print(f"\n(written to {args.out})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
