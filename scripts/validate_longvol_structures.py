"""Validates long_straddle/strangle (2026-09-08) through the SAME DSR/PBO/
Monte-Carlo/cost-model gates that validated short_vol's own condor and
rejected trend-following -- via short_vol_backtest.py's own run_sweep()/
evaluate_short_vol_gates(), now that both accept a `structure` parameter.

Sweeps only `sd` (the one dimension that actually changes these structures'
strikes -- strangle's OTM distance; long_straddle is always ATM regardless
of sd, so its own "sweep" is a single degenerate variant, disclosed as such
rather than padded with min_vrp/kelly_fraction values that don't apply to a
long-only, non-Kelly-sized structure -- see short_vol.py::decide()'s own
comments on why those don't map onto this structure family).

Usage:
    python -m scripts.validate_longvol_structures
    python -m scripts.validate_longvol_structures --underlying NIFTY
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_platform.backtesting.short_vol_backtest import (
    evaluate_short_vol_gates,
    load_daily_closes,
    run_sweep,
)

_SD_GRID = [{"sd": sd, "min_vrp": 0.0, "kelly_fraction": 0.0} for sd in (0.75, 1.0, 1.25, 1.5)]


def _load_vix() -> dict:
    from datetime import date
    path = Path("data/historical/INDIAVIX__ONE_DAY_deep.csv")
    if not path.exists():
        path = Path("data/historical/INDIAVIX__ONE_DAY.csv")
    if not path.exists():
        raise SystemExit(f"no INDIAVIX history found under data/historical/")
    bars = load_daily_closes(path)
    return {b.day: b.close for b in bars}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--underlying", default="NIFTY")
    args = parser.parse_args()

    csv_path = Path(f"data/historical/{args.underlying}__ONE_DAY_deep.csv")
    if not csv_path.exists():
        csv_path = Path(f"data/historical/{args.underlying}__ONE_DAY.csv")
    if not csv_path.exists():
        raise SystemExit(f"no historical CSV for {args.underlying}")
    bars = load_daily_closes(csv_path)
    vix_by_day = _load_vix()
    print(f"{args.underlying}: {len(bars)} daily bars, {bars[0].day} .. {bars[-1].day}, "
          f"{len(vix_by_day)} VIX observations")

    for structure in ("long_straddle", "strangle"):
        sweep = run_sweep(bars, vix_by_day, underlying=args.underlying, structure=structure, grid=_SD_GRID)
        n_trades = [len(r.trades) for r in sweep]
        gates = evaluate_short_vol_gates(sweep, strategy_id=f"{structure}_{args.underlying}")
        print(f"\n=== {structure} ===")
        print(f"  trades per sd-variant: {n_trades}")
        for gate in (gates.dsr, gates.pbo, gates.monte_carlo, gates.cost_model):
            if gate is not None:
                print(f"  {gate.gate_name:16} {gate.result.value:5} {gate.message}")
        best = max(sweep, key=lambda r: r.final_equity)
        cagr = (best.final_equity / best.starting_capital) - 1.0 if best.starting_capital else 0.0
        print(f"  best variant: final_equity={best.final_equity:.0f} "
              f"(vs start {best.starting_capital:.0f}, {cagr*100:.2f}% over the window) "
              f"trades={len(best.trades)}")
        print(f"  VERDICT: {'PASS' if gates.all_passed else 'FAIL'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
