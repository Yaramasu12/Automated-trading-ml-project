"""MCX commodity futures carry / roll-yield research — a structurally different
edge from anything tested so far in this repo (all prior research was directional
price TA on a single contract). This tests one of the most robustly documented
factors in commodities: the shape of the futures curve predicts returns.

Background (Erb & Harvey 2006, Gorton & Rouwenhorst 2006): when the curve is in
BACKWARDATION (near-month contract priced above far-month), holding the front
contract has historically earned a positive roll return as it converges toward
spot. When the curve is in CONTANGO (far > near), the roll return is negative.
This is genuinely different from "does the price go up" — it's priced off the
curve SHAPE, observable today, with no lookahead.

Data: MCX lists 4-6 simultaneous maturities per commodity at any time (checked
directly against the live instrument master), and far-dated contracts are listed
many months before they become the front month — so a genuine multi-maturity
history is reconstructable from CURRENTLY-LISTED contracts alone, without needing
an archived historical scrip master. This script fetches full daily history for
every currently-listed contract per commodity directly from Angel One.

Method (honest, no lookahead):
  * At each historical date, rank currently-listed-and-unexpired contracts by
    expiry; front = nearest, next = second-nearest.
  * carry_t = -annualized ln(next/front) basis (positive = backwardated = bullish
    for holding the front contract).
  * label = the front contract's OWN forward N-day return (only counted when the
    front contract has at least N days left before ITS OWN expiry, so the return
    is never computed across a contract roll).
  * Report the pooled information coefficient (correlation) between carry_t and
    forward return, a permutation-null p-value (shuffle carry values across the
    pooled sample, breaking the relationship while holding returns fixed), and a
    cost-aware backtest that trades sign(carry) with daily-portfolio aggregation
    across commodities (same discipline that caught the cross-sectional and gap
    backtests' sample-pooling bugs earlier in this project).

Only 7 commodities trade on MCX, far fewer than the 20-30+ used in the academic
literature — this is reported honestly as a statistical-power caveat regardless
of the verdict, not glossed over.

Run:  python scripts/research_commodity_carry.py
"""
from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("commodity_carry")

COMMODITIES = ("GOLD", "SILVER", "CRUDEOIL", "NATURALGAS", "COPPER", "ZINC", "NICKEL")


def fetch_curves(days_back: int = 730) -> dict[str, dict[date, dict]]:
    """Returns {commodity: {expiry_date: {date: close}}}."""
    from trading_platform.config import load_settings
    from trading_platform.data.angel_one_history import AngelOneHistoricalDataProvider
    from trading_platform.data.angel_one_instruments import AngelOneInstrumentMasterProvider

    s = load_settings()
    master = AngelOneInstrumentMasterProvider(s).load_cached()
    provider = AngelOneHistoricalDataProvider(s)
    to_dt = datetime.now()
    from_dt = to_dt - timedelta(days=days_back)

    curves: dict[str, dict[date, dict]] = {}
    for commodity in COMMODITIES:
        contracts = [
            inst for inst in master.instruments.values()
            if inst.name == commodity and inst.symbol.endswith("FUT") and inst.expiry is not None
        ]
        by_expiry: dict[date, dict] = {}
        for inst in contracts:
            try:
                bars = provider.get_candles(inst, from_dt, to_dt, "ONE_DAY")
            except Exception as exc:
                logger.warning("  fetch failed for %s (expiry=%s): %s", inst.symbol, inst.expiry, exc)
                continue
            if len(bars) < 15:
                continue
            by_expiry[inst.expiry] = {b.timestamp.date(): b.close for b in bars}
            logger.info("  %-22s expiry=%s  %d bars", inst.symbol, inst.expiry, len(bars))
        if len(by_expiry) >= 2:
            curves[commodity] = by_expiry
    return curves


def build_carry_samples(curves: dict[str, dict[date, dict]], horizon: int):
    """Pooled (commodity, date) samples: carry signal + forward front-month return."""
    samples = []  # (commodity, date, carry, fwd_ret)
    for commodity, by_expiry in curves.items():
        expiries = sorted(by_expiry)
        all_dates = sorted(set.union(*[set(by_expiry[e]) for e in expiries]))
        for d in all_dates:
            live = [e for e in expiries if e > d]
            if len(live) < 2:
                continue
            front, nxt = live[0], live[1]
            front_series = by_expiry[front]
            next_series = by_expiry[nxt]
            if d not in front_series or d not in next_series:
                continue
            front_px, next_px = front_series[d], next_series[d]
            if front_px <= 0 or next_px <= 0:
                continue
            days_between = (nxt - front).days
            if days_between <= 0:
                continue
            basis = np.log(next_px / front_px) * (365.0 / days_between)
            carry = -basis  # positive = backwardated = bullish front-month

            # Forward return of the SAME front contract, only if it stays the
            # front contract (has >= horizon days left) — never crosses a roll.
            days_to_front_expiry = (front - d).days
            if days_to_front_expiry < horizon:
                continue
            future_dates = sorted(dt for dt in front_series if dt > d)
            if len(future_dates) < horizon:
                continue
            fwd_date = future_dates[horizon - 1]
            fwd_ret = front_series[fwd_date] / front_px - 1.0
            samples.append((commodity, d, float(carry), float(fwd_ret)))
    return samples


