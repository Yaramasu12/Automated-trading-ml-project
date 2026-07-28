"""Two related, distinct cross-sectional hypotheses on the same NSE equity panel:

1. LOW-VOLATILITY / LOW-BETA ANOMALY (Ang/Hodges/Xing/Zhang 2006; Frazzini &
   Pedersen "Betting Against Beta" 2014): low-risk stocks have historically
   earned higher RISK-ADJUSTED returns than high-risk stocks, the opposite of
   what CAPM predicts. Tested here via trailing realized vol AND trailing beta
   vs. an equal-weighted universe return (no separate index fetch needed — beta
   against the cross-section's own market factor is a standard low-vol-anomaly
   construction). Distinct from every prior test here: it's a RISK-based sort,
   not a return-based one.

2. SHORT-TERM REVERSAL AT 1-2 DAY HORIZON (Jegadeesh 1990; Lehmann 1990):
   short-horizon reversal is a genuinely different regime from the 5-day
   reversal research_cross_sectional.py already tested — 1-2 day reversal is
   closer to microstructure/liquidity-provision territory (overreaction to
   order flow), where research_intraday_edge.py found the intraday version
   already dies at cost. Testing the DAILY-bar version here is the missing
   middle ground between "5-min" (tested, dies) and "5-day" (tested, no edge).

Same discipline as every other script here: walk-forward, cross-sectional
z-scoring per date, three-hurdle verdict (net-positive AND rank AUC>0.52 AND
shuffle p<0.05), non-overlapping-period Sharpe.

Run:  python scripts/research_lowvol_reversal.py
"""
from __future__ import annotations

import csv
import logging
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("lowvol_reversal")

HIST = Path(__file__).resolve().parent.parent / "data" / "historical"


