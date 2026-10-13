#!/usr/bin/env python3
"""Gap signal through the full gate suite with a REALISTIC per-trade execution
cost (2026-10-13 go/no-go). Unlike run_gap_gates.py (flat %), this charges each
(symbol, day) trade the India intraday-equity cost from
trading_platform.backtesting.execution_cost, keyed off that name's trailing
turnover (ADV) and volatility and the per-name order size — so the gates run
against costs a real account entering ~34 names at the open would actually pay.

Position sizing: equal-weight the day's names across CAPITAL (capped per slot);
smaller per-name size => higher % brokerage, exactly the real penalty.

    python scripts/run_gap_gates_realistic.py
"""
from __future__ import annotations

import logging
import os
import sys
from collections import Counter
from datetime import date
from itertools import product
from pathlib import Path
from types import SimpleNamespace

import numpy as np

REPO = Path(__file__).resolve().parent.parent
for p in (str(REPO), str(REPO / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from research_gap_calendar_volume import HIST, _load_symbol  # noqa: E402
from research_gap_backtest import walk_forward_pnl  # noqa: E402
from run_gap_gates import build_rows, _summ  # noqa: E402
from trading_platform.backtesting.execution_cost import IntradayCostModel  # noqa: E402
from trading_platform.validation.gates import GateEvaluator  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("gap_gates_real")

CAPITAL = 1_000_000.0
MAX_SLOT = 500_000.0
COST = IntradayCostModel()

MARGINS = (0.03, 0.05, 0.08)
N_EST = (80, 160)
DEPTH = (2, 3)
LR = (0.03, 0.05)


def build_liquidity_lookup() -> dict:
    """symbol -> {date: (adv_rupees, daily_vol)} from trailing 20d turnover/returns,
    strictly PRIOR to the day (no lookahead)."""
    look: dict = {}
    for f in sorted(HIST.glob("*__ONE_DAY.csv")):
        sym = f.name.split("__")[0]
        d = _load_symbol(f)
        if d is None:
            continue
        ts = d["ts"]; close = d["close"]; vol = d["volume"]
        turnover = close * vol
        rets = np.zeros(len(close))
        rets[1:] = np.where(close[:-1] > 0, close[1:] / close[:-1] - 1.0, 0.0)
        per_day = {}
        for t in range(len(ts)):
            lo = max(0, t - 20)
            adv = float(np.mean(turnover[lo:t])) if t > lo else float(turnover[t])
            dv = float(np.std(rets[lo:t])) if t - lo >= 2 else 0.02
            day = ts[t].date() if hasattr(ts[t], "date") else ts[t]
            per_day[day] = (adv, dv)
        look[sym] = per_day
    return look


def _as_date(d):
    return d.date() if hasattr(d, "date") else d


def variant_daily_net(all_rows, liq, m, ne, md, lr):
    res = walk_forward_pnl(all_rows, n_splits=6, confidence_margin=m,
                           n_estimators=ne, max_depth=md, learning_rate=lr)
    gross = res["gross_pnl"]; dates = res["dates"]; symbols = res["symbols"]
    if len(gross) < 100:
        return None
    day_counts = Counter(_as_date(d) for d in dates)
    net_by: dict = {}
    gross_by: dict = {}
    cost_samples = []
    for g, d, s in zip(gross, dates, symbols):
        day = _as_date(d)
        per_name = min(CAPITAL / max(day_counts[day], 1), MAX_SLOT)
        adv, dv = liq.get(s, {}).get(day, (5e7, 0.03))  # default: Rs 5cr, 3% vol
        cost = COST.roundtrip_cost_fraction(per_name, adv, dv)
        cost_samples.append(cost)
        net_by.setdefault(day, []).append(g - cost)
        gross_by.setdefault(day, []).append(g)
    net_arr = np.array([float(np.mean(v)) for _, v in sorted(net_by.items())])
    gross_arr = np.array([float(np.mean(v)) for _, v in sorted(gross_by.items())])
    return {
        "net_by": {d: float(np.mean(v)) for d, v in net_by.items()},
        "net_arr": net_arr, "gross_arr": gross_arr,
        "sharpe": _summ(net_arr)["sharpe"], "auc": res.get("oos_auc", float("nan")),
        "avg_cost": float(np.mean(cost_samples)), "n_days": len(net_arr),
        "params": {"margin": m, "n_estimators": ne, "max_depth": md, "learning_rate": lr},
    }


def main() -> int:
    all_rows = build_rows()
    logger.info("building per-symbol liquidity (ADV/vol) lookup ...")
    liq = build_liquidity_lookup()
    grid = list(product(MARGINS, N_EST, DEPTH, LR))
    variants = []
    for k, (m, ne, md, lr) in enumerate(grid, 1):
        v = variant_daily_net(all_rows, liq, m, ne, md, lr)
        if v is None:
            continue
        variants.append(v)
        logger.info("  [%2d/%2d] margin=%.2f ne=%d md=%d lr=%.2f -> Sharpe=%.2f AUC=%.4f avg_cost=%.3f%% n_days=%d",
                    k, len(grid), m, ne, md, lr, v["sharpe"], v["auc"], v["avg_cost"] * 100, v["n_days"])

    if len(variants) < 2:
        logger.error("Not enough valid variants — aborting."); return 2

    union_days = sorted({d for v in variants for d in v["net_by"]})
    matrix = np.array([[v["net_by"].get(d, 0.0) for v in variants] for d in union_days])
    trial_sharpes = [v["sharpe"] for v in variants]
    best = variants[int(np.argmax(trial_sharpes))]
    net_total = float(np.sum(best["net_arr"]) * CAPITAL)
    gross_total = float(np.sum(best["gross_arr"]) * CAPITAL)
    trade_list = [{"pnl": r * CAPITAL} for r in best["net_arr"]]

    logger.info("\nchosen best: %s Sharpe=%.2f AUC=%.4f avg_cost=%.3f%% (of %d variants)",
                best["params"], best["sharpe"], best["auc"], best["avg_cost"] * 100, len(variants))

    settings = SimpleNamespace(
        DSR_MIN_TRIALS=len(variants), DEFLECTED_SHARPE_MIN=0.5, PBO_MAX=0.4,
        GATE_MAX_DRAWDOWN=0.15, MIN_NET_COST_RATIO=0.6, MIN_WALKFORWARD_SHARPE=0.3, CSCV_N_GROUPS=8,
    )
    ev = GateEvaluator(settings=settings)
    ev.evaluate_dsr(best["sharpe"], trial_sharpes, best["net_arr"])
    ev.evaluate_pbo(matrix)
    ev.evaluate_cpcv(SimpleNamespace(out_of_sample_metrics={
        "mean_oos_sharpe": best["sharpe"], "mean_oos_auc": best["auc"]}))
    ev.evaluate_monte_carlo(trade_list, starting_capital=CAPITAL)
    ev.evaluate_cost_model(net_total, gross_total - net_total)
    results = ev.finalize(backtest_id="gap-gates-real", strategy_id="gap_open_to_close_realistic")

    logger.info("\n=== GAP GATES — REALISTIC PER-TRADE COST (%d-config sweep) ===", len(variants))
    for g in (results.dsr, results.pbo, results.cpcv, results.monte_carlo, results.cost_model):
        if g is not None:
            logger.info("  %-16s %-5s %s", g.gate_name, g.result.value, g.message)
    logger.info("\nALL GATES PASSED: %s", results.all_passed)
    logger.info("avg realistic round-trip cost faced by the best config: %.3f%%", best["avg_cost"] * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
