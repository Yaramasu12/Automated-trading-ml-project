#!/usr/bin/env python3
"""Index cash-futures BASIS signal through the full gate suite with a REALISTIC
two-leg execution cost (2026-11-04 go/no-go) — the same disciplined treatment
that killed the gap signal (reports/CANDIDATE_EDGE_SWEEP_2026-10-13.md).

research_index_basis.py's screen passed under a flat 0.05-0.10% cost. This:
  * charges the real two-leg cost from
    trading_platform.backtesting.basis_execution_cost (futures F&O + delivery
    cash/ETF STT + spreads + tracking error),
  * trades ONLY the executable side — 'basis rich -> short future / long cash';
    the 'basis cheap -> short cash' side is dropped because retail cannot hold a
    short cash/ETF position overnight in India,
  * runs a HORIZON x entry-threshold parameter sweep through DSR/PBO/CPCV/
    Monte-Carlo-DD/cost, so the verdict is deflated over the whole search, not a
    single lucky config.

    python scripts/run_index_basis_gates_realistic.py
"""
from __future__ import annotations

import logging
import sys
from datetime import date
from itertools import product
from pathlib import Path
from types import SimpleNamespace

import numpy as np

REPO = Path(__file__).resolve().parent.parent
for p in (str(REPO), str(REPO / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from research_index_basis import fetch  # noqa: E402
from trading_platform.backtesting.basis_execution_cost import BasisCostModel  # noqa: E402
from trading_platform.validation.gates import GateEvaluator  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("basis_gates_real")

CAPITAL = 1_000_000.0
LEG_NOTIONAL = 1_000_000.0          # ~1 index-future lot; cost frac is size-insensitive
COST = BasisCostModel()
ROUNDTRIP = COST.roundtrip_cost_fraction(LEG_NOTIONAL)

HORIZONS = (2, 3, 5)
HI_PCTL = (60, 67, 75)              # 'rich basis' entry threshold (executable side)


def variant_trades(data: dict, horizon: int, hi_pctl: float) -> dict:
    """Rich-side-only basis trades across NIFTY+BANKNIFTY for one (horizon,
    threshold). Returns {entry_date: net_return} and the net/gross arrays."""
    net_by: dict = {}
    gross_list: list[float] = []
    net_list: list[float] = []
    for _label, (dates, spot, fut) in data.items():
        if len(dates) < 40:
            continue
        s = np.array([spot[d] for d in dates])
        f = np.array([fut[d] for d in dates])
        basis = f / s - 1.0
        thr = np.percentile(basis, hi_pctl)
        i = 0
        while i < len(dates) - horizon:
            if basis[i] >= thr:
                # basis rich -> short future / long cash; profit if basis shrinks
                gross = -(basis[i + horizon] - basis[i])
                net = gross - ROUNDTRIP
                net_by[dates[i]] = net_by.get(dates[i], 0.0) + net
                gross_list.append(gross)
                net_list.append(net)
                i += horizon
            else:
                i += 1
    return {"net_by": net_by, "gross": np.array(gross_list), "net": np.array(net_list)}


def _sharpe(arr: np.ndarray, horizon: int) -> float:
    if arr.size < 2 or arr.std() == 0:
        return 0.0
    return float(arr.mean() / arr.std() * np.sqrt(250.0 / horizon))


def main() -> int:
    logger.info("Realistic two-leg round-trip cost = %.3f%% of notional "
                "(futures %.3f%% + cash/ETF %.3f%%)",
                ROUNDTRIP * 100,
                COST.futures_leg_fraction(LEG_NOTIONAL) * 100,
                COST.cash_leg_fraction(LEG_NOTIONAL) * 100)
    logger.info("Fetching NIFTY/BANKNIFTY spot + near-month futures ...")
    data = fetch()

    variants = []
    for horizon, hi in product(HORIZONS, HI_PCTL):
        t = variant_trades(data, horizon, hi)
        if t["net"].size < 10:
            continue
        sharpe = _sharpe(t["net"], horizon)
        variants.append({
            "horizon": horizon, "hi": hi, "net_by": t["net_by"],
            "net": t["net"], "gross": t["gross"], "sharpe": sharpe,
            "gross_mean": float(t["gross"].mean()), "net_mean": float(t["net"].mean()),
            "n": int(t["net"].size),
        })
        logger.info("  H=%d hi=%d%%  trades=%3d  gross/trade=%+.3f%%  net/trade=%+.3f%%  Sharpe=%+.2f",
                    horizon, hi, t["net"].size, t["gross"].mean() * 100, t["net"].mean() * 100, sharpe)

    if len(variants) < 2:
        logger.error("Not enough variants — aborting."); return 2

    trial_sharpes = [v["sharpe"] for v in variants]
    best = variants[int(np.argmax(trial_sharpes))]
    logger.info("\nchosen best: H=%d hi=%d%%  Sharpe=%+.2f  gross/trade=%+.3f%%  net/trade=%+.3f%%  (%d variants)",
                best["horizon"], best["hi"], best["sharpe"],
                best["gross_mean"] * 100, best["net_mean"] * 100, len(variants))

    union_days = sorted({d for v in variants for d in v["net_by"]})
    matrix = np.array([[v["net_by"].get(d, 0.0) for v in variants] for d in union_days])
    net_arr = best["net"]
    # honest OOS: second half of the chronological trades
    half = max(1, len(net_arr) // 2)
    oos_sharpe = _sharpe(net_arr[half:], best["horizon"])
    net_total = float(net_arr.sum() * CAPITAL)
    charges_total = float(ROUNDTRIP * best["n"] * CAPITAL)
    gross_total = net_total + charges_total
    trade_list = [{"pnl": r * CAPITAL} for r in net_arr]

    settings = SimpleNamespace(
        DSR_MIN_TRIALS=len(variants), DEFLECTED_SHARPE_MIN=0.5, PBO_MAX=0.4,
        GATE_MAX_DRAWDOWN=0.15, MIN_NET_COST_RATIO=0.6, MIN_WALKFORWARD_SHARPE=0.3, CSCV_N_GROUPS=8,
    )
    ev = GateEvaluator(settings=settings)
    ev.evaluate_dsr(best["sharpe"], trial_sharpes, net_arr)
    ev.evaluate_pbo(matrix)
    ev.evaluate_cpcv(SimpleNamespace(out_of_sample_metrics={
        "mean_oos_sharpe": oos_sharpe, "mean_oos_auc": 0.5}))
    ev.evaluate_monte_carlo(trade_list, starting_capital=CAPITAL)
    ev.evaluate_cost_model(net_total, gross_total - net_total)
    results = ev.finalize(backtest_id="index-basis-gates-real", strategy_id="index_basis_realistic")

    logger.info("\n=== INDEX-BASIS GATES — REALISTIC TWO-LEG COST (%d-config sweep) ===", len(variants))
    for g in (results.dsr, results.pbo, results.cpcv, results.monte_carlo, results.cost_model):
        if g is not None:
            logger.info("  %-16s %-5s %s", g.gate_name, g.result.value, g.message)
    logger.info("\nALL GATES PASSED: %s", results.all_passed)
    logger.info("realistic round-trip cost charged per trade: %.3f%%  |  best gross/trade: %+.3f%%",
                ROUNDTRIP * 100, best["gross_mean"] * 100)
    logger.info("VERDICT: %s", "SURVIVES realistic cost — promote to validation/build"
                if results.all_passed else
                "FAILS under realistic two-leg cost — not promoted (same wall as gap)")
    return 0 if results.all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
