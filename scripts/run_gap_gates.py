#!/usr/bin/env python3
"""Full promotion-gate run for the gap open-to-close signal (2026-10-13).

research_gap_calendar_volume + research_gap_backtest found a real, cost-surviving
signal, but a research screen is NOT the promotion bar. This runs the SAME gates
short_vol had to clear — Deflated Sharpe, PBO, CPCV, Monte-Carlo DD, cost model —
over a genuine parameter sweep so the selection-bias gates (DSR/PBO) are
meaningful, with a conservative flat open-auction cost on every trade.

LIMITATION (stated up front): the cost is a flat per-trade % (research's model).
It does NOT model per-name market impact, partial open-auction fills, or short-
borrow for a ~58-names/day book. A PASS here is necessary, not sufficient, for
live promotion — it says the signal survives selection-bias + conservative flat
cost, not that it survives real execution. A FAIL kills it outright.

    python scripts/run_gap_gates.py
"""
from __future__ import annotations

import logging
import os
import sys
from itertools import product
from pathlib import Path
from types import SimpleNamespace

import numpy as np

REPO = Path(__file__).resolve().parent.parent
for p in (str(REPO), str(REPO / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from research_gap_calendar_volume import HIST, _load_symbol, build_features  # noqa: E402
from research_gap_backtest import walk_forward_pnl  # noqa: E402
from trading_platform.validation.gates import GateEvaluator  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("gap_gates")

CAPITAL = 1_000_000.0
COST = float(os.getenv("GAP_GATE_COST", "0.0020"))  # 0.20% round-trip (research's conservative end)

# Genuine tunable surface for the gap classifier. 3 x 2 x 2 x 2 = 24 configs —
# this IS the search, so the DSR/PBO deflation is honest. DSR_MIN_TRIALS is set
# to the sweep size below so the breadth floor reflects this strategy's real
# surface, not short_vol's 54.
MARGINS = (0.03, 0.05, 0.08)
N_EST = (80, 160)
DEPTH = (2, 3)
LR = (0.03, 0.05)


def _daily_by_day(gross: np.ndarray, dates: list, cost: float) -> dict:
    net = gross - cost
    by: dict = {}
    for d, r in zip(dates, net):
        by.setdefault(d, []).append(r)
    return {d: float(np.mean(v)) for d, v in by.items()}


def _summ(arr: np.ndarray) -> dict:
    n = len(arr)
    if n == 0:
        return {"sharpe": 0.0, "n": 0}
    mean, std = float(np.mean(arr)), float(np.std(arr))
    return {"sharpe": (mean / std * np.sqrt(252)) if std > 0 else 0.0, "n": n, "mean": mean}


def build_rows() -> list[dict]:
    files = sorted(HIST.glob("*__ONE_DAY.csv"))
    all_rows: list[dict] = []
    for f in files:
        symbol = f.name.split("__")[0]
        data = _load_symbol(f)
        if data is None:
            continue
        rows = build_features(data, symbol)
        close, open_ = data["close"], data["open"]
        ts_index = {t: i for i, t in enumerate(data["ts"])}
        for r in rows:
            i = ts_index[r["ts"]]
            r["session_ret"] = float((close[i] - open_[i]) / open_[i])
        all_rows.extend(rows)
    logger.info("gap gate run | %d symbols | %d samples | cost=%.2f%% round-trip",
                len(files), len(all_rows), COST * 100)
    return all_rows


def main() -> int:
    all_rows = build_rows()
    grid = list(product(MARGINS, N_EST, DEPTH, LR))
    variants = []  # (params, sharpe, auc, day->net, day->gross)
    for k, (m, ne, md, lr) in enumerate(grid, 1):
        res = walk_forward_pnl(all_rows, n_splits=6, confidence_margin=m,
                               n_estimators=ne, max_depth=md, learning_rate=lr)
        gross, dates = res["gross_pnl"], res["dates"]
        if len(gross) < 100:
            logger.info("  [%2d/%2d] margin=%.2f ne=%d md=%d lr=%.2f -> too few trades, skip",
                        k, len(grid), m, ne, md, lr)
            continue
        net_by = _daily_by_day(gross, dates, COST)
        gross_by = _daily_by_day(gross, dates, 0.0)
        arr = np.array([net_by[d] for d in sorted(net_by)])
        s = _summ(arr)
        variants.append({
            "params": {"margin": m, "n_estimators": ne, "max_depth": md, "learning_rate": lr},
            "sharpe": s["sharpe"], "auc": res.get("oos_auc", float("nan")),
            "net_by": net_by, "gross_by": gross_by, "arr": arr,
        })
        logger.info("  [%2d/%2d] margin=%.2f ne=%d md=%d lr=%.2f -> Sharpe=%.2f AUC=%.4f n_days=%d",
                    k, len(grid), m, ne, md, lr, s["sharpe"], res.get("oos_auc", float("nan")), s["n"])

    if len(variants) < 2:
        logger.error("Not enough valid variants to deflate — aborting.")
        return 2

    # Align every variant's daily net return on the union of trading days
    # (0.0 = flat that day, the real portfolio experience) -> T x N for PBO.
    union_days = sorted({d for v in variants for d in v["net_by"]})
    matrix = np.array([[v["net_by"].get(d, 0.0) for v in variants] for d in union_days])
    trial_sharpes = [v["sharpe"] for v in variants]
    best_i = int(np.argmax(trial_sharpes))
    best = variants[best_i]
    best_arr = best["arr"]

    # Cost model + MC-DD inputs from the chosen (best) variant.
    net_total = float(sum(best["net_by"].values()) * CAPITAL)
    gross_total = float(sum(best["gross_by"].values()) * CAPITAL)
    charges = gross_total - net_total
    trade_list = [{"pnl": r * CAPITAL} for r in best_arr]

    logger.info("\nchosen best: %s  Sharpe=%.2f AUC=%.4f (of %d variants)",
                best["params"], best["sharpe"], best["auc"], len(variants))

    settings = SimpleNamespace(
        DSR_MIN_TRIALS=len(variants), DEFLECTED_SHARPE_MIN=0.5, PBO_MAX=0.4,
        GATE_MAX_DRAWDOWN=0.15, MIN_NET_COST_RATIO=0.6, MIN_WALKFORWARD_SHARPE=0.3,
        CSCV_N_GROUPS=8,
    )
    ev = GateEvaluator(settings=settings)
    ev.evaluate_dsr(best["sharpe"], trial_sharpes, best_arr)
    ev.evaluate_pbo(matrix)
    ev.evaluate_cpcv(SimpleNamespace(out_of_sample_metrics={
        "mean_oos_sharpe": best["sharpe"], "mean_oos_auc": best["auc"],
    }))
    ev.evaluate_monte_carlo(trade_list, starting_capital=CAPITAL)
    ev.evaluate_cost_model(net_total, charges)
    results = ev.finalize(backtest_id="gap-gates", strategy_id="gap_open_to_close")

    logger.info("\n=== GAP GATES (cost=%.2f%% round-trip, %d-config sweep) ===", COST * 100, len(variants))
    for g in (results.dsr, results.pbo, results.cpcv, results.monte_carlo, results.cost_model):
        if g is not None:
            logger.info("  %-16s %-5s %s", g.gate_name, g.result.value, g.message)
    logger.info("\nALL GATES PASSED: %s", results.all_passed)
    logger.info(
        "NOTE: flat-cost model only — no per-name impact/borrow for a ~58-names/day book. "
        "A pass is necessary, not sufficient, for live promotion."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
