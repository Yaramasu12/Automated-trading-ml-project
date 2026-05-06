#!/usr/bin/env python3
"""Daily task scheduler for the trading platform.

Runs three jobs every trading day:
  08:55 IST  — Refresh Angel One instrument master (before market open at 09:15)
  15:20 IST  — EOD auto square-off (NSE close is 15:30; give 10-min buffer)
  15:35 IST  — Save daily P&L report to the SQLite database

Run as a long-lived process (Docker service, systemd unit, or screen session):
    python scripts/daily_scheduler.py

All times are in IST (UTC+5:30).  The scheduler sleeps between jobs and
wakes exactly when the next job is due — no busy-loop, no Kafka required.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

# Make sure the project root is on sys.path when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from trading_platform.agent.market_hours import is_trading_day as _platform_is_trading_day

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("scheduler")

IST = ZoneInfo("Asia/Kolkata")

# (hour, minute) in IST
_JOBS: list[tuple[int, int, str]] = [
    (8,  55, "instrument_refresh"),
    (15, 20, "eod_square_off"),        # equity EOD — NSE/BSE close at 15:30
    (15, 35, "daily_pnl_report"),
    (23, 25, "mcx_eod_square_off"),    # commodity EOD — MCX close at 23:30
]

def _now_ist() -> datetime:
    return datetime.now(IST)


def _is_trading_day(dt: datetime) -> bool:
    return _platform_is_trading_day(dt.date())


def _next_run(hour: int, minute: int) -> datetime:
    """Return the next wall-clock datetime (IST) for the given (hour, minute)."""
    now = _now_ist()
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    # Skip weekends
    while not _is_trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def _sleep_until(target: datetime) -> None:
    now = _now_ist()
    delta = (target - now).total_seconds()
    if delta > 0:
        logger.info("Sleeping %.0f seconds until %s IST", delta, target.strftime("%H:%M"))
        time.sleep(delta)


# ---------------------------------------------------------------------------
# Job implementations
# ---------------------------------------------------------------------------

def run_instrument_refresh() -> None:
    logger.info("JOB: instrument_refresh — refreshing Angel One instrument master")
    try:
        from trading_platform.config import load_settings
        from trading_platform.data.angel_one_instruments import AngelOneInstrumentMasterProvider

        settings = load_settings()
        if not settings.angel_one_configured:
            logger.warning("Angel One not configured — skipping instrument refresh")
            return
        provider = AngelOneInstrumentMasterProvider(settings)
        result = provider.refresh()
        logger.info(
            "Instrument master refreshed: %d parsed, %d skipped",
            result.parsed_count,
            result.skipped_count,
        )
    except Exception as exc:
        logger.error("instrument_refresh failed: %s", exc)


def run_eod_square_off() -> None:
    logger.info("JOB: eod_square_off — squaring off all open intraday positions")
    try:
        from trading_platform.config import load_settings
        from trading_platform.data.persistence import TradingDatabase

        settings = load_settings()
        db = TradingDatabase()
        # Log the square-off event; actual order submission happens in the live runtime
        db.save_risk_event(
            event_type="eod_square_off_triggered",
            reason="Scheduled EOD square-off at 15:20 IST",
            approved=True,
        )
        logger.info("EOD square-off event recorded. Runtime will execute via kill-switch if live.")
        if settings.angel_one_configured:
            # POST to the local API to activate kill-switch (safest approach)
            try:
                import urllib.request
                token = os.environ.get("API_AUTH_TOKEN", "")
                headers = {"Content-Type": "application/json"}
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                req = urllib.request.Request(
                    "http://localhost:8000/kill-switch",
                    data=b'{"active": true}',
                    headers=headers,
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=5)
                logger.info("Kill-switch activated via API for EOD square-off")
            except Exception as api_exc:
                logger.warning("Could not reach API for kill-switch: %s", api_exc)
    except Exception as exc:
        logger.error("eod_square_off failed: %s", exc)


def run_daily_pnl_report() -> None:
    logger.info("JOB: daily_pnl_report — recording daily P&L snapshot")
    try:
        from datetime import date
        from trading_platform.data.persistence import TradingDatabase

        db = TradingDatabase()
        today = date.today()
        today_start = datetime.combine(today, dtime.min, tzinfo=timezone.utc)

        # Count today's trades, pushing the date filter into SQLite
        today_trades = db.trades(since=today_start, execution_mode="LIVE", limit=2000)
        total = len(today_trades)
        # Cannot derive wins from side alone (a SELL may close a loss); set 0
        wins = 0

        snapshot = db.latest_snapshot(execution_mode="LIVE")
        equity = snapshot["equity"] if snapshot else 0.0
        realized = snapshot["realized_pnl"] if snapshot else 0.0
        unrealized = snapshot["unrealized_pnl"] if snapshot else 0.0

        db.upsert_daily_pnl(
            trade_date=today,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            total_trades=total,
            winning_trades=wins,
            ending_equity=equity,
        )
        logger.info(
            "Daily P&L recorded: equity=%.2f, realized=%.2f, trades=%d",
            equity, realized, total,
        )
    except Exception as exc:
        logger.error("daily_pnl_report failed: %s", exc)


def run_mcx_eod_square_off() -> None:
    logger.info("JOB: mcx_eod_square_off — squaring off open MCX commodity positions at 23:25 IST")
    try:
        from trading_platform.config import load_settings

        settings = load_settings()
        if settings.angel_one_configured:
            try:
                import urllib.request
                token = os.environ.get("API_AUTH_TOKEN", "")
                headers = {"Content-Type": "application/json"}
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                # Use the square-off API endpoint for commodity scope
                req = urllib.request.Request(
                    "http://localhost:8000/square-off",
                    data=b'{"scope": "GLOBAL", "reason": "mcx_eod_squareoff_23:25"}',
                    headers=headers,
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=5)
                logger.info("MCX EOD square-off triggered via API")
            except Exception as api_exc:
                logger.warning("Could not reach API for MCX EOD square-off: %s", api_exc)
    except Exception as exc:
        logger.error("mcx_eod_square_off failed: %s", exc)


_JOB_FNS = {
    "instrument_refresh": run_instrument_refresh,
    "eod_square_off": run_eod_square_off,
    "daily_pnl_report": run_daily_pnl_report,
    "mcx_eod_square_off": run_mcx_eod_square_off,
}


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("Daily scheduler starting — will run on trading days (Mon-Fri)")
    logger.info("Jobs: %s", [f"{h:02d}:{m:02d} IST → {name}" for h, m, name in _JOBS])

    while True:
        # Find the soonest upcoming job
        now = _now_ist()
        upcoming = [
            (_next_run(h, m), name)
            for h, m, name in _JOBS
        ]
        upcoming.sort(key=lambda x: x[0])
        next_time, next_name = upcoming[0]

        _sleep_until(next_time)

        # Double-check it's still a trading day (could have drifted over weekend)
        if not _is_trading_day(_now_ist()):
            logger.info("Skipping %s — not a trading day", next_name)
            time.sleep(60)
            continue

        logger.info("Running job: %s", next_name)
        _JOB_FNS[next_name]()

        # Brief pause so we don't re-trigger the same minute
        time.sleep(90)


if __name__ == "__main__":
    main()
