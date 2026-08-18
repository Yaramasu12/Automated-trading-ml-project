#!/usr/bin/env python3
"""Run the short-vol parameter sweep through the REDESIGN §5 validation gates
and persist the results, so `short_vol` can earn promotion on evidence.

    python scripts/run_short_vol_gates.py --underlying NIFTY --persist

Without --persist it prints results and writes nothing, which is the safe
default for exploration. Data comes from the deep-history CSVs fetched via
Angel One (see data/historical/*__ONE_DAY_deep.csv); regenerate those first if
they're stale.

Read `trading_platform/backtesting/short_vol_backtest.py`'s LIMITATIONS section
before treating a PASS here as proof of live profitability — option legs are
Black-Scholes model prices off India VIX, not historical traded prices.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from trading_platform.backtesting.short_vol_backtest import (  # noqa: E402
    SWEEP_GRID,
    evaluate_short_vol_gates,
    load_daily_closes,
    run_sweep,
)

HIST = REPO / "data" / "historical"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--underlying", default="NIFTY")
    ap.add_argument("--capital", type=float, default=1_000_000.0)
    ap.add_argument("--persist", action="store_true",
                    help="write gate results to the backtest_gate_results table")
    args = ap.parse_args()

    under = args.underlying.upper()
    px_path = HIST / f"{under}__ONE_DAY_deep.csv"
    vix_path = HIST / "INDIAVIX__ONE_DAY_deep.csv"
    for p in (px_path, vix_path):
        if not p.exists():
            print(f"missing history: {p}", file=sys.stderr)
            return 2

    bars = load_daily_closes(px_path)
    vix = {b.day: b.close for b in load_daily_closes(vix_path)}
    covered = sum(1 for b in bars if b.day in vix)
    print(f"{under}: {len(bars)} bars {bars[0].day}..{bars[-1].day} | VIX covers {covered}")
    print(f"sweep: {len(SWEEP_GRID)} variants")

    sweep = run_sweep(bars, vix, underlying=under, starting_capital=args.capital)
    results = evaluate_short_vol_gates(sweep, strategy_id="short_vol")

    print(f"\n=== GATES ({under}) ===")
    for gate in (results.dsr, results.pbo, results.monte_carlo,
                 results.cost_model, results.promotion_ladder):
        if gate is not None:
            print(f"  {gate.gate_name:16} {gate.result.value:5} {gate.message}")
    print(f"\nall_passed: {results.all_passed}")

    best = max(sweep, key=lambda r: r.final_equity)
    info = best.to_dict()
    years = (bars[-1].day - bars[0].day).days / 365.25
    cagr = ((best.final_equity / best.starting_capital) ** (1 / years) - 1) * 100
    print(f"\nbest variant: {info['params']}")
    print(f"  trades={info['trades']} win_rate={info['win_rate']:.1%} "
          f"net={info['net_pnl']:,.0f} charges={info['total_charges']:,.0f} "
          f"CAGR={cagr:.2f}%/yr over {years:.1f}y")

    if args.persist:
        from trading_platform.config import load_settings
        from trading_platform.data.persistence import TradingDatabase

        settings = load_settings()
        db = TradingDatabase(database_url=settings.database_url or None)
        db.save_gate_results_batch(
            results.backtest_id, "short_vol", results,
            config_snapshot={"underlying": under, "grid_size": len(SWEEP_GRID),
                             "bars": len(bars), "capital": args.capital},
        )
        print(f"\npersisted under backtest_id={results.backtest_id} strategy_id=short_vol")
    else:
        print("\n(dry run — pass --persist to record these results)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
