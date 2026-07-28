"""Cost-aware backtest of the gap-dominated open-to-close signal.

research_gap_calendar_volume.py found a real, shuffle-null-significant signal
(OOS AUC 0.5676, p=0.0000, dominated by gap_pct). An AUC edge is necessary but
NOT sufficient — this repo's own intraday research already showed a weaker
signal (AUC 0.529) that this script's sibling (research_intraday_backtest.py)
was built specifically to cost-test. Same discipline here, same question: after
realistic costs, does trading this signal make money?

  * Walk-forward, chronological — the model only ever trains on the past.
  * Enter at today's open (direction from the model's predicted probability,
    only when it clears a confidence margin — else stay flat, no forced trades).
  * Exit at today's close. One round trip per trade.
  * A round-trip cost is subtracted from EVERY trade, swept across a realistic
    range. Entering AT THE OPEN specifically (not mid-session) typically means
    wider spreads than a random intraday entry — costs are swept higher than
    the intraday script's range to reflect that.
  * Reports GROSS vs NET so the cost sensitivity is visible, not hidden.

Verdict: tradeable only if NET return is positive with a sane Sharpe at a
realistic open-auction cost (>= 0.05% round trip; opens are less liquid than
mid-session for equities).

Run:  python scripts/research_gap_backtest.py
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("gap_backtest")

from research_gap_calendar_volume import HIST, _load_symbol, build_features  # noqa: E402


def walk_forward_pnl(rows: list[dict], n_splits: int = 6, confidence_margin: float = 0.05):
    """Predict direction walk-forward; simulate open->close P&L net of swept costs.

    Returns per-trade (symbol, day) results AND a proper per-CALENDAR-DAY
    portfolio series (equal-weighted mean across whatever symbols traded that
    day). The per-trade array is diagnostic only — Sharpe/annualized return
    must be computed on the daily portfolio series, not on pooled per-trade
    rows, or dozens of same-day cross-sectional bets get treated as
    independent time steps and the risk looks fake-smooth.
    """
    from sklearn.ensemble import GradientBoostingClassifier

    rows = sorted(rows, key=lambda r: r["ts"])
    X = np.asarray([r["X"] for r in rows], float)
    y = np.asarray([r["y"] for r in rows], int)
    session_ret = np.asarray([r["session_ret"] for r in rows], float)
    n = len(rows)
    fold = n // (n_splits + 1)

    oos_proba = np.full(n, np.nan)
    for s in range(1, n_splits + 1):
        tr_end, te_end = fold * s, fold * (s + 1)
        Xtr, ytr = X[:tr_end], y[:tr_end]
        Xte = X[tr_end:te_end]
        if len(np.unique(ytr)) < 2:
            continue
        clf = GradientBoostingClassifier(n_estimators=120, max_depth=3, learning_rate=0.05,
                                         subsample=0.8, random_state=42)
        clf.fit(Xtr, ytr)
        oos_proba[tr_end:te_end] = clf.predict_proba(Xte)[:, 1]

    mask = ~np.isnan(oos_proba)
    proba = oos_proba[mask]
    ret = session_ret[mask]
    dates = [rows[i]["ts"].date() for i in range(n) if mask[i]]
    symbols = [rows[i]["symbol"] for i in range(n) if mask[i]]

    # direction: long if predicted P(up) clears the margin above 0.5, short if
    # it clears the margin below 0.5, else flat — no forced trades.
    direction = np.zeros(len(proba))
    direction[proba > 0.5 + confidence_margin] = 1.0
    direction[proba < 0.5 - confidence_margin] = -1.0
    traded = direction != 0.0
    n_trades = int(traded.sum())

    gross_pnl = direction[traded] * ret[traded]
    traded_dates = [d for d, t in zip(dates, traded) if t]
    traded_symbols = [s for s, t in zip(symbols, traded) if t]
    return {
        "n_oos": len(proba), "n_trades": n_trades,
        "trade_rate": n_trades / max(len(proba), 1),
        "gross_pnl": gross_pnl, "dates": traded_dates, "symbols": traded_symbols,
    }


def daily_portfolio_series(gross_pnl: np.ndarray, dates: list, cost_pct: float) -> np.ndarray:
    """Equal-weighted mean return across all symbols traded on each calendar
    day — one number per trading day, which is what a real account actually
    experiences, not one number per (symbol, day) pair."""
    net = gross_pnl - cost_pct
    by_day: dict = {}
    for d, r in zip(dates, net):
        by_day.setdefault(d, []).append(r)
    return np.array([float(np.mean(v)) for _, v in sorted(by_day.items())])


def summarize(daily_ret: np.ndarray) -> dict:
    n = len(daily_ret)
    if n == 0:
        return {"ann_return": 0.0, "sharpe": 0.0, "n": 0}
    mean, std = float(np.mean(daily_ret)), float(np.std(daily_ret))
    ann_return = (1.0 + mean) ** 252 - 1.0 if mean > -1.0 else -1.0
    sharpe = (mean / std * np.sqrt(252)) if std > 0 else 0.0
    cum = float(np.prod(1.0 + daily_ret) - 1.0)
    running_max = np.maximum.accumulate(np.cumprod(1.0 + daily_ret))
    max_dd = float(np.min((np.cumprod(1.0 + daily_ret) - running_max) / running_max)) if n else 0.0
    return {"ann_return": ann_return, "sharpe": sharpe, "n": n,
            "mean_daily": mean, "cum_return": cum, "max_drawdown": max_dd}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--confidence-margin", type=float, default=0.05,
                    help="only trade when |P(up)-0.5| exceeds this")
    ap.add_argument("--eval-symbol", default="",
                    help="evaluate P&L on just this one symbol's signal (model still "
                         "trains on the full pooled universe) — tests the edge WITHOUT "
                         "the cross-symbol pooling/diversification effect, since same-day "
                         "trades across symbols are meaningfully correlated (~0.67 same-"
                         "direction rate, not the 0.5 true independence would give)")
    args = ap.parse_args()

    files = sorted(HIST.glob("*__ONE_DAY.csv"))
    all_rows: list[dict] = []
    for f in files:
        symbol = f.name.split("__")[0]
        data = _load_symbol(f)
        if data is None:
            continue
        rows = build_features(data, symbol)
        # Re-derive the raw session return alongside the binary label — build_features
        # only kept the sign; the backtest needs the actual magnitude for P&L.
        close, open_ = data["close"], data["open"]
        ts_index = {t: i for i, t in enumerate(data["ts"])}
        for r in rows:
            i = ts_index[r["ts"]]
            r["session_ret"] = float((close[i] - open_[i]) / open_[i])
        all_rows.extend(rows)

    logger.info("Gap signal cost-aware backtest | %d symbols | %d samples", len(files), len(all_rows))
    result = walk_forward_pnl(all_rows, confidence_margin=args.confidence_margin)
    logger.info("  OOS predictions       : %d", result["n_oos"])
    logger.info("  trades taken          : %d (%.1f%% of OOS days — margin=%.2f)",
                result["n_trades"], result["trade_rate"] * 100, args.confidence_margin)

    gross = result["gross_pnl"]
    dates = result["dates"]
    syms = result["symbols"]
    if args.eval_symbol:
        target = args.eval_symbol.strip().upper()
        keep = [i for i, s in enumerate(syms) if s.upper() == target]
        logger.info("  eval-symbol filter     : %s only (%d of %d trades) — trained on full "
                    "pooled universe, evaluated on this symbol's signal alone, no cross-"
                    "symbol diversification in the P&L", target, len(keep), len(gross))
        gross = gross[keep]
        dates = [dates[i] for i in keep]
    if len(gross) < 100:
        logger.error("Too few trades to evaluate — lower --confidence-margin or drop --eval-symbol.")
        return 2
    n_days = len(set(dates))
    logger.info("  spread across          : %d distinct trading days (%.1f trades/day avg)",
                n_days, result["n_trades"] / max(n_days, 1))

    logger.info("\n" + "=" * 70)
    logger.info("NET P&L AT SWEPT OPEN-AUCTION COSTS — daily portfolio series (round trip)")
    any_survives = False
    for cost_pct in (0.0, 0.0005, 0.0010, 0.0015, 0.0020):
        daily = daily_portfolio_series(gross, dates, cost_pct)
        s = summarize(daily)
        survives = s["sharpe"] > 0 and s["ann_return"] > 0
        any_survives = any_survives or (survives and cost_pct > 0)
        logger.info(
            "  cost=%.2f%%  mean/day=%.4f%%  ann_return=%.1f%%  Sharpe=%.2f  "
            "cum_return=%.1f%%  max_dd=%.1f%%  -> %s",
            cost_pct * 100, s["mean_daily"] * 100, s["ann_return"] * 100,
            s["sharpe"], s["cum_return"] * 100, s["max_drawdown"] * 100,
            "survives" if survives else "dies",
        )
    logger.info("=" * 70)
    logger.info("VERDICT: %s", (
        "gap signal SURVIVES realistic open-auction costs — genuinely tradeable"
        if any_survives else
        "gap signal is statistically real (AUC test passed) but DIES at realistic "
        "costs — an execution-cost artifact, not a tradeable edge"
    ))
    return 0 if any_survives else 1


if __name__ == "__main__":
    raise SystemExit(main())
