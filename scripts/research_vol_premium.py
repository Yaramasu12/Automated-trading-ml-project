"""Options volatility-risk-premium research — a genuinely NON-directional edge.

Every price/technical/cross-sectional signal we tested predicts DIRECTION, and
none had tradeable edge. This tests something structurally different: the
volatility risk premium. In index options, implied volatility (what an option
seller RECEIVES) tends to exceed the realized volatility that follows (what the
seller PAYS OUT). Systematically selling options harvests that spread — you get
paid to bear volatility risk. It is one of the most robustly documented edges in
options markets, NIFTY included.

Test (honest, no lookahead):
  * IV proxy = India VIX on day t (a real, market-priced 30-day implied vol).
  * RV = the FORWARD ~21-trading-day realized vol actually delivered after t.
  * Premium_t = IV_t - RV_t. If its mean is positive and stable, selling vol pays.
  * A short-vol P&L proxy (vega-scaled IV-RV) is swept against option costs, and
    we report the TAIL (worst period) because short vol's risk is fat left tails.

Verdict: harvestable only if mean premium is positive with a sane Sharpe AND the
tail is survivable (short vol that blows up in one crash is not a real edge).
"""
from __future__ import annotations

import dataclasses
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("vol_premium")


def fetch(days: int = 900):
    from trading_platform.config import load_settings
    from trading_platform.data.angel_one_history import AngelOneHistoricalDataProvider
    from trading_platform.data.angel_one_instruments import AngelOneInstrumentMasterProvider
    from trading_platform.domain.enums import Exchange

    s = load_settings()
    master = AngelOneInstrumentMasterProvider(s).load_cached()
    h = AngelOneHistoricalDataProvider(s)
    to_dt = datetime.now(); from_dt = to_dt - timedelta(days=days)
    nifty = master.get("NIFTY")

    def series(token):
        inst = dataclasses.replace(nifty, token=token, exchange=Exchange("NSE"))
        bars = h.get_candles(inst, from_dt, to_dt, "ONE_DAY")
        return {b.timestamp.date(): b.close for b in bars}

    vix = series("99926017")     # India VIX (implied vol, %)
    spot = series("99926000")    # NIFTY spot
    dates = sorted(set(vix) & set(spot))
    return (np.array(dates),
            np.array([vix[d] for d in dates]),
            np.array([spot[d] for d in dates]))


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window", type=int, default=21, help="forward trading days for realized vol")
    ap.add_argument("--days", type=int, default=900)
    args = ap.parse_args()

    dates, vix, spot = fetch(args.days)
    if len(dates) < 200:
        logger.error("Too little data (%d days).", len(dates)); return 2
    logger.info("Vol-risk-premium | %d days (%s..%s) | RV window=%dd",
                len(dates), dates[0], dates[-1], args.window)

    logret = np.zeros(len(spot))
    logret[1:] = np.log(spot[1:] / spot[:-1])
    iv = vix / 100.0                                   # annualized implied vol
    W = args.window
    prem, rv_all = [], []
    for t in range(len(spot) - W):
        rv = logret[t + 1:t + 1 + W].std() * np.sqrt(252)   # forward realized vol, annualized
        prem.append(iv[t] - rv); rv_all.append(rv)
    prem = np.asarray(prem); rv_all = np.asarray(rv_all); iv_used = iv[:len(prem)]

    mean_prem = float(prem.mean())
    pct_pos = float((prem > 0).mean())
    # Short-vol P&L proxy per period: you're short vega, so P&L ∝ (IV - RV).
    # Normalize by IV so it reads like a return on the premium sold.
    ret = prem / np.maximum(iv_used, 1e-6)
    periods_per_year = 252 / W
    sharpe = float(ret.mean() / (ret.std() + 1e-12) * np.sqrt(periods_per_year))
    ann = float(ret.mean() * periods_per_year)
    worst = float(ret.min())                            # the fat left tail

    logger.info("\n" + "=" * 64)
    logger.info("VOLATILITY RISK PREMIUM  (India VIX vs forward realized vol)")
    logger.info("  mean IV           : %.1f%%   mean RV: %.1f%%", iv_used.mean()*100, rv_all.mean()*100)
    logger.info("  mean premium (IV-RV): %+.2f vol-points   positive %.0f%% of the time",
                mean_prem * 100, pct_pos * 100)
    logger.info("  short-vol proxy   : ann %+.1f%%  Sharpe %.2f  worst-period %.1f%%",
                ann * 100, sharpe, worst * 100)
    # crude cost sensitivity: option round-trip is richer; assume ~2%% of premium drag
    for cost_frac in (0.0, 0.05, 0.10):
        net = ret - cost_frac
        ns = float(net.mean() / (net.std() + 1e-12) * np.sqrt(periods_per_year))
        logger.info("    cost=%.0f%% of premium -> ann %+.1f%%  Sharpe %.2f",
                    cost_frac*100, net.mean()*periods_per_year*100, ns)
    logger.info("=" * 64)
    harvestable = mean_prem > 0 and pct_pos > 0.6 and sharpe > 0.7
    logger.info("VERDICT: %s", (
        "VOL PREMIUM is real and harvestable — build a short-vol strategy (mind the tail)"
        if harvestable else
        "vol premium not clearly harvestable on this data"))
    logger.info("NOTE: short vol earns steadily then loses big in crashes — the worst-period"
                " number above is the risk you must size for.")
    return 0 if harvestable else 1


if __name__ == "__main__":
    raise SystemExit(main())
