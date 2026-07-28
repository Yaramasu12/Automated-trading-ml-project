"""NIFTY/BANKNIFTY cash-futures basis mean-reversion. The futures-spot spread
(basis) should shrink to ~0 by expiry (cost-of-carry arbitrage keeps it
bounded) — a large basis today should predict convergence. Genuinely
different from every prior directional test: it's a relative-value bet
between two instruments on the SAME underlying, not a price-direction call.

Limitation stated up front: NSE index futures are monthly, and only the
CURRENTLY-listed near-month contract's own history is available (same
constraint hit in research_commodity_carry.py) — so this test only covers
however many months that single contract has traded, not a multi-year
history. Reported honestly regardless of the result.

Run:  python scripts/research_index_basis.py
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("index_basis")

HORIZON = 3

# (label, spot_token, near_future_token, near_future_symbol)
PAIRS = [
    ("NIFTY", "99926000", "61093", "NIFTY28JUL26FUT"),
    ("BANKNIFTY", "99926009", "61088", "BANKNIFTY28JUL26FUT"),
]


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

    results = {}
    for label, spot_tok, fut_tok, fut_sym in PAIRS:
        spot_inst = dataclasses.replace(template, token=spot_tok, exchange=Exchange("NSE"), symbol=label)
        fut_inst = dataclasses.replace(template, token=fut_tok, exchange=Exchange("NFO"), symbol=fut_sym)
        spot_bars = provider.get_candles(spot_inst, from_dt, to_dt, "ONE_DAY")
        fut_bars = provider.get_candles(fut_inst, from_dt, to_dt, "ONE_DAY")
        spot = {b.timestamp.date(): b.close for b in spot_bars}
        fut = {b.timestamp.date(): b.close for b in fut_bars}
        dates = sorted(set(spot) & set(fut))
        logger.info("  %-10s spot=%d bars  future(%s)=%d bars  overlap=%d days",
                    label, len(spot), fut_sym, len(fut), len(dates))
        results[label] = (dates, spot, fut)
    return results


def main() -> int:
    logger.info("Fetching NIFTY/BANKNIFTY spot + near-month futures...")
    data = fetch()
    logger.info("\n" + "=" * 66)
    logger.info("CASH-FUTURES BASIS MEAN-REVERSION (horizon=%dd)", HORIZON)
    any_edge = False
    for label, (dates, spot, fut) in data.items():
        if len(dates) < 40:
            logger.info("\n%s: only %d overlapping days — too little to test (single-contract "
                        "history limitation, as flagged above)", label, len(dates))
            continue
        s = np.array([spot[d] for d in dates])
        f = np.array([fut[d] for d in dates])
        basis = f / s - 1.0
        fwd_basis_change = np.full(len(dates), np.nan)
        fwd_basis_change[:-HORIZON] = basis[HORIZON:] - basis[:-HORIZON]
        valid = np.isfinite(fwd_basis_change)
        b, fb = basis[valid], fwd_basis_change[valid]
        ic = float(np.corrcoef(b, fb)[0, 1]) if b.std() > 0 else 0.0
        logger.info("\n%s | %d days (%s..%s) | basis mean=%.3f%% std=%.3f%% | IC(basis vs fwd basis "
                    "change)=%+.4f (negative expected: high basis -> basis shrinks)",
                    label, len(dates), dates[0], dates[-1], basis.mean() * 100, basis.std() * 100, ic)

        # Backtest: short the future / long spot when basis is rich (top
        # tercile), reverse when cheap, non-overlapping HORIZON-day trades.
        thresh_hi = np.percentile(basis, 67); thresh_lo = np.percentile(basis, 33)
        trades = []
        i = 0
        while i < len(dates) - HORIZON:
            if basis[i] >= thresh_hi:
                trades.append(-(basis[i + HORIZON] - basis[i])); i += HORIZON
            elif basis[i] <= thresh_lo:
                trades.append(basis[i + HORIZON] - basis[i]); i += HORIZON
            else:
                i += 1
        trades = np.array(trades)
        if len(trades) < 5:
            logger.info("  too few threshold-crossing trades (%d) to backtest", len(trades)); continue
        for cost in (0.0, 0.0005, 0.0010):
            net = trades - cost
            sharpe = float(net.mean() / net.std() * np.sqrt(250 / HORIZON)) if net.std() > 0 else 0.0
            positive = net.mean() > 0
            logger.info("  cost=%.2f%%  mean/trade=%.3f%%  Sharpe=%.2f  (%d trades) -> %s",
                        cost * 100, net.mean() * 100, sharpe, len(net), "PROFITABLE" if positive else "loses")
            if cost > 0 and positive and sharpe > 0.5 and abs(ic) > 0.05:
                any_edge = True
    logger.info("=" * 66)
    logger.info("VERDICT: %s", "basis mean-reversion edge found — worth building" if any_edge else
                "no tradeable edge (and/or insufficient single-contract history to test properly)")
    return 0 if any_edge else 1


if __name__ == "__main__":
    raise SystemExit(main())
