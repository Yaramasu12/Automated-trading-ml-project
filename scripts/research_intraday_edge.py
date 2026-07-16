"""Intraday 5-minute edge research — does short-horizon microstructure predict?

The honesty discipline of this repo (see CLAUDE.md) says a feature set earns
deployment ONLY if it beats a statistical noise threshold out-of-sample. Daily-bar
TA already failed that test (AUC ~0.50 on 2500 days). This script runs the SAME
rigorous walk-forward test on genuinely INTRADAY features — the untested avenue.

Design against the ways intraday research fools you:
  * No lookahead: features at bar i use only bars [day_open .. i]; the label uses
    bars [i+1 .. i+H].
  * No overnight leakage: features (VWAP, opening range, bar-of-day) reset every
    day, and a label is only formed when i and i+H are the SAME trading day.
  * Walk-forward, chronological, pooled across symbols with a global time sort —
    the test window is always strictly after the train window.
  * Verdict is OOS ROC-AUC vs 0.5 + max(0.02, 2·SE_null); anything at or below is
    reported as NO EDGE, not massaged into looking positive.

Run:  python scripts/research_intraday_edge.py --days 40 --horizon 3 --save-cache
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

# Ensure the repo root is importable when run as `python scripts/...`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("intraday_research")

REPO = Path(__file__).resolve().parent.parent
CACHE = REPO / "data" / "intraday_research"
CACHE.mkdir(parents=True, exist_ok=True)

# Liquid, high-turnover names + indices — where any microstructure edge is most
# likely to exist and be tradeable.
BASKET = [
    "NIFTY", "BANKNIFTY", "RELIANCE", "TCS", "INFY", "HDFCBANK",
    "ICICIBANK", "SBIN", "TATAMOTORS", "AXISBANK", "MARUTI", "ITC",
]

FEATURE_NAMES = [
    "ret_1", "ret_3", "ret_6", "ret_12",      # short-horizon momentum
    "vwap_dev",                                  # deviation from intraday VWAP (reversion)
    "or_pos",                                    # position within the 30-min opening range
    "bar_of_day",                                # time-of-day (0=open .. 1=close)
    "rvol_6", "rvol_ratio",                      # realized vol + short/long vol ratio
    "rel_volume", "vol_mom",                     # volume vs recent avg + volume trend
    "body_ratio",                                # candle body / range
    "dist_hi_12", "dist_lo_12",                  # distance from recent high/low
    "run_len",                                    # signed consecutive up/down bar count
]


# ── Data ──────────────────────────────────────────────────────────────────────

def fetch_intraday(symbol: str, days: int, save_cache: bool) -> list[dict]:
    """5-min bars as list of {ts, open, high, low, close, volume}. Cached per day-count."""
    cache_file = CACHE / f"{symbol}__FIVE_MINUTE_{days}d.npz"
    if cache_file.exists():
        z = np.load(cache_file, allow_pickle=True)
        return list(z["bars"])
    from trading_platform.config import load_settings
    from trading_platform.data.angel_one_history import AngelOneHistoricalDataProvider
    from trading_platform.data.angel_one_instruments import AngelOneInstrumentMasterProvider

    settings = load_settings()
    master = AngelOneInstrumentMasterProvider(settings).load_cached()
    hist = AngelOneHistoricalDataProvider(settings)
    # Index spot candles need the AMXIDX tokens, not the WebSocket index tokens.
    index_tokens = {
        "NIFTY": ("99926000", "NSE"), "BANKNIFTY": ("99926009", "NSE"),
        "FINNIFTY": ("99926037", "NSE"), "MIDCPNIFTY": ("99926074", "NSE"),
    }
    import dataclasses
    from trading_platform.domain.enums import Exchange
    inst = master.get(symbol)
    if symbol in index_tokens:
        tok, exch = index_tokens[symbol]
        inst = dataclasses.replace(inst, token=tok, exchange=Exchange(exch))
    bars_raw = hist.get_candles(
        inst, datetime.now() - timedelta(days=days), datetime.now(), interval="FIVE_MINUTE"
    )
    bars = [
        {"ts": b.timestamp, "open": b.open, "high": b.high,
         "low": b.low, "close": b.close, "volume": b.volume}
        for b in bars_raw
    ]
    if save_cache and bars:
        np.savez_compressed(cache_file, bars=np.array(bars, dtype=object))
    return bars


# ── Feature engineering (per trading day, no lookahead, no overnight leak) ─────

def _day_features_labels(day_bars: list[dict], horizon: int) -> tuple[list[list[float]], list[int]]:
    """Build (X rows, y labels) for one trading day. Row i uses bars[..i]; label
    is sign of the close-to-close return over the next `horizon` bars, same day."""
    n = len(day_bars)
    if n < 15 + horizon:
        return [], []
    close = np.array([b["close"] for b in day_bars], float)
    high = np.array([b["high"] for b in day_bars], float)
    low = np.array([b["low"] for b in day_bars], float)
    openp = np.array([b["open"] for b in day_bars], float)
    vol = np.array([max(b["volume"], 0) for b in day_bars], float)
    logret = np.zeros(n)
    logret[1:] = np.log(close[1:] / np.maximum(close[:-1], 1e-9))
    # Cumulative VWAP up to and including each bar (uses only past+current → ok).
    typical = (high + low + close) / 3.0
    cum_pv = np.cumsum(typical * vol)
    cum_v = np.cumsum(vol)
    vwap = np.where(cum_v > 0, cum_pv / np.maximum(cum_v, 1e-9), close)
    # 30-min opening range = first 6 bars.
    or_hi, or_lo = high[:6].max(), low[:6].min()
    or_rng = max(or_hi - or_lo, 1e-9)

    X: list[list[float]] = []
    y: list[int] = []
    # i ranges where we have >=12 bars of history AND a full horizon ahead same day.
    for i in range(12, n - horizon):
        def r(k):  # k-bar return ending at i
            return float(np.log(close[i] / max(close[i - k], 1e-9)))
        rvol6 = float(np.std(logret[i - 5:i + 1]))
        rvol36 = float(np.std(logret[max(0, i - 35):i + 1])) or 1e-9
        recent_vol = vol[i - 11:i + 1]
        avg_vol = float(recent_vol.mean()) or 1e-9
        hi12, lo12 = high[i - 11:i + 1].max(), low[i - 11:i + 1].min()
        # signed run length
        run = 0
        for j in range(i, 0, -1):
            s = np.sign(logret[j])
            if s == 0:
                break
            if run == 0 or np.sign(run) == s:
                run += int(s)
            else:
                break
        feats = [
            r(1), r(3), r(6), r(12),
            float((close[i] - vwap[i]) / max(close[i], 1e-9)),
            float((close[i] - or_lo) / or_rng),
            float(i / max(n - 1, 1)),
            rvol6, float(rvol6 / rvol36),
            float(vol[i] / avg_vol), float(recent_vol[-3:].mean() / avg_vol),
            float((close[i] - openp[i]) / max(high[i] - low[i], 1e-9)),
            float((close[i] - hi12) / max(close[i], 1e-9)),
            float((close[i] - lo12) / max(close[i], 1e-9)),
            float(run),
        ]
        fwd = float(np.log(close[i + horizon] / max(close[i], 1e-9)))
        if not np.isfinite(fwd) or not all(np.isfinite(v) for v in feats):
            continue
        X.append(feats)
        y.append(1 if fwd > 0 else 0)
    return X, y


def build_dataset(symbol: str, bars: list[dict], horizon: int):
    """Group bars by trading day; concatenate per-day (X,y) preserving time order.
    Returns (X, y, timestamps) with timestamps for the global chronological sort."""
    by_day: dict = {}
    for b in bars:
        by_day.setdefault(b["ts"].date(), []).append(b)
    X_all, y_all, ts_all = [], [], []
    for day in sorted(by_day):
        day_bars = sorted(by_day[day], key=lambda x: x["ts"])
        X, y = _day_features_labels(day_bars, horizon)
        if not X:
            continue
        # timestamp of the feature bar (i) — reconstructed for ordering only
        base = len(X_all)
        X_all.extend(X)
        y_all.extend(y)
        # approximate: use day ordinal + within-day index for a stable global sort
        ts_all.extend([(day, k) for k in range(len(X))])
    return X_all, y_all, ts_all


# ── Walk-forward validation ───────────────────────────────────────────────────

def walk_forward(X, y, n_splits=5):
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.metrics import roc_auc_score

    X = np.asarray(X, float)
    y = np.asarray(y, int)
    n = len(y)
    if n < 400:
        return None
    fold = n // (n_splits + 1)
    oos_true, oos_proba = [], []
    for s in range(1, n_splits + 1):
        tr_end = fold * s
        te_end = fold * (s + 1)
        Xtr, ytr = X[:tr_end], y[:tr_end]
        Xte, yte = X[tr_end:te_end], y[tr_end:te_end]
        if len(np.unique(ytr)) < 2 or len(np.unique(yte)) < 2:
            continue
        clf = GradientBoostingClassifier(
            n_estimators=120, max_depth=3, learning_rate=0.05, subsample=0.8,
            random_state=42,
        )
        clf.fit(Xtr, ytr)
        p = clf.predict_proba(Xte)[:, 1]
        oos_true.extend(yte.tolist())
        oos_proba.extend(p.tolist())
    if len(oos_true) < 200:
        return None
    yt = np.asarray(oos_true)
    auc = float(roc_auc_score(yt, oos_proba))
    n1, n0 = int(yt.sum()), int(len(yt) - yt.sum())
    se_null = float(np.sqrt((n1 + n0 + 1) / (12.0 * max(n1, 1) * max(n0, 1))))
    threshold = 0.5 + max(0.02, 2 * se_null)
    return {
        "oos_auc": auc, "oos_samples": len(yt), "pos_rate": n1 / len(yt),
        "se_null": se_null, "threshold": threshold, "passed": auc >= threshold,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=40, help="calendar days of 5-min history")
    ap.add_argument("--horizon", type=int, default=3, help="forward bars for the label (3=15min)")
    ap.add_argument("--symbols", default="", help="comma-separated; default = liquid basket")
    ap.add_argument("--save-cache", action="store_true", help="cache fetched bars to disk")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or BASKET
    logger.info("Intraday edge research | %d symbols | %dd 5-min | horizon=%d bars (%dmin)",
                len(symbols), args.days, args.horizon, args.horizon * 5)

    pooled_X, pooled_y, pooled_ts = [], [], []
    per_symbol = {}
    for sym in symbols:
        try:
            bars = fetch_intraday(sym, args.days, args.save_cache)
        except Exception as e:
            logger.warning("  %s: fetch failed — %s", sym, str(e)[:80])
            continue
        if len(bars) < 200:
            logger.warning("  %s: only %d bars, skipping", sym, len(bars))
            continue
        X, y, ts = build_dataset(sym, bars, args.horizon)
        if len(y) < 300:
            logger.info("  %-11s %d bars -> %d samples (too few for solo WF)", sym, len(bars), len(y))
        else:
            res = walk_forward(X, y)
            per_symbol[sym] = res
            if res:
                logger.info("  %-11s %d bars -> %d samples | solo OOS AUC=%.4f %s",
                            sym, len(bars), len(y), res["oos_auc"],
                            "PASS" if res["passed"] else "")
        # global chronological pooling
        for xi, yi, (day, k) in zip(X, y, ts):
            pooled_X.append(xi)
            pooled_y.append(yi)
            pooled_ts.append((day, sym, k))

    if len(pooled_y) < 500:
        logger.error("Not enough pooled samples (%d) — widen --days or basket.", len(pooled_y))
        return 2

    # Global time sort so walk-forward never trains on the future.
    order = sorted(range(len(pooled_ts)), key=lambda idx: pooled_ts[idx])
    Xp = [pooled_X[i] for i in order]
    yp = [pooled_y[i] for i in order]

    logger.info("\n" + "=" * 62)
    logger.info("POOLED WALK-FORWARD  (%d samples, %d features)", len(yp), len(FEATURE_NAMES))
    res = walk_forward(Xp, yp, n_splits=6)
    if res is None:
        logger.error("Validation could not run (insufficient class balance).")
        return 2
    logger.info("  out-of-sample AUC   : %.4f", res["oos_auc"])
    logger.info("  noise threshold     : %.4f  (0.5 + 2*SE, SE=%.4f)", res["threshold"], res["se_null"])
    logger.info("  OOS samples         : %d  (%.1f%% up)", res["oos_samples"], res["pos_rate"] * 100)
    logger.info("  VERDICT             : %s",
                "REAL EDGE — worth pursuing" if res["passed"]
                else "NO validated edge (AUC <= noise) — intraday TA also empty")
    logger.info("=" * 62)

    # Feature importance on the full set (diagnostic only — not a validation).
    try:
        from sklearn.ensemble import GradientBoostingClassifier
        clf = GradientBoostingClassifier(n_estimators=120, max_depth=3, learning_rate=0.05,
                                         subsample=0.8, random_state=42)
        clf.fit(np.asarray(Xp, float), np.asarray(yp, int))
        imp = sorted(zip(FEATURE_NAMES, clf.feature_importances_), key=lambda t: -t[1])
        logger.info("\n  top features (in-sample importance, diagnostic):")
        for name, w in imp[:6]:
            logger.info("    %-12s %.3f", name, w)
    except Exception:
        pass
    return 0 if res["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
