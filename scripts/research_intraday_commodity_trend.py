"""Intraday MCX commodity trend-following with volatility-scaled sizing.
Different question from the already-rejected daily-bar TA and the already-
rejected 5-min NSE equity microstructure test: commodities have structurally
different intraday dynamics (driven by global futures markets trading nearly
24h, so MCX intraday moves often continue a trend already established
overnight in London/NY, rather than mean-reverting like single equity names).
Volatility-scaled sizing (bet size inversely proportional to recent realized
vol) is standard CTA/managed-futures practice — tested here rather than flat
sizing, since flat-sized trend signals often fail purely on risk-of-ruin in
volatility spikes even when the direction is right.

Run:  python scripts/research_intraday_commodity_trend.py
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("intraday_commodity_trend")

COMMODITIES = ("GOLD", "CRUDEOIL", "NATURALGAS", "COPPER")
TREND_LOOKBACK_BARS = 24   # ~2 hours of 5-min bars
FWD_BARS = 6                 # ~30 min forward horizon
VOL_WINDOW = 24


def fetch(days_back: int = 90):
    from trading_platform.config import load_settings
    from trading_platform.data.angel_one_history import AngelOneHistoricalDataProvider
    from trading_platform.data.angel_one_instruments import AngelOneInstrumentMasterProvider

    s = load_settings()
    master = AngelOneInstrumentMasterProvider(s).load_cached()
    provider = AngelOneHistoricalDataProvider(s)
    to_dt = datetime.now(); from_dt = to_dt - timedelta(days=days_back)

    out = {}
    for commodity in COMMODITIES:
        contracts = sorted(
            (inst for inst in master.instruments.values()
             if inst.name == commodity and inst.symbol.endswith("FUT") and inst.expiry is not None),
            key=lambda i: i.expiry,
        )
        if not contracts:
            continue
        front = contracts[0]
        bars = provider.get_candles(front, from_dt, to_dt, interval="FIVE_MINUTE")
        out[commodity] = bars
        logger.info("  %s (%s): %d 5-min bars", commodity, front.symbol, len(bars))
    return out


def main() -> int:
    logger.info("Fetching MCX intraday 5-min data...")
    data = fetch()
    data = {k: v for k, v in data.items() if len(v) > 500}
    if not data:
        logger.error("No usable intraday commodity data."); return 2

    logger.info("\n" + "=" * 66)
    logger.info("INTRADAY COMMODITY TREND-FOLLOWING, vol-scaled (lookback=%d bars, horizon=%d bars)",
                TREND_LOOKBACK_BARS, FWD_BARS)
    all_gross = {c: [] for c in (0.0, 0.0005, 0.0010)}
    pooled_ic = []
    for commodity, bars in data.items():
        closes = np.array([b.close for b in bars])
        n = len(closes)
        logret = np.zeros(n); logret[1:] = np.log(closes[1:] / closes[:-1])

        trend = np.full(n, np.nan)
        vol = np.full(n, np.nan)
        for t in range(TREND_LOOKBACK_BARS, n):
            trend[t] = closes[t] / closes[t - TREND_LOOKBACK_BARS] - 1.0
            vol[t] = logret[t - VOL_WINDOW + 1:t + 1].std() if t >= VOL_WINDOW else np.nan
        fwd = np.full(n, np.nan)
        fwd[:-FWD_BARS] = closes[FWD_BARS:] / closes[:-FWD_BARS] - 1.0

        valid = np.isfinite(trend) & np.isfinite(vol) & np.isfinite(fwd) & (vol > 1e-6)
        tr, vl, fw = trend[valid], vol[valid], fwd[valid]
        ic = float(np.corrcoef(tr, fw)[0, 1]) if tr.std() > 0 else 0.0
        pooled_ic.append(ic)
        logger.info("\n%-12s | %d samples | IC(trend vs fwd return) = %+.4f", commodity, len(tr), ic)

        # Vol-scaled position: sign(trend) * (target_vol / realized_vol), capped.
        target_vol = np.median(vl)
        size = np.clip(target_vol / vl, 0.2, 3.0)
        gross = np.sign(tr) * size * fw
        for cost in all_gross:
            all_gross[cost].append(gross - cost * size)

    any_edge = False
    for cost, arrs in all_gross.items():
        pooled = np.concatenate(arrs)
        per = pooled[::FWD_BARS]
        sharpe = float(per.mean() / per.std() * np.sqrt(252 * (375 / FWD_BARS / 5))) if per.std() > 0 else 0.0
        positive = per.mean() > 0
        logger.info("  pooled cost=%.2f%%  mean/trade=%.4f%%  Sharpe(approx,ann)=%.2f  (%d obs) -> %s",
                    cost * 100, pooled.mean() * 100, sharpe, len(pooled), "PROFITABLE" if positive else "loses")
        if cost > 0 and positive and sharpe > 0.5:
            any_edge = True
    ic_mean = float(np.mean(pooled_ic)) if pooled_ic else 0.0
    logger.info("  mean IC across commodities: %+.4f", ic_mean)

    edge = any_edge and abs(ic_mean) > 0.02
    logger.info("=" * 66)
    logger.info("VERDICT: %s", "intraday commodity trend edge found — worth building" if edge else
                f"no tradeable edge (mean IC={ic_mean:+.4f}, survives_costs={any_edge})")
    return 0 if edge else 1


if __name__ == "__main__":
    raise SystemExit(main())
