"""Sector rotation — relative strength between NSE sector indices (a genuine
cross-sectional question at the SECTOR level rather than single-stock level,
so it's structurally different from research_cross_sectional.py's stock-level
factors: sector indices average out idiosyncratic single-name noise).

Hypothesis: sectors with strong recent relative performance vs the broad
market continue to outperform over the following weeks (sector-level
momentum/rotation, distinct from the single-stock 12-1 momentum already
rejected — sector aggregates are far less noisy).

Same walk-forward + shuffle-null + cost-aware discipline as
research_cross_sectional.py, applied to an 11-sector cross-section instead of
individual stocks.

Run:  python scripts/research_sector_rotation.py
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("sector_rotation")

# label -> token (NSE AMXIDX sector indices)
SECTORS = {
    "NIFTY_IT": "99926008", "NIFTY_REALTY": "99926018", "NIFTY_ENERGY": "99926020",
    "NIFTY_FMCG": "99926021", "NIFTY_PHARMA": "99926023", "NIFTY_PSU_BANK": "99926025",
    "NIFTY_AUTO": "99926029", "NIFTY_METAL": "99926030", "NIFTY_MEDIA": "99926031",
    "NIFTY_COMMODITIES": "99926035", "NIFTY_FIN_SERVICE": "99926037",
}
FORMATION = 21   # trailing 1-month relative strength
HORIZON = 10


def fetch(days_back: int = 2000):
    import dataclasses
    from trading_platform.config import load_settings
    from trading_platform.data.angel_one_history import AngelOneHistoricalDataProvider
    from trading_platform.data.angel_one_instruments import AngelOneInstrumentMasterProvider
    from trading_platform.domain.enums import Exchange

    s = load_settings()
    master = AngelOneInstrumentMasterProvider(s).load_cached()
    provider = AngelOneHistoricalDataProvider(s)
    to_dt = datetime.now(); from_dt = to_dt - timedelta(days=days_back)
    template = next(iter(master.instruments.values()))

    out = {}
    for label, token in SECTORS.items():
        inst = dataclasses.replace(template, token=token, exchange=Exchange("NSE"), symbol=label)
        try:
            bars = provider.get_candles(inst, from_dt, to_dt, "ONE_DAY")
        except Exception as exc:
            logger.warning("  %s: fetch failed: %s", label, exc); continue
        if len(bars) < 200:
            logger.warning("  %s: only %d bars", label, len(bars)); continue
        out[label] = {b.timestamp.date(): b.close for b in bars}
        logger.info("  %-20s %d bars (%s..%s)", label, len(bars), min(out[label]), max(out[label]))
    return out


def _zscore_rows(a: np.ndarray) -> np.ndarray:
    mu = np.nanmean(a, axis=1, keepdims=True)
    sd = np.nanstd(a, axis=1, keepdims=True)
    sd = np.where(sd < 1e-9, 1.0, sd)
    return (a - mu) / sd


def main() -> int:
    logger.info("Fetching NSE sector index history...")
    series = fetch()
    if len(series) < 5:
        logger.error("Too few sectors fetched (%d).", len(series)); return 2
    labels = sorted(series)
    common = sorted(set.intersection(*[set(series[l]) for l in labels]))
    close = np.array([[series[l][d] for l in labels] for d in common])
    T, N = close.shape
    logger.info("Panel: %d sectors, %d common days (%s..%s)", N, T, common[0], common[-1])

    rel_strength = np.full_like(close, np.nan)
    for t in range(FORMATION, T):
        rel_strength[t] = close[t] / close[t - FORMATION] - 1.0
    feat = _zscore_rows(rel_strength)

    fwd = np.full_like(close, np.nan)
    fwd[:-HORIZON] = close[HORIZON:] / close[:-HORIZON] - 1.0
    fwd_rel = fwd - np.nanmean(fwd, axis=1, keepdims=True)

    per_date = []
    for t in range(FORMATION, T - HORIZON):
        row = feat[t]
        valid = np.isfinite(row) & np.isfinite(fwd_rel[t])
        if valid.sum() < 5:
            continue
        per_date.append((row[valid], fwd_rel[t][valid]))

    ics = [np.corrcoef(f, r)[0, 1] for f, r in per_date if f.std() > 0]
    pooled_ic = float(np.nanmean(ics)) if ics else 0.0
    logger.info("\n" + "=" * 66)
    logger.info("SECTOR ROTATION | formation=%dd horizon=%dd | pooled IC=%+.4f", FORMATION, HORIZON, pooled_ic)

    def ls_returns(cost, key_fn=None):
        rets = []
        for i, (f, r) in enumerate(per_date):
            key = key_fn(i, f) if key_fn else f
            k = max(1, len(key) // 3)   # top/bottom third of 11 sectors
            idx = np.argsort(key)
            rets.append(r[idx[-k:]].mean() - r[idx[:k]].mean() - 2 * cost)
        return np.array(rets)

    any_trade = False
    for cost in (0.0, 0.0010, 0.0020):
        raw = ls_returns(cost)
        per = raw[::HORIZON]
        periods_per_year = 250 / HORIZON
        sharpe = float(per.mean() / per.std() * np.sqrt(periods_per_year)) if len(per) > 3 and per.std() > 0 else 0.0
        positive = bool(per.mean() > 0) if len(per) else False
        tag = "PROFITABLE" if positive else "loses"
        logger.info("  cost=%.2f%%  ann=%+.1f%%  Sharpe=%.2f  (%d periods)  -> %s",
                    cost * 100, per.mean() * periods_per_year * 100 if len(per) else 0, sharpe, len(per), tag)
        if cost >= 0.0010 and positive and sharpe > 0.5:
            any_trade = True

    # Permutation test at the highest tested cost.
    rng = np.random.default_rng(0)
    observed = ls_returns(0.0020)
    observed_sharpe = (observed[::HORIZON].mean() / observed[::HORIZON].std() * np.sqrt(250 / HORIZON)
                        if observed[::HORIZON].std() > 0 else 0.0)
    hits = 0
    n_perm = 2000
    for _ in range(n_perm):
        shuffled = ls_returns(0.0020, key_fn=lambda i, f: rng.permutation(len(f)))
        per = shuffled[::HORIZON]
        sd = per.std()
        sh = float(per.mean() / sd * np.sqrt(250 / HORIZON)) if sd > 0 else 0.0
        if sh >= observed_sharpe:
            hits += 1
    perm_p = hits / n_perm
    logger.info("  shuffle p-value: %.4f", perm_p)

    edge = any_trade and perm_p < 0.05 and abs(pooled_ic) > 0.02
    logger.info("=" * 66)
    logger.info("VERDICT: %s", "sector rotation edge survives — worth building" if edge else
                f"no tradeable edge (IC={pooled_ic:+.4f}, shuffle p={perm_p:.4f})")
    return 0 if edge else 1


if __name__ == "__main__":
    raise SystemExit(main())