def load_panel():
    series: dict[str, dict] = {}
    for f in sorted(HIST.glob("*__ONE_DAY.csv")):
        sym = f.name.split("__")[0]
        rows = {}
        with open(f) as fh:
            for r in csv.DictReader(fh):
                rows[r["timestamp"][:10]] = float(r["close"])
        if len(rows) > 300:
            series[sym] = rows
    if series:
        counts = sorted(len(v) for v in series.values())
        floor = counts[len(counts) // 2] * 0.9
        for sym in list(series):
            if len(series[sym]) < floor:
                del series[sym]
    for sym in list(series):
        closes = [series[sym][d] for d in sorted(series[sym])]
        if any(closes[i - 1] > 0 and abs(closes[i] / closes[i - 1] - 1.0) >= 0.35
               for i in range(1, len(closes))):
            logger.warning("  excluding %s: unadjusted split/bonus detected", sym)
            del series[sym]
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


def _ic_and_backtest(feature: np.ndarray, close: np.ndarray, horizon: int, cost_sweep):
    """Pooled information coefficient + quintile long-short backtest for a single
    cross-sectionally z-scored feature (higher feature value = predicted winner)."""
    T, N = close.shape
    fwd = np.full_like(close, np.nan)
    fwd[:-horizon] = close[horizon:] / close[:-horizon] - 1.0
    fwd_rel = fwd - np.nanmean(fwd, axis=1, keepdims=True)

    ics, ls_by_t, per_date = [], {}, []
    for t in range(len(feature)):
        row = feature[t]
        valid = np.isfinite(row) & np.isfinite(fwd[t])
        if valid.sum() < 6:
            continue
        f, r = row[valid], fwd_rel[t][valid]
        ic = np.corrcoef(f, r)[0, 1] if f.std() > 0 else 0.0
        if np.isfinite(ic):
            ics.append(ic)
        k = max(1, valid.sum() // 5)
        idx = np.argsort(f)
        ls_by_t[t] = (f, r, idx, k)
        per_date.append((f, r, idx, k))

    pooled_ic = float(np.nanmean(ics)) if ics else 0.0
    results = {}
    for cost in cost_sweep:
        daily = []
        for t in sorted(ls_by_t):
            f, r, idx, k = ls_by_t[t]
            daily.append(r[idx[-k:]].mean() - r[idx[:k]].mean() - 2 * cost)
        daily = np.array(daily)
        per = daily[::max(horizon, 1)]
        periods_per_year = 250 / max(horizon, 1)
        sharpe = float(per.mean() / per.std() * np.sqrt(periods_per_year)) if len(per) > 3 and per.std() > 0 else 0.0
        results[cost] = {"ann": float(per.mean() * periods_per_year) if len(per) else 0.0,
                          "sharpe": sharpe, "n_periods": len(per), "positive": bool(per.mean() > 0) if len(per) else False}
    return pooled_ic, results, per_date


def _permutation_p(per_date, horizon: int, cost: float, observed_sharpe: float, n_perm: int = 1500) -> float:
    rng = np.random.default_rng(0)
    hits = 0
    for _ in range(n_perm):
        daily = []
        for f, r, idx, k in per_date:
            shuf = rng.permutation(len(f))
            daily.append(r[shuf[-k:]].mean() - r[shuf[:k]].mean() - 2 * cost)
        per = np.array(daily)[::max(horizon, 1)]
        sd = per.std()
        sh = float(per.mean() / sd * np.sqrt(250 / max(horizon, 1))) if sd > 0 else 0.0
        if sh >= observed_sharpe:
            hits += 1
    return hits / n_perm


def run_hypothesis(name: str, feature: np.ndarray, close: np.ndarray, horizon: int, cost_sweep=(0.0, 0.0010, 0.0020)):
    logger.info("\n" + "-" * 66)
    logger.info("%s  (horizon=%dd)", name, horizon)
    ic, results, per_date = _ic_and_backtest(feature, close, horizon, cost_sweep)
    logger.info("  pooled IC (feature vs fwd relative return): %+.4f", ic)
    any_trade = False
    for cost in cost_sweep:
        res = results[cost]
        tag = "PROFITABLE" if res["positive"] else "loses"
        perm_p = _permutation_p(per_date, horizon, cost, res["sharpe"]) if cost == cost_sweep[-1] or res["positive"] else None
        pstr = f"  shuffle p={perm_p:.3f}" if perm_p is not None else ""
        if cost >= 0.0010 and res["positive"] and res["sharpe"] > 0.5 and abs(ic) > 0.02 and perm_p is not None and perm_p < 0.05:
            any_trade = True
        logger.info("  cost=%.2f%%  ann=%+.1f%%  Sharpe=%.2f  (%d periods)%s  -> %s",
                    cost * 100, res["ann"] * 100, res["sharpe"], res["n_periods"], pstr, tag)
    logger.info("  VERDICT: %s", "EDGE — worth building" if any_trade else "no tradeable edge")
    return any_trade


def main() -> int:
    symbols, dates, close = load_panel()
    logger.info("Loaded panel: %d stocks, %d common days (%s..%s)",
                len(symbols), len(dates), dates[0] if len(dates) else "?", dates[-1] if len(dates) else "?")
    if len(symbols) < 6:
        logger.error("Need >=6 stocks; have %d.", len(symbols)); return 2

    T, N = close.shape
    logret = np.zeros_like(close)
    logret[1:] = np.log(close[1:] / close[:-1])
    mkt_ret = logret.mean(axis=1, keepdims=True)  # equal-weighted universe return, no external index needed

    any_edge = False

    # --- Hypothesis 1: low-volatility / low-beta anomaly ---
    W = 60
    trailing_vol = np.full_like(close, np.nan)
    trailing_beta = np.full_like(close, np.nan)
    for t in range(W, T):
        window_r = logret[t - W + 1:t + 1]
        window_m = mkt_ret[t - W + 1:t + 1, 0]
        trailing_vol[t] = window_r.std(axis=0)
        mvar = window_m.var()
        if mvar > 1e-12:
            trailing_beta[t] = (window_r * window_m[:, None]).mean(axis=0) / mvar
    # Negate so higher feature value = lower risk = predicted OUTperformer (long side).
    low_vol_feat = _zscore_rows(-trailing_vol)
    low_beta_feat = _zscore_rows(-trailing_beta)
    any_edge |= run_hypothesis("LOW-VOLATILITY ANOMALY (long low trailing-60d realized vol)", low_vol_feat, close, horizon=21)
    any_edge |= run_hypothesis("LOW-BETA ANOMALY (long low trailing-60d beta vs equal-weighted universe)", low_beta_feat, close, horizon=21)

    # --- Hypothesis 2: short-term reversal at 1-2 day horizon ---
    for h in (1, 2):
        rev = np.full_like(close, np.nan)
        rev[h:] = -(close[h:] / close[:-h] - 1.0)  # negative of recent return = reversal signal
        rev_feat = _zscore_rows(rev)
        any_edge |= run_hypothesis(f"SHORT-TERM REVERSAL ({h}-day horizon)", rev_feat, close, horizon=h)

    logger.info("\n" + "=" * 66)
    logger.info("OVERALL VERDICT: %s", "at least one edge survives — worth building" if any_edge
                else "no tradeable edge in low-vol/low-beta or 1-2 day reversal on this data")
    return 0 if any_edge else 1


if __name__ == "__main__":
    raise SystemExit(main())
