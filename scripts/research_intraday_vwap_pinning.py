"""Two intraday NIFTY hypotheses, structurally different from every prior test
(all previous work here was on daily bars):

1. VWAP-REVERSION: does intraday price deviation from the session's running
   VWAP predict short-horizon mean-reversion (a genuine, well-documented
   microstructure effect — large deviations from VWAP attract liquidity
   providers betting on reversion)? This is an execution-timing question, not
   a directional-forecast question.

2. EXPIRY-DAY PINNING: NIFTY weekly options expire on Tuesdays (current NSE
   schedule). The "pin risk" effect (well documented for Indian weekly
   options — Vashishtha & Rakshit and others) predicts intraday range
   COMPRESSES on expiry afternoons as market-maker delta-hedging pins price
   near high-OI strikes. Tested here via realized intraday range in the last
   trading hour, expiry Tuesdays vs other days — same permutation-test
   discipline as research_turn_of_month.py.

Run:  python scripts/research_intraday_vwap_pinning.py
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("intraday_vwap_pinning")

VWAP_WINDOW_BARS = 12   # rolling 1-hour VWAP (5-min bars)
FWD_BARS = 3              # 15-minute forward horizon


def fetch(days_back: int = 90):
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
    inst = dataclasses.replace(template, token="99926000", exchange=Exchange("NSE"), symbol="NIFTY")
    bars = provider.get_candles(inst, from_dt, to_dt, interval="FIVE_MINUTE")
    logger.info("  fetched %d 5-min NIFTY bars (%s..%s)", len(bars), bars[0].timestamp if bars else None,
                bars[-1].timestamp if bars else None)
    return bars


def vwap_reversion(bars):
    logger.info("\n" + "-" * 66)
    logger.info("VWAP-REVERSION (rolling %d-bar VWAP, %d-bar / 15min forward horizon)",
                VWAP_WINDOW_BARS, FWD_BARS)
    by_day: dict = {}
    for b in bars:
        by_day.setdefault(b.timestamp.date(), []).append(b)

    devs, fwd_rets = [], []
    for day, day_bars in by_day.items():
        closes = np.array([b.close for b in day_bars])
        vols = np.array([max(b.volume, 1) for b in day_bars])
        if len(closes) < VWAP_WINDOW_BARS + FWD_BARS + 5:
            continue
        for i in range(VWAP_WINDOW_BARS, len(closes) - FWD_BARS):
            w_c = closes[i - VWAP_WINDOW_BARS:i]; w_v = vols[i - VWAP_WINDOW_BARS:i]
            vwap = float((w_c * w_v).sum() / w_v.sum())
            dev = closes[i] / vwap - 1.0
            fwd_ret = closes[i + FWD_BARS] / closes[i] - 1.0
            devs.append(dev); fwd_rets.append(fwd_ret)
    devs = np.array(devs); fwd_rets = np.array(fwd_rets)
    ic = float(np.corrcoef(devs, fwd_rets)[0, 1]) if devs.std() > 0 else 0.0
    logger.info("  %d intraday samples | IC(VWAP deviation vs fwd return) = %+.4f "
                "(negative expected: above VWAP -> reverts down)", len(devs), ic)

    rng = np.random.default_rng(0)
    n_perm = 2000
    hits = 0
    for _ in range(n_perm):
        shuffled = rng.permutation(devs)
        perm_ic = np.corrcoef(shuffled, fwd_rets)[0, 1] if shuffled.std() > 0 else 0.0
        if abs(perm_ic) >= abs(ic):
            hits += 1
    perm_p = hits / n_perm
    logger.info("  shuffle p-value: %.4f", perm_p)

    # Trade: fade deviations beyond 1 std, non-overlapping every FWD_BARS bars.
    thresh = devs.std()
    direction = np.where(np.abs(devs) >= thresh, -np.sign(devs), 0.0)
    triggered = direction != 0
    gross = direction[triggered] * fwd_rets[triggered]
    any_edge = False
    if triggered.sum() > 30:
        for cost in (0.0, 0.0010, 0.0020):   # index options-implied equity-equivalent cost per leg
            net = gross - cost
            per = net[::FWD_BARS] if len(net) > FWD_BARS else net
            sharpe = float(per.mean() / per.std() * np.sqrt(252 * 25)) if len(per) > 5 and per.std() > 0 else 0.0
            positive = per.mean() > 0 if len(per) else False
            logger.info("  cost=%.2f%%  mean/trade=%.4f%%  Sharpe(approx,ann)=%.2f  (%d trades) -> %s",
                        cost * 100, net.mean() * 100 if len(net) else 0, sharpe, triggered.sum(),
                        "PROFITABLE" if positive else "loses")
            if cost > 0 and positive and abs(ic) > 0.02 and perm_p < 0.05:
                any_edge = True
    logger.info("  VERDICT: %s", "VWAP-reversion edge — worth building" if any_edge else "no tradeable edge")
    return any_edge


def expiry_pinning(bars):
    logger.info("\n" + "-" * 66)
    logger.info("EXPIRY-DAY PINNING (last trading hour realized range, Tuesday-expiry vs other days)")
    by_day: dict = {}
    for b in bars:
        by_day.setdefault(b.timestamp.date(), []).append(b)

    tue_ranges, other_ranges = [], []
    for day, day_bars in sorted(by_day.items()):
        if len(day_bars) < 20:
            continue
        last_hour = day_bars[-12:]  # last ~1hr of 5-min bars
        closes = np.array([b.close for b in last_hour])
        rng_pct = (closes.max() - closes.min()) / closes.mean()
        if day.weekday() == 1:   # Tuesday
            tue_ranges.append(rng_pct)
        else:
            other_ranges.append(rng_pct)
    tue_ranges = np.array(tue_ranges); other_ranges = np.array(other_ranges)
    if len(tue_ranges) < 8 or len(other_ranges) < 8:
        logger.info("  too few days (%d Tuesdays, %d other) to test", len(tue_ranges), len(other_ranges))
        return False

    diff = float(tue_ranges.mean() - other_ranges.mean())
    logger.info("  %d Tuesday-expiry days, %d other days | last-hour range: expiry=%.3f%% other=%.3f%% "
                "diff=%+.3f%% (negative expected: pinning compresses range)",
                len(tue_ranges), len(other_ranges), tue_ranges.mean() * 100, other_ranges.mean() * 100, diff * 100)

    rng = np.random.default_rng(0)
    all_ranges = np.concatenate([tue_ranges, other_ranges])
    n_tue = len(tue_ranges)
    n_perm = 3000
    hits = 0
    for _ in range(n_perm):
        perm = rng.permutation(all_ranges)
        d = perm[:n_tue].mean() - perm[n_tue:].mean()
        if d <= diff:
            hits += 1
    perm_p = hits / n_perm
    logger.info("  shuffle p-value: %.4f", perm_p)
    edge = diff < 0 and perm_p < 0.05
    logger.info("  VERDICT: %s", "pinning effect is real (compresses range, p<0.05) — informs execution "
                "timing, not a directly tradeable P&L strategy on its own" if edge else "no significant pinning effect")
    return edge


def main() -> int:
    logger.info("Fetching NIFTY intraday 5-min data...")
    bars = fetch()
    if len(bars) < 500:
        logger.error("Too few bars fetched."); return 2
    e1 = vwap_reversion(bars)
    e2 = expiry_pinning(bars)
    logger.info("\n" + "=" * 66)
    logger.info("OVERALL VERDICT: VWAP-reversion=%s, expiry-pinning=%s", e1, e2)
    return 0 if (e1 or e2) else 1


if __name__ == "__main__":
    raise SystemExit(main())
