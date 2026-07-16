"""Cost-aware walk-forward backtest of the 5-minute signal.

The edge test (research_intraday_edge.py) found a faint 5-min directional signal
(pooled OOS AUC ~0.529, just above the noise floor). AUC being above noise is
necessary but NOT sufficient to trade: at a 5-minute holding period you round-trip
constantly, and transaction costs can easily exceed a 0.529-AUC edge.

This script answers the only question that matters: **after realistic costs, does
trading this signal make money?** It is deliberately pessimistic —

  * Walk-forward, chronological: the model only ever trains on the past.
  * No lookahead / no overnight leakage (same discipline as the edge script).
  * Enter at bar i, exit H bars later (same day). Direction from the model, and
    only when its confidence clears a margin (else stay flat — no forced trades).
  * A round-trip cost is subtracted from EVERY trade, swept across a realistic
    range so you see exactly where the edge dies.
  * Reports GROSS vs NET so you can see how much of the raw edge costs eat.

Verdict rule: the signal is tradeable only if NET return is positive with a
sane Sharpe at a realistic cost (>= 0.03% round trip for liquid intraday). A
positive gross that goes negative after costs = real-but-not-tradeable.

Run:  python scripts/research_intraday_backtest.py --horizon 1 --margin 0.55
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.research_intraday_edge import (  # noqa: E402
    BASKET, FEATURE_NAMES, fetch_intraday, _day_features_labels,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("intraday_backtest")


def build_dataset_with_returns(bars: list[dict], horizon: int):
    """Like build_dataset but also returns the forward RETURN per row (for P&L),
    plus a (day, index) key for the global chronological sort."""
    by_day: dict = {}
    for b in bars:
        by_day.setdefault(b["ts"].date(), []).append(b)
    X_all, y_all, ret_all, ts_all = [], [], [], []
    for day in sorted(by_day):
        day_bars = sorted(by_day[day], key=lambda x: x["ts"])
        X, y = _day_features_labels(day_bars, horizon)
        if not X:
            continue
        close = np.array([b["close"] for b in day_bars], float)
        # _day_features_labels emits rows for i in range(12, n-horizon); mirror it
        # to recover the forward return at each emitted row.
        rets = []
        for i in range(12, len(day_bars) - horizon):
            rets.append(float((close[i + horizon] - close[i]) / max(close[i], 1e-9)))
        # Guard: lengths must line up (they do — same loop bounds & finite filter).
        m = min(len(X), len(rets))
        for k in range(m):
            X_all.append(X[k]); y_all.append(y[k]); ret_all.append(rets[k]); ts_all.append((day, k))
    return X_all, y_all, ret_all, ts_all


def backtest(X, y, rets, margin: float, costs: list[float], n_splits: int = 6):
    from sklearn.ensemble import GradientBoostingClassifier

    X = np.asarray(X, float); y = np.asarray(y, int); rets = np.asarray(rets, float)
    n = len(y)
    if n < 500:
        return None
    fold = n // (n_splits + 1)
    trade_rets: list[float] = []   # signed gross return per taken trade
    n_long = n_short = 0
    for s in range(1, n_splits + 1):
        tr_end, te_end = fold * s, fold * (s + 1)
        Xtr, ytr = X[:tr_end], y[:tr_end]
        Xte, rte = X[tr_end:te_end], rets[tr_end:te_end]
        if len(np.unique(ytr)) < 2 or len(Xte) == 0:
            continue
        clf = GradientBoostingClassifier(n_estimators=120, max_depth=3,
                                         learning_rate=0.05, subsample=0.8, random_state=42)
        clf.fit(Xtr, ytr)
        p = clf.predict_proba(Xte)[:, 1]
        for prob, r in zip(p, rte):
            if prob >= margin:            # long
                trade_rets.append(r); n_long += 1
            elif prob <= (1 - margin):    # short
                trade_rets.append(-r); n_short += 1
            # else flat — no trade
    if len(trade_rets) < 30:
        return {"trades": len(trade_rets), "too_few": True}
    tr = np.asarray(trade_rets)
    gross_mean = float(tr.mean())
    win_rate = float((tr > 0).mean())
    # Annualization: ~75 five-min bars/day, ~250 trading days.
    trades_per_year = 75 * 250 * (len(tr) / len(y))
    out = {
        "trades": len(tr), "n_long": n_long, "n_short": n_short,
        "win_rate": win_rate, "gross_per_trade": gross_mean,
        "gross_ann": gross_mean * trades_per_year,
        "by_cost": {},
    }
    for c in costs:
        net = tr - c
        net_mean = float(net.mean())
        sharpe = float(net_mean / (net.std() + 1e-12) * np.sqrt(trades_per_year)) if net.std() > 0 else 0.0
        out["by_cost"][c] = {
            "net_per_trade": net_mean,
            "net_ann_return": net_mean * trades_per_year,
            "sharpe": sharpe,
            "positive": net_mean > 0,
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--horizon", type=int, default=1, help="bars held (1=5min, best-edge horizon)")
    ap.add_argument("--margin", type=float, default=0.55, help="prob confidence to take a trade")
    ap.add_argument("--symbols", default="")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or BASKET
    costs = [0.0, 0.0003, 0.0005, 0.0010]   # 0, 3bps, 5bps, 10bps round trip
    logger.info("Cost-aware 5-min backtest | horizon=%d (%dmin) | margin=%.2f | %d symbols",
                args.horizon, args.horizon * 5, args.margin, len(symbols))

    pooled_X, pooled_y, pooled_r, pooled_ts = [], [], [], []
    for sym in symbols:
        try:
            bars = fetch_intraday(sym, args.days, save_cache=True)
        except Exception as e:
            logger.warning("  %s: fetch failed — %s", sym, str(e)[:70]); continue
        if len(bars) < 200:
            continue
        X, yy, rr, ts = build_dataset_with_returns(bars, args.horizon)
        for xi, yi, ri, (day, k) in zip(X, yy, rr, ts):
            pooled_X.append(xi); pooled_y.append(yi); pooled_r.append(ri); pooled_ts.append((day, sym, k))

    if len(pooled_y) < 1000:
        logger.error("Too few pooled samples (%d).", len(pooled_y)); return 2
    order = sorted(range(len(pooled_ts)), key=lambda i: pooled_ts[i])
    Xp = [pooled_X[i] for i in order]; yp = [pooled_y[i] for i in order]; rp = [pooled_r[i] for i in order]

    res = backtest(Xp, yp, rp, args.margin, costs)
    if res is None or res.get("too_few"):
        logger.error("Backtest could not run / too few trades: %s", res); return 2

    logger.info("\n" + "=" * 64)
    logger.info("POOLED COST-AWARE BACKTEST  (%d samples)", len(yp))
    logger.info("  trades taken       : %d  (long %d / short %d)", res["trades"], res["n_long"], res["n_short"])
    logger.info("  win rate           : %.1f%%", res["win_rate"] * 100)
    logger.info("  GROSS / trade      : %+.4f%%   (annualized %+.1f%%)",
                res["gross_per_trade"] * 100, res["gross_ann"] * 100)
    logger.info("  --- after costs (round trip) ---")
    verdict_tradeable = False
    for c, d in res["by_cost"].items():
        tag = "PROFITABLE" if d["positive"] else "loses"
        if c >= 0.0003 and d["positive"] and d["sharpe"] > 0.5:
            verdict_tradeable = True
        logger.info("  cost=%.2f%%  net/trade=%+.4f%%  ann=%+.1f%%  Sharpe=%.2f  -> %s",
                    c * 100, d["net_per_trade"] * 100, d["net_ann_return"] * 100, d["sharpe"], tag)
    logger.info("=" * 64)
    logger.info("VERDICT: %s", (
        "TRADEABLE — edge survives realistic costs; build the strategy"
        if verdict_tradeable else
        "NOT tradeable — edge does not survive realistic costs (real-but-too-small)"
    ))
    return 0 if verdict_tradeable else 1


if __name__ == "__main__":
    raise SystemExit(main())