def _daily_portfolio_sharpe(dates, rets, horizon: int) -> tuple[float, float, int]:
    from collections import defaultdict
    by_date = defaultdict(list)
    for d, r in zip(dates, rets):
        by_date[d].append(r)
    daily = np.array([np.mean(v) for _, v in sorted(by_date.items())])
    per = daily[::max(horizon, 1)]
    if len(per) < 5 or per.std() == 0:
        return 0.0, float(per.mean()) if len(per) else 0.0, len(per)
    sharpe = float(per.mean() / per.std() * np.sqrt(250 / max(horizon, 1)))
    return sharpe, float(per.mean()), len(per)


def _permutation_ic_p(carry: np.ndarray, fwd: np.ndarray, observed_ic: float, n_perm: int = 5000) -> float:
    rng = np.random.default_rng(0)
    hits = 0
    for _ in range(n_perm):
        shuffled = rng.permutation(carry)
        ic = np.corrcoef(shuffled, fwd)[0, 1]
        if abs(ic) >= abs(observed_ic):
            hits += 1
    return hits / n_perm


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--horizon", type=int, default=10, help="forward holding days")
    ap.add_argument("--days-back", type=int, default=730)
    args = ap.parse_args()

    logger.info("Fetching multi-maturity curves for %s ...", ", ".join(COMMODITIES))
    curves = fetch_curves(args.days_back)
    if len(curves) < 3:
        logger.error("Too few commodities with >=2 live maturities (%d) — cannot test.", len(curves))
        return 2
    logger.info("Got curves for %d commodities: %s", len(curves), ", ".join(sorted(curves)))

    samples = build_carry_samples(curves, args.horizon)
    if len(samples) < 100:
        logger.error("Too few (commodity,date) samples (%d) for a meaningful test.", len(samples))
        return 2
    commodities_used = sorted(set(s[0] for s in samples))
    dates = [s[1] for s in samples]
    carry = np.array([s[2] for s in samples])
    fwd = np.array([s[3] for s in samples])

    logger.info("\n" + "=" * 70)
    logger.info("CARRY / ROLL-YIELD  |  %d samples across %d commodities  |  horizon=%dd",
                len(samples), len(commodities_used), args.horizon)
    logger.info("CAVEAT: only %d commodities trade on MCX (literature typically uses 20-30+); "
                "statistical power here is inherently limited regardless of the result below.",
                len(commodities_used))

    ic = float(np.corrcoef(carry, fwd)[0, 1])
    perm_p = _permutation_ic_p(carry, fwd, ic)
    logger.info("  pooled information coefficient (carry vs fwd return): %+.4f  (shuffle p=%.4f)", ic, perm_p)

    logger.info("\n  --- cost-aware directional backtest: long if carry>0 else short, "
                "daily-portfolio aggregated ---")
    any_survives = False
    direction = np.sign(carry)
    for cost_pct in (0.0, 0.0005, 0.0010, 0.0020):
        gross = direction * fwd - cost_pct
        sharpe, mean_per, n_periods = _daily_portfolio_sharpe(dates, gross, args.horizon)
        survives = sharpe > 0 and mean_per > 0
        any_survives = any_survives or (survives and cost_pct > 0)
        logger.info("  cost=%.2f%%  mean/period=%.4f%%  Sharpe=%.2f  (%d non-overlapping periods) -> %s",
                    cost_pct * 100, mean_per * 100, sharpe, n_periods,
                    "survives" if survives else "dies")

    logger.info("=" * 70)
    edge = ic > 0.03 and perm_p < 0.05 and any_survives
    logger.info("VERDICT: %s", (
        "carry signal shows a real, shuffle-null-significant relationship AND survives costs — worth building"
        if edge else
        "no tradeable carry edge on this data (needs |IC|>0.03, shuffle p<0.05, AND survives realistic costs; "
        f"got IC={ic:+.4f}, p={perm_p:.4f}, survives_costs={any_survives}) — "
        "also limited by only 7 tradeable MCX commodities"
    ))
    return 0 if edge else 1


if __name__ == "__main__":
    raise SystemExit(main())
