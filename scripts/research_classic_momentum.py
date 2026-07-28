"""Classic 12-1 cross-sectional equity momentum — the most replicated anomaly in
finance (Jegadeesh & Titman 1993; Asness/Moskowitz/Pedersen "Value and Momentum
Everywhere" 2013 found it in >40 countries and asset classes). Genuinely
different from what research_cross_sectional.py already tested: that script
used 20/60-day formation at a 5-day holding horizon (short-horizon reversal
territory); this uses the textbook 12-month formation with a 1-month skip,
held for 1 month, which is a structurally different signal (skipping the most
recent month specifically excludes short-term reversal, which is a separate,
often opposite-signed effect).

Discipline (same bar as every other research script here):
  * Formation return = close[t-21] / close[t-273] - 1 (12mo return, skip most
    recent month — features at date t use only data up to t).
  * Cross-sectionally z-scored within each date; label = beats the day's
    cross-sectional median forward-21-day return (market beta differenced out).
  * Walk-forward, chronological, GradientBoostingClassifier.
  * Long-short backtest (top vs bottom quintile), permutation-null test
    (shuffle ranks within date), and a cost sweep — same three-hurdle bar as
    research_cross_sectional.py: net-positive AND rank AUC > 0.52 AND shuffle
    p < 0.05.

Known limitation, stated up front: the cached history here is ~2 years, so a
12-month-formation/1-month-hold test only has a handful of non-overlapping
rebalance periods. That is disclosed in the verdict regardless of outcome —
a strategy this data-starved cannot be "proven" here, only screened for an
obviously absent or an implausibly large (and therefore suspect) effect.

Run:  python scripts/research_classic_momentum.py
"""
from __future__ import annotations

import csv
import logging
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("classic_momentum")

HIST = Path(__file__).resolve().parent.parent / "data" / "historical"

FORMATION = 252     # ~12 months of trading days
SKIP = 21            # ~1 month skip (excludes short-term reversal)
HOLD = 21             # ~1 month holding period


def load_panel():
    series: dict[str, dict] = {}
    for f in sorted(HIST.glob("*__ONE_DAY.csv")):
        sym = f.name.split("__")[0]
        rows = {}
        with open(f) as fh:
            for r in csv.DictReader(fh):
                d = r["timestamp"][:10]
                rows[d] = float(r["close"])
        if len(rows) > FORMATION + SKIP + HOLD + 60:
            series[sym] = rows
    symbols = sorted(series)
    common = set.intersection(*[set(series[s]) for s in symbols]) if symbols else set()
    dates = sorted(common)
    close = np.array([[series[s][d] for s in symbols] for d in dates])
    return symbols, np.array(dates), close


def _zscore_rows(a: np.ndarray) -> np.ndarray:
    mu = np.nanmean(a, axis=1, keepdims=True)
    sd = np.nanstd(a, axis=1, keepdims=True)
    sd = np.where(sd < 1e-9, 1.0, sd)
    return (a - mu) / sd


def build_features(close: np.ndarray):
    T, N = close.shape
    mom_12_1 = np.full_like(close, np.nan)
    for t in range(FORMATION + SKIP, T):
        mom_12_1[t] = close[t - SKIP] / close[t - FORMATION - SKIP] - 1.0
    # A short-term reversal control feature so the classifier can differentiate
    # "recently spiked" from genuine 12-month trend continuation.
    rev5 = np.full_like(close, np.nan)
    for t in range(5, T):
        rev5[t] = -(close[t] / close[t - 5] - 1.0)

    feats = {"mom_12_1": _zscore_rows(mom_12_1), "rev5": _zscore_rows(rev5)}
    names = list(feats)

    fwd = np.full_like(close, np.nan)
    fwd[:-HOLD] = close[HOLD:] / close[:-HOLD] - 1.0
    fwd_rel = fwd - np.nanmean(fwd, axis=1, keepdims=True)

    X, y, meta, rel = [], [], [], []
    for t in range(FORMATION + SKIP, T - HOLD):
        med = np.nanmedian(fwd[t])
        for n in range(N):
            row = [feats[k][t, n] for k in names]
            if any(not np.isfinite(v) for v in row) or not np.isfinite(fwd[t, n]):
                continue
            X.append(row)
            y.append(1 if fwd[t, n] > med else 0)
            meta.append((t, n))
            rel.append(float(fwd_rel[t, n]))
    return names, X, y, meta, rel


