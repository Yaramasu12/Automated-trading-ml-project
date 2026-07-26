"""Cross-sectional (relative-value) edge research — genuinely different from price TA.

Time-series TA (does THIS stock go up?) showed no tradeable edge. This tests a
structurally different question: does a stock out/under-perform its PEERS? That
removes the unpredictable market-wide move and isolates cross-sectional dispersion,
where momentum / reversal / low-vol factors sometimes live. And crucially it
rebalances daily/weekly, not every 5 minutes — so costs are a fraction of what
killed the intraday signal.

Discipline (same honesty bar as the other scripts):
  * Features at date t use only data up to t; label is the FORWARD relative return.
  * Cross-sectional features are z-scored within each date's cross-section only.
  * Label = 1 if the stock's forward N-day return beats the cross-sectional MEDIAN
    that day (relative outperformance), so market beta is differenced out.
  * Walk-forward, chronological. Report OOS rank accuracy AND a long-short backtest
    (long top quintile, short bottom quintile) NET of a swept round-trip cost.

Verdict: tradeable only if the long-short is net-positive with a sane Sharpe at a
realistic daily-rebalance cost.

Run:  python scripts/research_cross_sectional.py --horizon 5
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("xs_research")

HIST = Path(__file__).resolve().parent.parent / "data" / "historical"


def load_panel() -> tuple[list[str], np.ndarray, np.ndarray, np.ndarray]:
    """Load all cached daily CSVs, align on common dates.
    Returns (symbols, dates, close[T,N], volume[T,N])."""
    import csv
    from datetime import datetime
    series: dict[str, dict] = {}
    for f in sorted(HIST.glob("*__ONE_DAY.csv")):
        sym = f.name.split("__")[0]
        rows = {}
        with open(f) as fh:
            for r in csv.DictReader(fh):
                d = r["timestamp"][:10]
                rows[d] = (float(r["close"]), float(r["volume"] or 0))
        if len(rows) > 200:
            series[sym] = rows
    symbols = sorted(series)
    # common date set
    common = set.intersection(*[set(series[s]) for s in symbols])
    dates = sorted(common)
    close = np.array([[series[s][d][0] for s in symbols] for d in dates])
    vol = np.array([[series[s][d][1] for s in symbols] for d in dates])
    return symbols, np.array(dates), close, vol


def _zscore_rows(a: np.ndarray) -> np.ndarray:
    """Cross-sectional z-score within each row (date). NaN-safe."""
    mu = np.nanmean(a, axis=1, keepdims=True)
    sd = np.nanstd(a, axis=1, keepdims=True)
    sd = np.where(sd < 1e-9, 1.0, sd)
    return (a - mu) / sd


def build_xs_features(close: np.ndarray, vol: np.ndarray, horizon: int):
    """Cross-sectional features per (date, stock). Returns X[list of rows],
    y[relative-outperf label], meta[(t_index, n_index)] for walk-forward + backtest,
    and fwd_rel[forward relative return] for the long-short P&L."""
    T, N = close.shape
    logret = np.zeros_like(close)
    logret[1:] = np.log(close[1:] / np.maximum(close[:-1], 1e-9))

    def trailing_return(k):
        r = np.full_like(close, np.nan)
        r[k:] = close[k:] / np.maximum(close[:-k], 1e-9) - 1.0
        return r

    mom20 = trailing_return(20)
    mom60 = trailing_return(60)
    rev5 = -trailing_return(5)                       # short-term reversal
    vol20 = np.full_like(close, np.nan)
    for t in range(20, T):
        vol20[t] = logret[t - 19:t + 1].std(axis=0)
    # distance from trailing 60-day high
    dist_hi = np.full_like(close, np.nan)
    for t in range(60, T):
        hi = close[t - 59:t + 1].max(axis=0)
        dist_hi[t] = close[t] / np.maximum(hi, 1e-9) - 1.0
    relvol = np.full_like(close, np.nan)
    for t in range(20, T):
        av = vol[t - 19:t + 1].mean(axis=0)
        relvol[t] = vol[t] / np.maximum(av, 1e-9)

    feats = {
        "mom20": _zscore_rows(mom20), "mom60": _zscore_rows(mom60),
        "rev5": _zscore_rows(rev5), "vol20": _zscore_rows(vol20),
        "dist_hi": _zscore_rows(dist_hi), "relvol": _zscore_rows(relvol),
    }
    names = list(feats)

    # Forward N-day return, cross-sectionally demeaned = relative return.
    fwd = np.full_like(close, np.nan)
    fwd[:-horizon] = close[horizon:] / np.maximum(close[:-horizon], 1e-9) - 1.0
    fwd_rel = fwd - np.nanmean(fwd, axis=1, keepdims=True)

    X, y, meta, rel = [], [], [], []
    for t in range(60, T - horizon):
        med = np.nanmedian(fwd[t])
        for n in range(N):
            row = [feats[k][t, n] for k in names]
            if any(not np.isfinite(v) for v in row) or not np.isfinite(fwd[t, n]):
                continue
            X.append(row)
            y.append(1 if fwd[t, n] > med else 0)     # beats peers
            meta.append((t, n))
            rel.append(float(fwd_rel[t, n]))
    return names, X, y, meta, rel


def walk_forward_xs(X, y, meta, rel, horizon, cost, n_splits=6):
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.metrics import roc_auc_score

    X = np.asarray(X, float); y = np.asarray(y, int); rel = np.asarray(rel, float)
    tdx = np.array([m[0] for m in meta])
    order = np.argsort(tdx, kind="stable")
    X, y, rel, tdx = X[order], y[order], rel[order], tdx[order]
    uniq_t = np.unique(tdx)
    if len(uniq_t) < 200:
        return None
    fold = len(uniq_t) // (n_splits + 1)
    oos_true, oos_p = [], []
    ls_daily: dict[int, list] = {}     # t -> list of (rel_return, weight)
    per_date: list = []                # (probs, rel) per OOS date, for the permutation test
    for s in range(1, n_splits + 1):
        tr_t = set(uniq_t[: fold * s]); te_t = set(uniq_t[fold * s: fold * (s + 1)])
        tr = np.array([t in tr_t for t in tdx]); te = np.array([t in te_t for t in tdx])
        if len(np.unique(y[tr])) < 2 or te.sum() == 0:
            continue
        clf = GradientBoostingClassifier(n_estimators=150, max_depth=3,
                                         learning_rate=0.05, subsample=0.8, random_state=42)
        clf.fit(X[tr], y[tr])
        p = clf.predict_proba(X[te])[:, 1]
        oos_true.extend(y[te].tolist()); oos_p.extend(p.tolist())
        # long-short: per test date, rank by prob, long top / short bottom quintile
        for t in np.unique(tdx[te]):
            mask = te & (tdx == t)
            probs = clf.predict_proba(X[mask])[:, 1]
            rr = rel[mask]
            k = max(1, len(probs) // 5)
            idx = np.argsort(probs)
            longs = idx[-k:]; shorts = idx[:k]
            # dollar-neutral long-short return, minus turnover cost each rebalance
            ls_ret = rr[longs].mean() - rr[shorts].mean() - 2 * cost
            ls_daily.setdefault(int(t), []).append(ls_ret)
            per_date.append((probs, rr))
    if len(oos_true) < 500 or not ls_daily:
        return None
    auc = float(roc_auc_score(oos_true, oos_p))
    # collapse to one return per rebalance date, then to non-overlapping periods
    daily = np.array([np.mean(v) for _, v in sorted(ls_daily.items())])
    # non-overlapping: sample every `horizon` days to avoid overlap-inflated Sharpe
    per = daily[::horizon]
    periods_per_year = 250 / horizon
    mean = float(per.mean()); sd = float(per.std())
    sharpe = float(mean / sd * np.sqrt(periods_per_year)) if sd > 0 else 0.0
    ann = mean * periods_per_year
    perm_p = _permutation_p(per_date, horizon, cost, sharpe)
    return {"oos_auc": auc, "oos_n": len(oos_true), "ls_ann": ann, "ls_sharpe": sharpe,
            "ls_periods": len(per), "ls_mean_per_reb": mean, "positive": mean > 0,
            "perm_p": perm_p}


def _ls_sharpe(per_date, horizon: int, cost: float, keys) -> float:
    """Long-short Sharpe when each date's names are ranked by the supplied key."""
    daily = []
    for (_probs, rr), key in zip(per_date, keys):
        k = max(1, len(key) // 5)
        idx = np.argsort(key)
        daily.append(rr[idx[-k:]].mean() - rr[idx[:k]].mean() - 2 * cost)
    per = np.array(daily)[::horizon]
    sd = per.std()
    return float(per.mean() / sd * np.sqrt(250 / horizon)) if sd > 0 else 0.0


def _permutation_p(per_date, horizon: int, cost: float, observed: float, n_perm: int = 2000) -> float:
    """Fraction of random rankings that match or beat the observed Sharpe.

    A long-short P&L is not evidence on its own. With a small universe the top
    quintile is two or three names and there are only a few dozen non-overlapping
    periods, so an ordinary run of luck clears "positive after costs" often. This
    shuffles the predicted ranks within each date -- destroying any signal while
    holding the dates, universe, returns and cost model fixed -- so the spread of
    shuffled Sharpes is exactly the null the strategy has to beat.
    """
    if not per_date:
        return 1.0
    rng = np.random.default_rng(0)
    hits = 0
    for _ in range(n_perm):
        keys = [rng.permutation(len(p)) for p, _ in per_date]
        if _ls_sharpe(per_date, horizon, cost, keys) >= observed:
            hits += 1
    return hits / n_perm


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--horizon", type=int, default=5, help="forward holding days (5=weekly)")
    args = ap.parse_args()

    symbols, dates, close, vol = load_panel()
    logger.info("Cross-sectional research | %d stocks | %d common days (%s..%s) | horizon=%dd",
                len(symbols), len(dates), dates[0], dates[-1], args.horizon)
    if len(symbols) < 6:
        logger.error("Need >=6 stocks for a cross-section; have %d.", len(symbols)); return 2

    names, X, y, meta, rel = build_xs_features(close, vol, args.horizon)
    logger.info("  built %d (date,stock) samples, %d features: %s", len(X), len(names), names)

    logger.info("\n" + "=" * 64)
    logger.info("WALK-FORWARD CROSS-SECTIONAL  + long-short backtest")
    any_trade = False
    for cost in (0.0, 0.0010, 0.0020):    # 0, 10bps, 20bps round trip per leg per rebalance
        res = walk_forward_xs(X, y, meta, rel, args.horizon, cost)
        if res is None:
            logger.info("  (insufficient data for walk-forward)"); break
        if cost == 0.0:
            logger.info("  OOS rank AUC (beats-peers) : %.4f  (0.50 = no cross-sectional signal)", res["oos_auc"])
            # What the ranking is worth before costs, and therefore the most you can
            # afford to pay per leg. A strategy is only tradeable if this clears real
            # Indian equity costs -- brokerage + STT + exchange fees + spread/impact,
            # realistically 10-25 bps a leg on even liquid Nifty names.
            gross_bps = res["ls_mean_per_reb"] * 1e4
            logger.info("  gross alpha per rebalance  : %+.2f bps", gross_bps)
            logger.info("  break-even cost per leg    : %.2f bps  <-- must exceed real costs",
                        max(gross_bps / 2.0, 0.0))
            logger.info("  --- long-short (top vs bottom quintile), net of cost ---")
        tag = "PROFITABLE" if res["positive"] else "loses"
        # Three independent hurdles, all required. P&L alone is not evidence: with a
        # small universe the quintiles are 2-3 names over a few dozen periods, and a
        # run of luck clears "positive after costs" routinely. Ranking skill (AUC)
        # says the model actually orders the cross-section, and the permutation
        # p-value says the P&L is not what random ranking would have produced anyway.
        if (cost >= 0.0010 and res["positive"]
                and res["ls_sharpe"] > 0.5
                and res["oos_auc"] > 0.52
                and res["perm_p"] < 0.05):
            any_trade = True
        logger.info("  cost=%.2f%%/leg  ann=%+.1f%%  Sharpe=%.2f  (%d rebalances)  "
                    "shuffle p=%.3f  -> %s",
                    cost * 100, res["ls_ann"] * 100, res["ls_sharpe"],
                    res["ls_periods"], res["perm_p"], tag)
    logger.info("=" * 64)
    logger.info("VERDICT: %s", (
        "CROSS-SECTIONAL EDGE survives costs AND beats a shuffled null — worth building"
        if any_trade else
        "no tradeable cross-sectional edge in this universe "
        "(needs net-positive AND rank AUC > 0.52 AND shuffle p < 0.05)"))
    return 0 if any_trade else 1


if __name__ == "__main__":
    raise SystemExit(main())
