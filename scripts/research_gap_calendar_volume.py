"""Gap / calendar / volume-conditioned edge research — a genuinely new question.

Every prior test in this repo used CLOSE-TO-CLOSE returns (does the stock go up
tomorrow?) and failed. This asks a structurally different question: given how
today OPENED — the overnight gap, the calendar position, and yesterday's
volume-confirmed momentum — does today's OPEN-TO-CLOSE session have a
predictable direction? That isolates the intraday session conditional on the
open, which nothing else here has tested.

Three sub-hypotheses, deliberately combined into ONE model rather than tested
separately — testing three things and reporting whichever one passes is exactly
the multiple-comparisons trap this repo's other scripts guard against (see the
cross-sectional script's shuffle-null gate). One combined test, one verdict;
feature importance shows which sub-hypothesis (if any) is actually doing the
work.

  * Gap:      gap_pct (open vs prior close), and whether the PRIOR day's gap
              continued or filled (lagged, safe).
  * Calendar: day-of-week, turn-of-month indicators.
  * Volume:   momentum over the prior 5 days interacted with relative volume —
              structurally different from the plain momentum already tested and
              rejected (AUC ~0.50): this asks whether volume-CONFIRMED momentum
              behaves differently, not whether momentum alone predicts.

Honesty discipline (same as every other script here):
  * Every feature at day t uses only data through yesterday's close + today's
    open — nothing from today's session leaks in.
  * Label = sign of (close[t] - open[t]) — same-day, no overnight leakage.
  * Walk-forward, chronological, pooled across the full cached universe with a
    global time sort.
  * Verdict = OOS AUC vs a closed-form noise threshold AND a label-shuffle
    permutation test (1000 shuffles) — both must clear, matching the strictest
    bar already used in this repo (cross-sectional's shuffle p < 0.05).

Run:  python scripts/research_gap_calendar_volume.py
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("gap_calendar_research")

HIST = Path(__file__).resolve().parent.parent / "data" / "historical"

FEATURE_NAMES = [
    "gap_pct", "prior_gap_continued",
    "dow_mon", "dow_fri", "is_month_start", "is_month_end",
    "mom5", "rel_volume", "mom5_x_relvol",
]


def _load_symbol(path: Path) -> dict | None:
    ts, o, h, l, c, v = [], [], [], [], [], []
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                ts.append(datetime.fromisoformat(row["timestamp"]))
                o.append(float(row["open"]))
                h.append(float(row["high"]))
                l.append(float(row["low"]))
                c.append(float(row["close"]))
                v.append(float(row["volume"]))
    except Exception:
        return None
    if len(ts) < 120:
        return None
    order = sorted(range(len(ts)), key=lambda i: ts[i])
    return {
        "ts": [ts[i] for i in order],
        "open": np.array([o[i] for i in order], float),
        "close": np.array([c[i] for i in order], float),
        "volume": np.array([v[i] for i in order], float),
    }


def build_features(data: dict, symbol: str) -> list[dict]:
    """One row per day t, features known at today's open, label = today's session."""
    close = data["close"]
    open_ = data["open"]
    vol = data["volume"]
    ts = data["ts"]

    rows: list[dict] = []
    for t in range(6, len(ts)):
        prior_close = close[t - 1]
        if prior_close <= 0 or open_[t] <= 0:
            continue
        gap_pct = (open_[t] - prior_close) / prior_close

        # Prior day's gap: did the prior session continue the gap direction or fill it?
        prior_prior_close = close[t - 2]
        prior_gap = (open_[t - 1] - prior_prior_close) / prior_prior_close if prior_prior_close > 0 else 0.0
        prior_session_ret = (close[t - 1] - open_[t - 1]) / open_[t - 1] if open_[t - 1] > 0 else 0.0
        prior_gap_continued = 1.0 if np.sign(prior_gap) == np.sign(prior_session_ret) and prior_gap != 0 else 0.0

        day_dt = ts[t]
        dow = day_dt.weekday()  # 0=Mon .. 4=Fri
        day_of_month = day_dt.day
        days_in_month = _days_in_month(day_dt.year, day_dt.month)
        is_month_start = 1.0 if day_of_month <= 3 else 0.0
        is_month_end = 1.0 if day_of_month >= days_in_month - 2 else 0.0

        mom5 = float(np.log(close[t - 1] / max(close[t - 6], 1e-9)))
        avg_vol20 = float(np.mean(vol[max(0, t - 21):t - 1])) or 1e-9
        rel_volume = float(vol[t - 1] / avg_vol20)
        mom5_x_relvol = mom5 * rel_volume

        session_ret = (close[t] - open_[t]) / open_[t]
        if not np.isfinite(session_ret):
            continue

        feats = [
            gap_pct, prior_gap_continued,
            1.0 if dow == 0 else 0.0, 1.0 if dow == 4 else 0.0,
            is_month_start, is_month_end,
            mom5, rel_volume, mom5_x_relvol,
        ]
        if not all(np.isfinite(v) for v in feats):
            continue
        rows.append({
            "symbol": symbol, "ts": day_dt,
            "X": feats, "y": 1 if session_ret > 0 else 0,
        })
    return rows


def _days_in_month(year: int, month: int) -> int:
    import calendar
    return calendar.monthrange(year, month)[1]


def walk_forward_with_shuffle(X: np.ndarray, y: np.ndarray, n_splits: int = 6, n_shuffles: int = 1000) -> dict | None:
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.metrics import roc_auc_score

    n = len(y)
    if n < 1000:
        return None
    fold = n // (n_splits + 1)
    oos_true, oos_proba = [], []
    for s in range(1, n_splits + 1):
        tr_end, te_end = fold * s, fold * (s + 1)
        Xtr, ytr = X[:tr_end], y[:tr_end]
        Xte, yte = X[tr_end:te_end], y[tr_end:te_end]
        if len(np.unique(ytr)) < 2 or len(np.unique(yte)) < 2:
            continue
        clf = GradientBoostingClassifier(n_estimators=120, max_depth=3, learning_rate=0.05,
                                         subsample=0.8, random_state=42)
        clf.fit(Xtr, ytr)
        p = clf.predict_proba(Xte)[:, 1]
        oos_true.extend(yte.tolist())
        oos_proba.extend(p.tolist())
    if len(oos_true) < 400:
        return None
    yt = np.asarray(oos_true)
    proba = np.asarray(oos_proba)
    auc = float(roc_auc_score(yt, proba))
    n1, n0 = int(yt.sum()), int(len(yt) - yt.sum())
    se_null = float(np.sqrt((n1 + n0 + 1) / (12.0 * max(n1, 1) * max(n0, 1))))
    threshold = 0.5 + max(0.02, 2 * se_null)

    # Label-shuffle permutation test: destroy any real relationship between
    # features and outcome while holding the OOS prediction distribution fixed,
    # see how often a shuffled AUC matches or beats what we actually observed.
    rng = np.random.default_rng(0)
    hits = 0
    for _ in range(n_shuffles):
        yt_shuffled = rng.permutation(yt)
        try:
            shuf_auc = roc_auc_score(yt_shuffled, proba)
        except ValueError:
            continue
        if shuf_auc >= auc:
            hits += 1
    perm_p = hits / n_shuffles

    return {
        "oos_auc": auc, "oos_samples": len(yt), "pos_rate": n1 / len(yt),
        "se_null": se_null, "threshold": threshold,
        "beats_threshold": auc >= threshold,
        "perm_p": perm_p, "beats_shuffle": perm_p < 0.05,
        "passed": auc >= threshold and perm_p < 0.05,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--shuffles", type=int, default=1000)
    args = ap.parse_args()

    files = sorted(HIST.glob("*__ONE_DAY.csv"))
    logger.info("Gap/calendar/volume research | %d cached symbols", len(files))

    all_rows: list[dict] = []
    used, skipped = 0, 0
    for f in files:
        symbol = f.name.split("__")[0]
        df = _load_symbol(f)
        if df is None:
            skipped += 1
            continue
        rows = build_features(df, symbol)
        if len(rows) < 100:
            skipped += 1
            continue
        all_rows.extend(rows)
        used += 1

    logger.info("  %d symbols used, %d skipped (insufficient history) | %d total samples",
                used, skipped, len(all_rows))
    if len(all_rows) < 1000:
        logger.error("Not enough pooled samples.")
        return 2

    # Global chronological sort — walk-forward must never train on the future.
    all_rows.sort(key=lambda r: r["ts"])
    X = np.asarray([r["X"] for r in all_rows], float)
    y = np.asarray([r["y"] for r in all_rows], int)

    logger.info("\n" + "=" * 66)
    logger.info("POOLED WALK-FORWARD — gap + calendar + volume-conditioned momentum")
    logger.info("  label: same-day OPEN-to-CLOSE direction (not close-to-close)")
    res = walk_forward_with_shuffle(X, y, n_shuffles=args.shuffles)
    if res is None:
        logger.error("Validation could not run (insufficient samples/class balance).")
        return 2
    logger.info("  out-of-sample AUC     : %.4f", res["oos_auc"])
    logger.info("  noise threshold       : %.4f  (0.5 + 2*SE, SE=%.4f)", res["threshold"], res["se_null"])
    logger.info("  OOS samples           : %d  (%.1f%% up)", res["oos_samples"], res["pos_rate"] * 100)
    logger.info("  shuffle-null p-value  : %.4f  (%d permutations, need < 0.05)", res["perm_p"], args.shuffles)
    logger.info("  VERDICT               : %s",
                "REAL EDGE — beats noise threshold AND shuffle-null, worth pursuing further"
                if res["passed"] else
                "NO validated edge — gap/calendar/volume-momentum, combined, is not predictive here")
    logger.info("=" * 66)

    try:
        from sklearn.ensemble import GradientBoostingClassifier
        clf = GradientBoostingClassifier(n_estimators=120, max_depth=3, learning_rate=0.05,
                                         subsample=0.8, random_state=42)
        clf.fit(X, y)
        imp = sorted(zip(FEATURE_NAMES, clf.feature_importances_), key=lambda t: -t[1])
        logger.info("\n  feature importance (in-sample, diagnostic — shows which sub-hypothesis dominates):")
        for name, w in imp:
            logger.info("    %-20s %.3f", name, w)
    except Exception:
        pass
    return 0 if res["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