def walk_forward(X, y, meta, rel, cost, n_splits=4):
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.metrics import roc_auc_score

    X = np.asarray(X, float); y = np.asarray(y, int); rel = np.asarray(rel, float)
    tdx = np.array([m[0] for m in meta])
    order = np.argsort(tdx, kind="stable")
    X, y, rel, tdx = X[order], y[order], rel[order], tdx[order]
    uniq_t = np.unique(tdx)
    if len(uniq_t) < 40:
        return None
    fold = len(uniq_t) // (n_splits + 1)
    if fold < 1:
        return None
    oos_true, oos_p = [], []
    ls_by_date: dict[int, float] = {}
    per_date: list = []
    for s in range(1, n_splits + 1):
        tr_t = set(uniq_t[: fold * s]); te_t = set(uniq_t[fold * s: fold * (s + 1)])
        tr = np.array([t in tr_t for t in tdx]); te = np.array([t in te_t for t in tdx])
        if len(np.unique(y[tr])) < 2 or te.sum() == 0:
            continue
        clf = GradientBoostingClassifier(n_estimators=100, max_depth=2,
                                         learning_rate=0.05, subsample=0.8, random_state=42)
        clf.fit(X[tr], y[tr])
        p = clf.predict_proba(X[te])[:, 1]
        oos_true.extend(y[te].tolist()); oos_p.extend(p.tolist())
        for t in np.unique(tdx[te]):
            mask = te & (tdx == t)
            probs = clf.predict_proba(X[mask])[:, 1]
            rr = rel[mask]
            k = max(1, len(probs) // 5)
            idx = np.argsort(probs)
            longs, shorts = idx[-k:], idx[:k]
            ls_by_date[int(t)] = rr[longs].mean() - rr[shorts].mean() - 2 * cost
            per_date.append((probs, rr))
    if len(oos_true) < 200 or not ls_by_date:
        return None
    auc = float(roc_auc_score(oos_true, oos_p))
    # ls_by_date has one entry per TRADING DAY in the test window, each holding a
    # forward-HOLD-day return — adjacent entries overlap almost completely. Slicing
    # every HOLD-th one collapses this to genuinely non-overlapping rebalance
    # periods; skipping this step (as an earlier version of this script did)
    # inflated the apparent rebalance count 16x and the Sharpe along with it —
    # the same overlapping-window bug already caught once in research_gap_backtest.py.
    all_vals = [v for _, v in sorted(ls_by_date.items())]
    per = np.array(all_vals[::HOLD])
    per_date = per_date[::HOLD]
    periods_per_year = 250 / HOLD
    mean = float(per.mean()); sd = float(per.std())
    sharpe = float(mean / sd * np.sqrt(periods_per_year)) if sd > 0 else 0.0
    ann = mean * periods_per_year
    perm_p = _permutation_p(per_date, cost, sharpe)
    return {"oos_auc": auc, "oos_n": len(oos_true), "ls_ann": ann, "ls_sharpe": sharpe,
            "ls_periods": len(per), "ls_mean_per_reb": mean, "positive": mean > 0, "perm_p": perm_p}


def _ls_sharpe_for_keys(per_date, cost: float, keys) -> float:
    daily = []
    for (_probs, rr), key in zip(per_date, keys):
        k = max(1, len(key) // 5)
        idx = np.argsort(key)
        daily.append(rr[idx[-k:]].mean() - rr[idx[:k]].mean() - 2 * cost)
    per = np.array(daily)
    sd = per.std()
    return float(per.mean() / sd * np.sqrt(250 / HOLD)) if sd > 0 else 0.0


def _permutation_p(per_date, cost: float, observed: float, n_perm: int = 2000) -> float:
    if not per_date:
        return 1.0
    rng = np.random.default_rng(0)
    hits = 0
    for _ in range(n_perm):
        keys = [rng.permutation(len(p)) for p, _ in per_date]
        if _ls_sharpe_for_keys(per_date, cost, keys) >= observed:
            hits += 1
    return hits / n_perm


def main() -> int:
    symbols, dates, close = load_panel()
    logger.info("Classic 12-1 momentum | %d stocks | %d common days (%s..%s)",
                len(symbols), len(dates), dates[0] if len(dates) else "?", dates[-1] if len(dates) else "?")
    if len(symbols) < 6:
        logger.error("Need >=6 stocks; have %d.", len(symbols)); return 2

    names, X, y, meta, rel = build_features(close)
    n_dates = len(set(m[0] for m in meta))
    n_rebalances_available = n_dates // HOLD
    logger.info("  built %d (date,stock) samples across %d distinct dates (~%d non-overlapping "
                "%d-day rebalance periods available)", len(X), n_dates, n_rebalances_available, HOLD)
    if n_rebalances_available < 8:
        logger.warning("  CAVEAT: only ~%d independent rebalance periods — a 12-month-formation "
                        "test needs years of history to be conclusive. Treat any result below as a "
                        "screen for an obviously-absent or implausibly-large effect, not proof.",
                        n_rebalances_available)

    logger.info("\n" + "=" * 66)
    logger.info("WALK-FORWARD 12-1 MOMENTUM  + long-short backtest (monthly rebalance)")
    any_trade = False
    for cost in (0.0, 0.0010, 0.0020):
        res = walk_forward(X, y, meta, rel, cost)
        if res is None:
            logger.info("  (insufficient data for walk-forward)"); break
        if cost == 0.0:
            logger.info("  OOS rank AUC (beats-peers) : %.4f  (0.50 = no signal)", res["oos_auc"])
            gross_bps = res["ls_mean_per_reb"] * 1e4
            logger.info("  gross alpha per rebalance  : %+.2f bps", gross_bps)
        tag = "PROFITABLE" if res["positive"] else "loses"
        if (cost >= 0.0010 and res["positive"] and res["ls_sharpe"] > 0.5
                and res["oos_auc"] > 0.52 and res["perm_p"] < 0.05):
            any_trade = True
        logger.info("  cost=%.2f%%/leg  ann=%+.1f%%  Sharpe=%.2f  (%d rebalances)  shuffle p=%.3f  -> %s",
                    cost * 100, res["ls_ann"] * 100, res["ls_sharpe"], res["ls_periods"], res["perm_p"], tag)
    logger.info("=" * 66)
    logger.info("VERDICT: %s", (
        "12-1 MOMENTUM survives costs AND beats a shuffled null — worth building "
        "(but see the sample-size caveat above)"
        if any_trade else
        "no tradeable 12-1 momentum edge on this data "
        "(needs net-positive AND rank AUC > 0.52 AND shuffle p < 0.05)"))
    return 0 if any_trade else 1


if __name__ == "__main__":
    raise SystemExit(main())
