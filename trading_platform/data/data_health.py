"""Phase-5 data-health monitor — staleness / gap observability over the
Phase-2 unified market-data store.

READ-ONLY by design: the ONLY write this module ever makes is a `risk_events`
row when a source is stale beyond tolerance, and it routes that through the
EXISTING alert channel (db.save_risk_event) rather than inventing a new one, so
staleness surfaces exactly where every other platform alert already does. It
never fetches, never backfills and never fabricates a reading — a day with no
data stays absent, a blocked source (MCX's Akamai WAF) stays "no_data"/failed.

For each (source, asset_class) it reports, bounded to a recent lookback window:
  * latest_trade_date actually present in the store,
  * gap_count vs the NSE trading calendar (market_hours.is_trading_day) — how
    many trading days inside the observed span have no row,
  * stale_trading_days — trading days between the latest present date and
    `as_of`, and whether that exceeds DATA_STALENESS_MAX_DAYS,
  * row freshness (total rows, rows on the latest date, last ingested_at),
  * the last successful and last failed ingestion_runs for the source.

Staleness is defined on actual data presence, counted in TRADING days, so
weekends/holidays never trip a false alarm. A source that has never produced a
row AND has no ingestion history is reported as "no_data", NOT "stale": that is
the honest state for an unconfigured or permanently-blocked source (e.g. MCX),
and it
keeps the alert channel from being spammed about data that was never expected.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone

from trading_platform.agent.market_hours import is_trading_day, now_ist

logger = logging.getLogger(__name__)

# The risk_events event_type used for a staleness alert. Kept stable so alerts
# are greppable and de-dupable across polls.
STALENESS_EVENT_TYPE = "data_staleness"

DEFAULT_STALENESS_MAX_DAYS = 4
# Calendar days the freshness/gap scan looks back over. ~45 calendar days is
# roughly 30 trading days — enough to show recent gaps without scanning the
# whole history on every /data/health poll.
LOOKBACK_CALENDAR_DAYS = 45

# Which (table, asset_class) each bhavcopy source is expected to populate in the
# Phase-2 store. The `source` string matches both ingestion_runs.source and the
# `source` column every persist_bhavcopy write stamps, so the two line up. MCX
# publishes only commodity futures (eod_ohlcv), no options / index IV.
SOURCE_ASSET_CLASSES: dict[str, list[tuple[str, str]]] = {
    "nse_fo": [
        ("eod_ohlcv", "ohlcv"),
        ("eod_option_quote", "option"),
        ("index_atm_iv30", "index_iv30"),
    ],
    "bse_fo": [
        ("eod_ohlcv", "ohlcv"),
        ("eod_option_quote", "option"),
        ("index_atm_iv30", "index_iv30"),
    ],
    "mcx_bhavcopy": [
        ("eod_ohlcv", "ohlcv"),
    ],
}


def _max_stale_days(explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    try:
        return int(os.getenv("DATA_STALENESS_MAX_DAYS", str(DEFAULT_STALENESS_MAX_DAYS)))
    except (TypeError, ValueError):
        return DEFAULT_STALENESS_MAX_DAYS


def _trading_days(start: date, end: date) -> list[date]:
    """Trading days in [start, end] inclusive, per the NSE calendar."""
    out: list[date] = []
    if start is None or end is None or start > end:
        return out
    d = start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def _trading_days_after(after: date, through: date) -> int:
    """Count of trading days strictly after `after`, up to and including
    `through`. 0 when `through <= after` (e.g. latest present == as_of)."""
    if after is None or through is None or through <= after:
        return 0
    return len(_trading_days(after + timedelta(days=1), through))


def _run_summary(run: dict | None) -> dict | None:
    if not run:
        return None
    td = run.get("trade_date")
    return {
        "trade_date": td.isoformat() if hasattr(td, "isoformat") else td,
        "state": run.get("state"),
        "rows": run.get("rows"),
        "detail": run.get("detail"),
        "updated_at": run.get("updated_at"),
    }


def compute_data_health(
    db,
    *,
    as_of: date | None = None,
    max_stale_days: int | None = None,
    lookback_calendar_days: int = LOOKBACK_CALENDAR_DAYS,
    write_alert: bool = True,
) -> dict:
    """Build the /data/health report. Optionally (default) write a risk_events
    row for each source found stale beyond tolerance, de-duped to at most one
    per source per UTC day so a polling monitor can't spam the channel."""
    as_of = as_of or now_ist().date()
    tol = _max_stale_days(max_stale_days)
    since = as_of - timedelta(days=lookback_calendar_days)

    entries: list[dict] = []
    stale_sources: dict[str, list[str]] = {}

    for source, classes in SOURCE_ASSET_CLASSES.items():
        try:
            last_done = db.last_ingestion_run_by_state(source, ("DONE",))
            last_failed = db.last_ingestion_run_by_state(source, ("FAILED",))
        except Exception as exc:  # noqa: BLE001 - never let a read failure break the report
            logger.debug("ingestion-run lookup failed for %s: %s", source, exc)
            last_done = last_failed = None
        has_run_history = bool(last_done or last_failed)

        for table, asset_class in classes:
            try:
                h = db.market_source_health(table, source, since=since)
            except Exception as exc:  # noqa: BLE001
                logger.debug("market_source_health failed for %s/%s: %s", source, table, exc)
                entries.append({
                    "source": source, "asset_class": asset_class, "table": table,
                    "status": "error", "error": str(exc)[:200], "is_stale": False,
                })
                continue

            present = h["trade_dates"]
            present_set = set(present)
            latest = h["latest_trade_date"]

            if present:
                gap_days = [d for d in _trading_days(present[0], latest) if d not in present_set]
            else:
                gap_days = []

            if latest is None:
                # No baseline. Not "stale" — either never configured, or a
                # legitimately data-less (e.g. perpetually WAF-blocked) source.
                status, is_stale, stale_days = "no_data", False, None
            else:
                stale_days = _trading_days_after(latest, as_of)
                if stale_days > tol:
                    status, is_stale = "stale", True
                else:
                    status, is_stale = "ok", False

            entries.append({
                "source": source,
                "asset_class": asset_class,
                "table": table,
                "status": status,
                "is_stale": is_stale,
                "latest_trade_date": latest.isoformat() if latest else None,
                "stale_trading_days": stale_days,
                "gap_count": len(gap_days),
                # last few missing trading days, for a human skim (not the full span)
                "gap_dates": [d.isoformat() for d in gap_days[-10:]],
                "total_rows": h["total_rows"],
                "rows_on_latest_date": h["rows_on_latest"],
                "last_ingested_at": h["last_ingested_at"],
                "has_ingestion_history": has_run_history,
                "last_successful_run": _run_summary(last_done),
                "last_failed_run": _run_summary(last_failed),
                "lookback_since": since.isoformat(),
            })
            if is_stale:
                stale_sources.setdefault(source, []).append(asset_class)

    alerts_written: list[str] = []
    if write_alert and stale_sources:
        for src, cls in stale_sources.items():
            try:
                if _write_staleness_alert(db, src, cls, tol, as_of):
                    alerts_written.append(src)
            except Exception as exc:  # noqa: BLE001 - alerting must never break the read
                logger.warning("failed to write data_staleness risk_event for %s: %s", src, exc)

    return {
        "as_of": as_of.isoformat(),
        "max_stale_trading_days": tol,
        "stale": bool(stale_sources),
        "stale_sources": stale_sources,
        "alerts_written": alerts_written,
        "entries": entries,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _write_staleness_alert(db, source: str, asset_classes: list[str],
                           tol: int, as_of: date) -> bool:
    """Write ONE data_staleness risk_event for `source`, unless one already
    exists today (UTC). Returns whether a row was written."""
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        for ev in db.recent_risk_events(limit=100):
            if (ev.get("event_type") == STALENESS_EVENT_TYPE
                    and ev.get("symbol") == source
                    and str(ev.get("occurred_at") or "")[:10] == today):
                return False  # already alerted for this source today
    except Exception as exc:  # noqa: BLE001
        logger.debug("recent_risk_events scan failed (will still alert): %s", exc)

    reason = (
        f"Market-data source '{source}' is stale beyond {tol} trading days "
        f"(asset classes: {', '.join(asset_classes)}) as of {as_of.isoformat()}. "
        "No fresh bhavcopy has been ingested — check the scheduler's post-market "
        "EOD ingest jobs / scripts/ingest_market_data.py."
    )
    db.save_risk_event(
        event_type=STALENESS_EVENT_TYPE, reason=reason, symbol=source, approved=False,
    )
    logger.warning("DATA STALENESS: %s", reason)
    return True
