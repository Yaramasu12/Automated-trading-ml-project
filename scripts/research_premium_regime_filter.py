#!/usr/bin/env python3
"""Does a slow "is the vol premium rich right now?" filter help, out of sample?

Motivation (measured, scripts/validate_index_strategies.py): the short-vol
condor's yearly return tracks the vol risk premium (implied - realized vol)
almost one-for-one on NIFTY, BANKNIFTY and FINNIFTY, and that premium has
collapsed since 2024 (NIFTY 6.6 pts in 2021 -> 0.6 in 2026). If the premium is
what is being harvested, trading only when it is rich should help -- but that
is exactly the sort of idea that looks brilliant in-sample. So:

  * the threshold set is small and fixed in advance: {0, 1, 2, 3, 4} pts of
    trailing-60-day MEDIAN premium (a slow regime filter, not a per-trade gate)
  * theta is CHOSEN on 2019-2022 only (best in-sample Sharpe)
  * the chosen theta is then evaluated ONCE on 2023-2026 (out of sample), which
    contains both a rich year (2023) and the thin era (2024-26)
  * every theta's out-of-sample result is printed too, so nothing is hidden

Prereq: scripts/build_index_iv_history.py has been run.

    python scripts/research_premium_regime_filter.py
"""
from __future__ import annotations

import argparse
import math
import statistics
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for p in (REPO, REPO / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import validate_index_strategies as v  # noqa: E402
from trading_platform.backtesting.short_vol_backtest import ShortVolBacktester  # noqa: E402
from trading_platform.strategies.short_vol import ShortVolStrategy  # noqa: E402

THETAS = (0.0, 1.0, 2.0, 3.0, 4.0)
SPLIT = date(2023, 1, 1)
# (index, hold_days, sd, spread) -- the tradeable-today shapes; min_vrp is 0 so
# the regime filter is the ONLY premium gate being tested.
CONFIGS = (
    ("NIFTY", 5, 1.0, 1.0),
    ("BANKNIFTY", 10, 1.0, 1.5),
    ("FINNIFTY", 10, 1.0, 2.0),
)


def premium_series(bars, iv):
    """day -> (vrp, trailing-60d median vrp)"""
    vrp, out = {}, {}
    hist: list[float] = []
    for i, b in enumerate(bars):
        rv = v.realized_vol(bars, i)
        if rv is not None and b.day in iv:
            vrp[b.day] = iv[b.day] - rv
            hist.append(vrp[b.day])
            out[b.day] = (vrp[b.day], statistics.median(hist[-60:]) if len(hist) >= 20 else None)
    return out


def run(idx, bars, iv, hold, sd, spread, entry_filter):
    spec = v.SPECS[idx]
    strat = ShortVolStrategy(sd=sd, min_vrp=0.0, kelly_fraction=0.30, risk_budget=0.05)
    bt = ShortVolBacktester(underlying=idx, hold_days=hold, spread_points=spread, strategy=strat,
                            wing_pct=v.WING_PCT, entry_filter=entry_filter)
    bt.lot_size, bt.strike_step = spec["lot"], spec["step"]
    return bt.run(bars, iv)


def window_stats(res, lo: date, hi: date):
    pts = [(d, e) for d, e in res.equity_curve if lo <= d <= hi]
    if len(pts) < 30:
        return None
    vals = [e for _, e in pts]
    peak, mdd = vals[0], 0.0
    for x in vals:
        peak = max(peak, x)
        mdd = max(mdd, (peak - x) / peak)
    rets = [(vals[i] - vals[i - 1]) / vals[i - 1] for i in range(1, len(vals)) if vals[i - 1] > 0]
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    sharpe = statistics.mean(rets) / sd * math.sqrt(252) if sd > 0 else 0.0
    yrs = max((pts[-1][0] - pts[0][0]).days / 365.25, 1e-9)
    cagr = ((vals[-1] / vals[0]) ** (1 / yrs) - 1) * 100
    n = sum(1 for t in res.trades if lo <= t.entry_day <= hi)
    return dict(cagr=cagr, mdd=mdd * 100, sharpe=sharpe, n=n, yrs=yrs)


def fmt(s):
    return "  n/a" if s is None else f"{s['cagr']:6.2f}% DD {s['mdd']:5.1f}% Sh {s['sharpe']:5.2f} n={s['n']:3d}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(REPO / "audit_results" / "2026-09-19" / "premium_regime_filter.md"))
    args = ap.parse_args()
    lines = ["# Premium-regime filter: chosen in-sample (2019-2022), judged out-of-sample (2023-2026)\n"]
    current = []
    for idx, hold, sd, spread in CONFIGS:
        bars = v.load_bars(idx)
        iv = v.load_iv(v.HIST / f"{idx}__IV30_deep.csv")
        bars = [b for b in bars if b.day in iv]
        prem = premium_series(bars, iv)
        first, last = bars[0].day, bars[-1].day
        lines.append(f"\n## {idx}  (condor, hold {hold}, sd {sd}, spread {spread} pts/leg; {first} .. {last})\n")
        lines.append("| filter (trailing-60d median premium >=) | in-sample 2019-22 | OUT-OF-SAMPLE 2023-26 | OOS 2024-26 only (thin era) |")
        lines.append("|---|---|---|---|")
        results = {}
        for theta in (None,) + THETAS:
            if theta is None:
                flt = None
            else:
                flt = (lambda d, _t=theta: (prem.get(d, (None, None))[1] is not None
                                            and prem[d][1] >= _t))
            res = run(idx, bars, iv, hold, sd, spread, flt)
            results[theta] = res
            ins = window_stats(res, first, date(2022, 12, 31))
            oos = window_stats(res, SPLIT, last)
            thin = window_stats(res, date(2024, 1, 1), last)
            label = "none (always trade)" if theta is None else f">= {theta:g} pts"
            lines.append(f"| {label} | {fmt(ins)} | {fmt(oos)} | {fmt(thin)} |")
        # pre-committed selection: best in-sample Sharpe among the filtered thetas
        best_theta = max(THETAS, key=lambda t: (window_stats(results[t], first, date(2022, 12, 31)) or {"sharpe": -9})["sharpe"])
        chosen = window_stats(results[best_theta], SPLIT, last)
        base = window_stats(results[None], SPLIT, last)
        lines.append(f"\ntheta chosen on 2019-22 data only: **>= {best_theta:g} pts** -> OOS {fmt(chosen)}  |  "
                     f"unfiltered OOS {fmt(base)}")
        cur = prem[last]
        current.append((idx, last, iv[last], cur[0], cur[1], best_theta))
    lines.append("\n## Where each index's premium stands today\n")
    lines.append("| index | as of | ATM-IV30 % | premium today (pts) | trailing-60d median (pts) | chosen theta | filter says |")
    lines.append("|---|---|---|---|---|---|---|")
    for idx, day, ivv, vrp, med, th in current:
        lines.append(f"| {idx} | {day} | {ivv:.2f} | {vrp:+.2f} | {med:+.2f} | {th:g} | "
                     f"{'TRADE' if med is not None and med >= th else 'STAND ASIDE'} |")
    text = "\n".join(lines)
    Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
