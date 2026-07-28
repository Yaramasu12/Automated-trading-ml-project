"""Turn-of-month seasonality — one of the oldest documented calendar anomalies
(Ariel 1987; Lakonishok & Smidt 1988): equity index returns are disproportionately
earned in a narrow window around month-end/month-start, not spread evenly across
the month. Structurally different from research_gap_calendar_volume.py, which
mixed a calendar feature into a gap+volume model where gap_pct dominated the
signal — this isolates the calendar effect alone on the index level.

This is not a fitted/trained signal — it's a fixed calendar rule (long the index
on the last trading day of the month through the first N days of the next month),
so there is no walk-forward training step. The discipline instead is:
  * A t-test / permutation test comparing turn-of-month day returns to all other
    days' returns (shuffle which days are labeled "turn-of-month", holding the
    actual return sequence fixed).
  * A cost-aware backtest actually holding the position only across the
    turn-of-month window, non-overlapping periods (one per month), swept costs.

Verdict: tradeable only if turn-of-month-day mean return is significantly higher
than other days (permutation p<0.05) AND the resulting monthly-rebalance backtest
survives realistic costs with a sane Sharpe.

Run:  python scripts/research_turn_of_month.py
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("turn_of_month")

# (symbol_label, token, exchange)
INDICES = [
    ("NIFTY", "99926000", "NSE"),
    ("BANKNIFTY", "99926009", "NSE"),
    ("SENSEX", "99919000", "BSE"),
    ("BANKEX", "99919012", "BSE"),
]

LAST_N_DAYS = 1   # last N trading days of the month
FIRST_N_DAYS = 3  # first N trading days of the next month


def fetch_series(days: int = 900):
    import dataclasses
    from trading_platform.config import load_settings
    from trading_platform.data.angel_one_history import AngelOneHistoricalDataProvider
    from trading_platform.data.angel_one_instruments import AngelOneInstrumentMasterProvider
    from trading_platform.domain.enums import Exchange

    s = load_settings()
    master = AngelOneInstrumentMasterProvider(s).load_cached()
    provider = AngelOneHistoricalDataProvider(s)
    to_dt = datetime.now(); from_dt = to_dt - timedelta(days=days)
    template = next(iter(master.instruments.values()))

    out = {}
    for label, token, exch in INDICES:
        inst = dataclasses.replace(template, token=token, exchange=Exchange(exch), symbol=label)
        bars = provider.get_candles(inst, from_dt, to_dt, "ONE_DAY")
        if len(bars) < 200:
            logger.warning("  %s: only %d bars, skipping", label, len(bars))
            continue
        out[label] = {b.timestamp.date(): b.close for b in sorted(bars, key=lambda b: b.timestamp)}
        logger.info("  %-10s %d bars (%s..%s)", label, len(bars),
                    min(out[label]), max(out[label]))
    return out


def _label_turn_of_month(dates: list) -> np.ndarray:
    """True for the last LAST_N_DAYS trading days of a month and the first
    FIRST_N_DAYS trading days of the following month."""
    is_tom = np.zeros(len(dates), dtype=bool)
    for i, d in enumerate(dates):
        month_dates = [j for j, dd in enumerate(dates) if dd.year == d.year and dd.month == d.month]
        pos_in_month = month_dates.index(i)
        if pos_in_month < FIRST_N_DAYS:
            is_tom[i] = True
        elif pos_in_month >= len(month_dates) - LAST_N_DAYS:
            is_tom[i] = True
    return is_tom


def _permutation_p(logret: np.ndarray, is_tom: np.ndarray, observed_diff: float, n_perm: int = 5000) -> float:
    rng = np.random.default_rng(0)
    n_tom = int(is_tom.sum())
    hits = 0
    idx = np.arange(len(logret))
    for _ in range(n_perm):
        shuffled = rng.choice(idx, size=n_tom, replace=False)
        mask = np.zeros(len(logret), dtype=bool); mask[shuffled] = True
        diff = logret[mask].mean() - logret[~mask].mean()
        if diff >= observed_diff:
            hits += 1
    return hits / n_perm


def analyze(label: str, series: dict, cost_sweep=(0.0, 0.0005, 0.0010)):
    dates = sorted(series)
    closes = np.array([series[d] for d in dates])
    logret = np.zeros(len(closes))
    logret[1:] = np.log(closes[1:] / closes[:-1])
    is_tom = _label_turn_of_month(dates)

    tom_mean = float(logret[is_tom].mean())
    other_mean = float(logret[~is_tom].mean())
    diff = tom_mean - other_mean
    perm_p = _permutation_p(logret, is_tom, diff)

    # Backtest: hold a long position through each turn-of-month window as ONE
    # trade (entry at the window's first day's open-equivalent = prior close,
    # exit at the window's last day's close) — genuinely non-overlapping,
    # one trade per calendar month.
    windows = []
    cur = []
    for i, tom in enumerate(is_tom):
        if tom:
            cur.append(i)
        elif cur:
            windows.append(cur); cur = []
    if cur:
        windows.append(cur)
    # merge windows that are adjacent across a month boundary (month-end run
    # immediately followed by next month's start run) into one held trade
    merged = []
    for w in windows:
        if merged and w[0] == merged[-1][-1] + 1:
            merged[-1].extend(w)
        else:
            merged.append(w)
    trade_rets = []
    for w in merged:
        entry_close = closes[w[0] - 1] if w[0] > 0 else closes[w[0]]
        exit_close = closes[w[-1]]
        trade_rets.append(exit_close / entry_close - 1.0)
    trade_rets = np.array(trade_rets)

    logger.info("\n%s | %d days (%s..%s) | %d turn-of-month trades",
                label, len(dates), dates[0], dates[-1], len(trade_rets))
    logger.info("  mean daily logret: turn-of-month=%+.4f%%  other=%+.4f%%  diff=%+.4f%%  "
                "(shuffle p=%.4f)", tom_mean * 100, other_mean * 100, diff * 100, perm_p)
    any_survives = False
    for cost in cost_sweep:
        net = trade_rets - cost
        if len(net) < 5 or net.std() == 0:
            logger.info("  cost=%.2f%%  too few trades to evaluate", cost * 100)
            continue
        sharpe = float(net.mean() / net.std() * np.sqrt(12))  # ~monthly trades
        survives = sharpe > 0 and net.mean() > 0 and cost > 0
        any_survives = any_survives or survives
        logger.info("  cost=%.2f%%  mean/trade=%.3f%%  ann_est=%.1f%%  Sharpe=%.2f  (%d trades) -> %s",
                    cost * 100, net.mean() * 100, net.mean() * 12 * 100, sharpe, len(net),
                    "survives" if survives else "dies")
    edge = perm_p < 0.05 and any_survives
    return edge, perm_p, diff


def main() -> int:
    logger.info("Fetching index history for turn-of-month test...")
    series_by_index = fetch_series()
    if not series_by_index:
        logger.error("No index data fetched."); return 2

    logger.info("\n" + "=" * 66)
    logger.info("TURN-OF-MONTH SEASONALITY  (last %dd of month + first %dd of next)",
                LAST_N_DAYS, FIRST_N_DAYS)
    any_edge = False
    for label, series in series_by_index.items():
        edge, p, diff = analyze(label, series)
        any_edge = any_edge or edge
    logger.info("=" * 66)
    logger.info("VERDICT: %s", (
        "turn-of-month effect is real (shuffle p<0.05) AND survives costs on at least "
        "one index — worth building"
        if any_edge else
        "no tradeable turn-of-month edge on this data across NIFTY/BANKNIFTY/SENSEX/BANKEX"
    ))
    return 0 if any_edge else 1


if __name__ == "__main__":
    raise SystemExit(main())
