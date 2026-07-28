"""USD/INR vs MCX metals correlation overlay. Gold and silver are dollar-
denominated globally; a weakening rupee mechanically inflates INR-denominated
MCX prices even with flat dollar gold, and a genuine USD move often
anticipates or coincides with metal moves. Tests whether USDINR's OWN recent
move has predictive power for forward gold/silver returns beyond what the
metal's own price history already captures — i.e. a genuinely different
information source, not another price-momentum feature.

Limitation stated up front: USDINR futures are monthly-listed on NSE's
currency segment, so (same constraint as research_index_basis.py and
research_commodity_carry.py) only the currently-listed near-month contract's
own history is available — a few months, not years.

Run:  python scripts/research_usdinr_overlay.py
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("usdinr_overlay")

HORIZON = 5
USDINR_TOKEN = "1265"   # USDINR26JULFUT — nearest expiry, longest available history


def fetch(days_back: int = 400):
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

    usdinr_inst = dataclasses.replace(template, token=USDINR_TOKEN, exchange=Exchange("CDS"), symbol="USDINRFUT")
    usdinr_bars = provider.get_candles(usdinr_inst, from_dt, to_dt, "ONE_DAY")
    usdinr = {b.timestamp.date(): b.close for b in usdinr_bars}
    logger.info("  USDINR: %d bars (%s..%s)", len(usdinr), min(usdinr) if usdinr else None, max(usdinr) if usdinr else None)

    metals = {}
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
        metals[commodity] = {b.timestamp.date(): b.close for b in bars}
        logger.info("  %s (%s): %d bars", commodity, front.symbol, len(bars))
    return usdinr, metals


def main() -> int:
    logger.info("Fetching USDINR + MCX metals...")
    usdinr, metals = fetch()
    if not usdinr or not metals:
        logger.error("Missing data."); return 2

    logger.info("\n" + "=" * 66)
    logger.info("USD/INR -> METALS OVERLAY (horizon=%dd)", HORIZON)
    any_edge = False
    for commodity, series in metals.items():
        dates = sorted(set(usdinr) & set(series))
        if len(dates) < 60:
            logger.info("\n%s: only %d overlapping days with USDINR — too little to test "
                        "(single-contract history limitation, as flagged above)", commodity, len(dates))
            continue
        fx = np.array([usdinr[d] for d in dates])
        px = np.array([series[d] for d in dates])
        fx_ret5 = np.full(len(dates), np.nan)
        fx_ret5[5:] = fx[5:] / fx[:-5] - 1.0   # trailing 5-day USDINR move
        fwd_metal_ret = np.full(len(dates), np.nan)
        fwd_metal_ret[:-HORIZON] = px[HORIZON:] / px[:-HORIZON] - 1.0

        valid = np.isfinite(fx_ret5) & np.isfinite(fwd_metal_ret)
        x, y = fx_ret5[valid], fwd_metal_ret[valid]
        ic = float(np.corrcoef(x, y)[0, 1]) if x.std() > 0 else 0.0
        logger.info("\n%s | %d overlapping days (%s..%s) | IC(trailing 5d USDINR move vs fwd "
                    "%dd metal return) = %+.4f", commodity, len(dates), dates[0], dates[-1], HORIZON, ic)

        rng = np.random.default_rng(0)
        n_perm = 3000
        hits = 0
        for _ in range(n_perm):
            shuffled = rng.permutation(x)
            perm_ic = np.corrcoef(shuffled, y)[0, 1] if shuffled.std() > 0 else 0.0
            if abs(perm_ic) >= abs(ic):
                hits += 1
        perm_p = hits / n_perm
        logger.info("  shuffle p-value: %.4f", perm_p)

        direction = np.sign(x)
        gross = direction * y
        per = gross[::HORIZON]
        if len(per) > 5 and per.std() > 0:
            for cost in (0.0, 0.0005, 0.0010):
                net = per - cost
                sharpe = float(net.mean() / net.std() * np.sqrt(250 / HORIZON))
                positive = net.mean() > 0
                logger.info("  cost=%.2f%%  mean/period=%.3f%%  Sharpe=%.2f  (%d periods) -> %s",
                            cost * 100, net.mean() * 100, sharpe, len(net), "PROFITABLE" if positive else "loses")
                if cost > 0 and positive and sharpe > 0.5 and abs(ic) > 0.05 and perm_p < 0.05:
                    any_edge = True
    logger.info("=" * 66)
    logger.info("VERDICT: %s", "USD/INR overlay edge found — worth building" if any_edge else
                "no tradeable edge (and/or insufficient single-contract history to test properly)")
    return 0 if any_edge else 1


if __name__ == "__main__":
    raise SystemExit(main())
