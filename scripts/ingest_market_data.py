#!/usr/bin/env python3
"""Resumable, backfill-capable EOD market-data ingestion orchestrator.

Pulls the FREE daily NSE / BSE bhavcopy into the Phase-2 market-data store
(instrument_master / eod_ohlcv / eod_option_quote / index_atm_iv30). ZERO
Angel One — the whole point of Phase 2 is to decouple validation/retraining
from the live feed.

Resumable + backfill-capable via the ingestion_runs checkpoint/lease table:
each (source, trade_date) is claimed with ONE insert (claim-or-skip, exactly
like learning_runs) before any fetch, so a re-run skips days already DONE /
HOLIDAY and a crashed day's expired lease is reclaimed. Per-day writes are
idempotent upserts, so a replay never double-counts.

    python scripts/ingest_market_data.py --source nse --date 2026-11-04
    python scripts/ingest_market_data.py --source all --start 2024-07-08 --end 2024-07-31
    python scripts/ingest_market_data.py --source bse --start 2026-11-01   # ... to today
"""
from __future__ import annotations

import argparse
import socket
import sys
import time
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from trading_platform.data.persistence import TradingDatabase  # noqa: E402
from trading_platform.data.ingest import nse_bhavcopy, bse_bhavcopy  # noqa: E402

SOURCES = {"nse": nse_bhavcopy, "bse": bse_bhavcopy}


def _daterange(start: date, end: date):
    for n in range((end - start).days + 1):
        d = start + timedelta(n)
        if d.weekday() < 5:  # skip weekends; holidays are detected on fetch
            yield d


def run_source(db, name: str, start: date, end: date, *, lease_owner: str,
               lease_seconds: float, delay: float, force: bool) -> dict:
    mod = SOURCES[name]
    totals = {"done": 0, "holiday": 0, "skipped": 0, "failed": 0, "rows": 0}
    for d in _daterange(start, end):
        if not force:
            claim = db.claim_ingestion_run(mod.SOURCE, d, lease_owner=lease_owner,
                                           lease_seconds=lease_seconds)
            if claim is None:
                totals["skipped"] += 1
                continue
            run_id = claim["id"]
        else:
            # Force: still record the run, but ignore a prior terminal state.
            claim = db.claim_ingestion_run(mod.SOURCE, d, lease_owner=lease_owner,
                                           lease_seconds=lease_seconds)
            run_id = claim["id"] if claim else None

        if delay:
            time.sleep(delay)
        try:
            counts = mod.ingest_day(db, d)
        except Exception as exc:  # noqa: BLE001
            totals["failed"] += 1
            if run_id is not None:
                db.update_ingestion_run(run_id, "FAILED", rows=0, detail=str(exc)[:500])
            print(f"[{name}] {d} FAILED: {exc}", flush=True)
            continue

        if counts is None:
            totals["holiday"] += 1
            if run_id is not None:
                db.update_ingestion_run(run_id, "HOLIDAY", rows=0, detail="no file")
            continue
        n = sum(counts.values())
        totals["done"] += 1
        totals["rows"] += n
        if run_id is not None:
            db.update_ingestion_run(run_id, "DONE", rows=n,
                                    detail=",".join(f"{k}={v}" for k, v in counts.items()))
        print(f"[{name}] {d} DONE {counts}", flush=True)
    return totals


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=("nse", "bse", "all"), default="nse")
    ap.add_argument("--date", help="a single ISO trade date (overrides start/end)")
    ap.add_argument("--start", help="first ISO trade date")
    ap.add_argument("--end", default=date.today().isoformat(), help="last ISO trade date")
    ap.add_argument("--lease-owner", default=f"ingest@{socket.gethostname()}")
    ap.add_argument("--lease-seconds", type=float, default=1800.0)
    ap.add_argument("--delay", type=float, default=0.2, help="per-request sleep, seconds")
    ap.add_argument("--force", action="store_true",
                    help="re-ingest even days already marked DONE/HOLIDAY")
    args = ap.parse_args()

    if args.date:
        start = end = date.fromisoformat(args.date)
    else:
        if not args.start:
            ap.error("provide --date or --start")
        start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    names = ("nse", "bse") if args.source == "all" else (args.source,)
    db = TradingDatabase()
    print(f"ingest backend={db.backend} sources={names} window={start}..{end}", flush=True)
    grand = {}
    for name in names:
        t = run_source(db, name, start, end, lease_owner=args.lease_owner,
                       lease_seconds=args.lease_seconds, delay=args.delay, force=args.force)
        grand[name] = t
        print(f"[{name}] totals: {t}", flush=True)
    print(f"ingestion complete: {grand}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
