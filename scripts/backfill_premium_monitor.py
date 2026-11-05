#!/usr/bin/env python3
"""Backfill the index_vol_premium DB table from the already-downloaded
atm_by_expiry.csv (see build_index_iv_history.py), so GET /research/premium-
status has real trailing-median history immediately instead of building it
one day at a time from whenever the scheduled job first runs.

Replays each day chronologically through the exact same
research.index_premium.compute_reading() the live daily job uses, so a
backfilled day and a freshly-scheduled day are computed identically.

    python scripts/backfill_premium_monitor.py                 # NSE (default)
    python scripts/backfill_premium_monitor.py --exchange bse  # SENSEX / BANKEX

--exchange bse replays the BSE checkpoint (data/historical/iv_bse/
atm_by_expiry.csv, written by build_index_iv_history.py --exchange bse) through
the identical compute_reading loop and writes SENSEX/BANKEX into the SAME
index_vol_premium table, so they get trailing-60d-median history immediately.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from trading_platform.data.persistence import TradingDatabase  # noqa: E402
from trading_platform.research.index_premium import INDICES, compute_reading, iv30_for_day  # noqa: E402
from trading_platform.research.bse_index_premium import BSE_INDICES  # noqa: E402

_CHECKPOINT = {
    "nse": (REPO / "data" / "historical" / "iv" / "atm_by_expiry.csv", INDICES),
    "bse": (REPO / "data" / "historical" / "iv_bse" / "atm_by_expiry.csv", BSE_INDICES),
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exchange", choices=("nse", "bse"), default="nse",
                    help="which checkpoint to replay (default nse — unchanged behaviour)")
    args = ap.parse_args()
    rows_csv, indices = _CHECKPOINT[args.exchange]

    if not rows_csv.exists():
        print(f"missing {rows_csv} — run build_index_iv_history.py "
              f"{'--exchange bse ' if args.exchange == 'bse' else ''}first", file=sys.stderr)
        return 2
    by_day: dict[str, list[dict]] = {}
    with open(rows_csv, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            by_day.setdefault(r["date"], []).append(r)

    db = TradingDatabase()
    prior_spots: dict[str, list[tuple[date, float]]] = {i: [] for i in indices}
    prior_premiums: dict[str, list[float]] = {i: [] for i in indices}
    n = 0
    for day_str in sorted(by_day):
        day = date.fromisoformat(day_str)
        for idx, (iv30, spot) in iv30_for_day(by_day[day_str]).items():
            realized, premium, trailing_median = compute_reading(
                day, iv30, spot, prior_spots[idx], prior_premiums[idx],
            )
            db.save_index_premium(day, idx, spot, iv30, realized, premium, trailing_median)
            prior_spots[idx] = (prior_spots[idx] + [(day, spot)])[-20:]
            if premium is not None:
                prior_premiums[idx] = (prior_premiums[idx] + [premium])[-59:]
            n += 1
    print(f"[{args.exchange}] backfilled {n} (day, index) rows into index_vol_premium")
    for idx in indices:
        latest = [r for r in db.latest_index_premium() if r["index_symbol"] == idx]
        if latest:
            r = latest[0]
            print(f"  {idx:11} latest={r['trade_date']} premium={r['premium']} trailing60d={r['trailing_median_60d']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
