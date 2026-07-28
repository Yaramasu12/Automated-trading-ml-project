"""Gold-silver ratio mean-reversion — classic commodity pairs trade. The
gold/silver price ratio is mean-reverting over multi-month horizons (both are
precious metals driven by overlapping macro factors — USD strength, real
rates — but with different industrial-demand components that pull the ratio
back after it overshoots). Tests whether a large ratio deviation from its own
trailing mean predicts subsequent convergence.

Uses the current front-month contract's own price history for each metal
(GOLD, SILVER) rather than a roll-stitched continuous series — MCX lists
contracts far enough in advance that the current near-month contract alone
gives ~2 years of history, and using a single contract avoids roll-jump
artifacts contaminating the ratio (a genuine advantage here, not just a
shortcut).

Discipline: z-score the log(gold/silver) ratio vs its own trailing 60-day
mean; label = forward N-day change in the log ratio (does it revert toward
the mean); walk-forward is not needed here (fixed rule, not a fitted model,
same as research_turn_of_month.py) — instead a permutation test shuffles which
days are "extreme deviation" days, and a cost-aware backtest trades the
convergence bet with non-overlapping holding periods.

Run:  python scripts/research_commodity_pairs.py
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("commodity_pairs")

WINDOW = 60
HORIZON = 10
Z_ENTRY = 1.0


def fetch_front_month(days_back: int = 730):
    from trading_platform.config import load_settings
    from trading_platform.data.angel_one_history import AngelOneHistoricalDataProvider
    from trading_platform.data.angel_one_instruments import AngelOneInstrumentMasterProvider

    s = load_settings()
    master = AngelOneInstrumentMasterProvider(s).load_cached()
    provider = AngelOneHistoricalDataProvider(s)
    to_dt = datetime.now(); from_dt = to_dt - timedelta(days=days_back)

    out = {}
    for commodity in ("GOLD", "SILVER"):
        contracts = sorted(
            (inst for inst in master.instruments.values()
             if inst.name == commodity and inst.symbol.endswith("FUT") and inst.expiry is not None),
            key=lambda i: i.expiry,
        )
        if not contracts:
            continue
        front = contracts[0]
        bars = provider.get_candles(front, from_dt, to_dt, "ONE_DAY")
        out[commodity] = {b.timestamp.date(): b.close for b in bars}
        logger.info("  %s front month %s: %d bars", commodity, front.symbol, len(bars))
    return out


def main() -> int:
    series = fetch_front_month()
    if "GOLD" not in series or "SILVER" not in series:
        logger.error("Missing GOLD or SILVER series."); return 2
    dates = sorted(set(series["GOLD"]) & set(series["SILVER"]))
    if len(dates) < 150:
        logger.error("Only %d overlapping days — insufficient.", len(dates)); return 2

    gold = np.array([series["GOLD"][d] for d in dates])
    silver = np.array([series["SILVER"][d] for d in dates])
    ratio = gold / silver
    log_ratio = np.log(ratio)

    T = len(dates)
    zscore = np.full(T, np.nan)
    for t in range(WINDOW, T):
        w = log_ratio[t - WINDOW:t]
        mu, sd = w.mean(), w.std()
        if sd > 1e-9:
            zscore[t] = (log_ratio[t] - mu) / sd

    # Forward change in the ratio: mean-reversion predicts a HIGH zscore (ratio
    # rich) is followed by the ratio falling (log_ratio decreasing).
    fwd_change = np.full(T, np.nan)
    fwd_change[:-HORIZON] = log_ratio[HORIZON:] - log_ratio[:-HORIZON]

    valid = np.isfinite(zscore) & np.isfinite(fwd_change)
    z, fc = zscore[valid], fwd_change[valid]
    ic = float(np.corrcoef(z, fc)[0, 1]) if z.std() > 0 else 0.0
    logger.info("\n" + "=" * 66)
    logger.info("GOLD/SILVER RATIO MEAN-REVERSION | %d days (%s..%s)", T, dates[0], dates[-1])
    logger.info("  information coefficient (zscore vs %d-day fwd ratio change): %+.4f "
                "(negative = mean-reverting, as hypothesized)", HORIZON, ic)

    # Trade: when |zscore| >= Z_ENTRY, bet on reversion (short the ratio if
    # rich, long if cheap) for HORIZON days; non-overlapping entries only
    # (skip until the current position's window has closed).
    trade_rets = []
    i = WINDOW
    while i < T - HORIZON:
        if np.isfinite(zscore[i]) and abs(zscore[i]) >= Z_ENTRY:
            direction = -np.sign(zscore[i])  # rich (positive z) -> bet ratio falls
            ret = direction * (log_ratio[i + HORIZON] - log_ratio[i])
            trade_rets.append(ret)
            i += HORIZON  # non-overlapping
        else:
            i += 1
    trade_rets = np.array(trade_rets)
    logger.info("  %d non-overlapping mean-reversion trades triggered (|z|>=%.1f)", len(trade_rets), Z_ENTRY)

    rng = np.random.default_rng(0)
    n_perm = 3000
    hits = 0
    observed_mean = trade_rets.mean() if len(trade_rets) else 0.0
    for _ in range(n_perm):
        shuffled_dir = rng.choice([-1.0, 1.0], size=len(trade_rets))
        idx = rng.integers(WINDOW, T - HORIZON, size=len(trade_rets))
        rr = shuffled_dir * (log_ratio[np.minimum(idx + HORIZON, T - 1)] - log_ratio[idx])
        if rr.mean() >= observed_mean:
            hits += 1
    perm_p = hits / n_perm

    any_survives = False
    for cost in (0.0, 0.0005, 0.0010, 0.0020):
        net = trade_rets - cost
        if len(net) < 5 or net.std() == 0:
            logger.info("  cost=%.2f%%  too few trades", cost * 100); continue
        periods_per_year = 250 / HORIZON
        sharpe = float(net.mean() / net.std() * np.sqrt(periods_per_year))
        survives = sharpe > 0 and net.mean() > 0 and cost > 0
        any_survives = any_survives or survives
        logger.info("  cost=%.2f%%  mean/trade=%.3f%%  Sharpe=%.2f  (%d trades)  -> %s",
                    cost * 100, net.mean() * 100, sharpe, len(net), "survives" if survives else "dies")
    logger.info("  shuffle p-value: %.4f", perm_p)

    edge = perm_p < 0.05 and any_survives
    logger.info("=" * 66)
    logger.info("VERDICT: %s", (
        "gold-silver ratio mean-reversion is real and survives costs — worth building"
        if edge else
        f"no tradeable edge (IC={ic:+.4f}, shuffle p={perm_p:.4f}, survives_costs={any_survives})"))
    return 0 if edge else 1


if __name__ == "__main__":
    raise SystemExit(main())
