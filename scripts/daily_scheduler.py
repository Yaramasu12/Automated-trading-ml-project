#!/usr/bin/env python3
"""Daily task scheduler for the trading platform.

Runs scheduled jobs every trading day:
  08:55 IST  — Refresh Angel One instrument master (before market open at 09:15)
  08:58 IST  — Start live feed
  15:20 IST  — EOD auto square-off (NSE close is 15:30; give 10-min buffer)
  15:35 IST  — Stop live feed
  15:36 IST  — Save daily P&L report to the SQLite database
  16:15 IST  — Re-validate the return forecaster on fresh daily candles
  16:30 IST  — Re-validate the intraday forecaster on 5-minute candles
  23:25 IST  — MCX EOD square-off

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
import urllib.error
import urllib.request
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
    (8,  58, "start_feed"),
    (15, 20, "eod_square_off"),        # equity EOD — NSE/BSE close at 15:30
    (15, 35, "stop_feed"),
    (15, 36, "daily_pnl_report"),
    (16, 15, "model_retrain"),         # after the equity close, before MCX EOD
    (16, 30, "intraday_retrain"),
    (23, 25, "mcx_eod_square_off"),    # commodity EOD — MCX close at 23:30
]

# Forward horizons (bars) the nightly re-validation sweeps. Mirrors the --sweep
# list in scripts/train_return_forecaster.py.
RETRAIN_HORIZONS = (1, 3, 5, 10)
RETRAIN_DAYS = 750
RETRAIN_INTERVAL = "ONE_DAY"

# Intraday sweep: 3/6/12 five-minute bars = 15/30/60 minutes ahead.
INTRADAY_HORIZONS = (3, 6, 12)
# Must match the day-count in the cache filenames (SYMBOL__FIVE_MINUTE_<days>d.npz),
# because that is what makes this job runnable without Angel One credentials.
INTRADAY_DAYS = 40

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


def _api_base_url() -> str:
    return os.environ.get("TRADING_API_BASE_URL", "http://trading-api:8000").rstrip("/")


def _api_post(path: str, payload: bytes, *, retries: int = 3, timeout: int = 5) -> None:
    token = os.environ.get("API_AUTH_TOKEN", "")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{_api_base_url()}{path}"
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            urllib.request.urlopen(req, timeout=timeout).close()
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
            logger.warning("API POST %s failed on attempt %d/%d: %s", path, attempt, retries, exc)
            if attempt < retries:
                time.sleep(min(2 ** (attempt - 1), 5))
    raise RuntimeError(f"API POST {path} failed after {retries} attempts: {last_exc}")


def _checkpoint_sqlite_wal() -> None:
    for label, factory in (
        ("trading", _trading_db_factory),
        ("oms", _oms_store_factory),
        ("paper_learning", _paper_learning_factory),
    ):
        try:
            store = factory()
            store.checkpoint()
            close = getattr(store, "close", None)
            if callable(close):
                close()
            logger.info("SQLite WAL checkpoint completed for %s", label)
        except Exception as exc:
            logger.warning("SQLite WAL checkpoint failed for %s: %s", label, exc)


def _trading_db_factory():
    from trading_platform.data.persistence import TradingDatabase

    return TradingDatabase()


def _oms_store_factory():
    from trading_platform.execution.oms_store import OMSEventStore

    return OMSEventStore()


def _paper_learning_factory():
    from trading_platform.trace.learning_journal import PaperLearningJournal

    return PaperLearningJournal()


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
        logger.info("EOD square-off event recorded. Runtime will execute square-off via API if configured.")
        if settings.angel_one_configured:
            # POST to the API with a reason so liquidation/audit state is explicit.
            try:
                _api_post(
                    "/execution/square-off",
                    b'{"scope": "GLOBAL", "reason": "scheduled_eod_square_off_15:20_ist"}',
                )
                logger.info("EOD square-off triggered via API")
            except Exception as api_exc:
                logger.warning("EOD square-off API activation failed; event is recorded locally: %s", api_exc)
    except Exception as exc:
        logger.error("eod_square_off failed: %s", exc)


def _count_winning_trades(trades: list) -> int:
    """Count completed round-trips where the realised P&L is positive.

    Uses FIFO matching: each SELL is matched against the earliest unmatched BUY
    for the same symbol.  A matched pair is a "win" when sell_price > buy_price.
    """
    from collections import defaultdict, deque
    buys: dict = defaultdict(deque)   # symbol -> deque of (price, qty)
    wins = 0
    for t in trades:
        symbol = getattr(t, "symbol", None) or (t.get("symbol") if isinstance(t, dict) else None)
        side   = getattr(t, "side",   None) or (t.get("side")   if isinstance(t, dict) else None)
        price  = getattr(t, "price",  None) or (t.get("price")  if isinstance(t, dict) else None)
        qty    = getattr(t, "quantity", 1)  or (t.get("quantity", 1) if isinstance(t, dict) else 1)
        if not symbol or not side or price is None:
            continue
        try:
            price = float(price)
            qty   = int(qty)
        except (TypeError, ValueError):
            continue
        if str(side).upper() == "BUY":
            buys[symbol].append((price, qty))
        elif str(side).upper() == "SELL":
            remaining = qty
            while remaining > 0 and buys[symbol]:
                buy_price, buy_qty = buys[symbol][0]
                matched = min(remaining, buy_qty)
                if price > buy_price:
                    wins += 1
                remaining -= matched
                if matched >= buy_qty:
                    buys[symbol].popleft()
                else:
                    buys[symbol][0] = (buy_price, buy_qty - matched)
                break  # one win/loss per sell fill
    return wins


def run_daily_pnl_report() -> None:
    logger.info("JOB: daily_pnl_report — recording daily P&L snapshot")
    try:
        from datetime import date
        from trading_platform.config import load_settings
        from trading_platform.data.persistence import TradingDatabase

        settings = load_settings()
        execution_mode = settings.execution_mode.value
        db = TradingDatabase()
        today = date.today()
        today_start = datetime.combine(today, dtime.min, tzinfo=timezone.utc)

        # Count today's trades, pushing the date filter into SQLite
        today_trades = db.trades(since=today_start, execution_mode=execution_mode, limit=2000)
        total = len(today_trades)

        # Compute wins by pairing BUY/SELL fills for each symbol using FIFO matching.
        # A "win" is a completed round-trip where the exit price > average entry price (BUY side)
        # or exit price < average entry price (SELL side).
        wins = _count_winning_trades(today_trades)

        snapshot = db.latest_snapshot(execution_mode=execution_mode)
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


def run_start_feed() -> None:
    logger.info("JOB: start_feed — starting live feed before market open")
    try:
        _api_post("/feed/start", b"{}", retries=3, timeout=8)
        logger.info("Live feed start requested via API")
    except Exception as exc:
        logger.warning("start_feed failed: %s", exc)


def run_stop_feed() -> None:
    logger.info("JOB: stop_feed — stopping live feed after market close")
    try:
        _api_post("/feed/stop", b"{}", retries=3, timeout=8)
        logger.info("Live feed stop requested via API")
    except Exception as exc:
        logger.warning("stop_feed failed: %s", exc)


def run_mcx_eod_square_off() -> None:
    logger.info("JOB: mcx_eod_square_off — squaring off open MCX commodity positions at 23:25 IST")
    try:
        from trading_platform.config import load_settings

        settings = load_settings()
        if settings.angel_one_configured:
            try:
                # Use the square-off API endpoint for commodity scope
                _api_post(
                    "/execution/square-off",
                    b'{"scope": "GLOBAL", "reason": "mcx_eod_squareoff_23:25"}',
                )
                logger.info("MCX EOD square-off triggered via API")
            except Exception as api_exc:
                logger.warning("Could not reach API for MCX EOD square-off: %s", api_exc)
    except Exception as exc:
        logger.error("mcx_eod_square_off failed: %s", exc)


def _retrain_bars() -> tuple[dict, str] | None:
    """Load candles for re-validation: real Angel One data, else the CSV cache.

    Synthetic bars are deliberately not an option here. Training on a random walk
    is a useful honesty check when you run it by hand — the gate must reject it —
    but a scheduled job doing it would file verdicts about noise alongside
    verdicts about the market, and nothing downstream would tell them apart.
    """
    from trading_platform.config import load_settings
    from scripts.train_return_forecaster import DEFAULT_SYMBOLS, HIST_DIR, fetch_angel, fetch_csv

    if load_settings().angel_one_configured:
        try:
            fresh = fetch_angel(DEFAULT_SYMBOLS, RETRAIN_DAYS, RETRAIN_INTERVAL)
        except Exception as exc:
            logger.warning("Angel One fetch failed (%s) — falling back to cached candles", exc)
            fresh = {}
        if fresh:
            return fresh, "angel"
        # Angel One's TOTP session has to be re-established daily and does fail —
        # expired session, clock drift, rate limits. Yesterday's candles are a far
        # better basis for tonight's verdict than skipping the run entirely.
        logger.warning("Angel One returned no series — falling back to cached candles")
    if any(HIST_DIR.glob(f"*__{RETRAIN_INTERVAL}.csv")):
        return fetch_csv(None, RETRAIN_INTERVAL), "csv_cache"
    return None


def run_model_retrain() -> None:
    """Re-validate the return forecaster against fresh candles after the close.

    Promotion is the validation gate's decision, never this job's: the artifact
    under models/ is written only when out-of-sample AUC clears
    0.5 + max(0.02, 2·SE_null) and accuracy beats the majority baseline.  A
    REJECTED verdict is a correct, expected outcome — daily-bar TA features have
    shown no out-of-sample edge on this data — so it is recorded and left alone.
    Nothing here retries a rejection with a weaker threshold.

    Every horizon's verdict is written to model_runs whether it passed or failed,
    so the record shows what was actually tried rather than only what survived.
    """
    logger.info("JOB: model_retrain — re-validating the return forecaster")
    try:
        from trading_platform.data.persistence import TradingDatabase
        from trading_platform.neural.return_forecaster import GradientBoostedReturnForecaster
        from scripts.train_return_forecaster import MODEL_PATH

        loaded = _retrain_bars()
        if loaded is None:
            logger.warning(
                "model_retrain skipped: no Angel One credentials and no cached candles under "
                "data/historical. Nothing real to train on — refusing to train on synthetic data."
            )
            return
        bars_by_symbol, source = loaded
        if not bars_by_symbol:
            logger.warning("model_retrain skipped: %s returned no usable series", source)
            return

        total_bars = sum(len(v) for v in bars_by_symbol.values())
        logger.info("Re-validating on %d symbols / %d bars from %s",
                    len(bars_by_symbol), total_bars, source)

        db = TradingDatabase()
        best: tuple[int, dict] | None = None
        for horizon in RETRAIN_HORIZONS:
            forecaster = GradientBoostedReturnForecaster(horizon=horizon)
            accepted = forecaster.train_pooled(bars_by_symbol)
            metrics = dict(forecaster.last_train_metrics or {})
            metrics["source"] = source
            db.save_model_run(
                model_name="return_forecaster",
                symbol="__POOLED__",
                signal="accepted" if accepted else "rejected",
                confidence=metrics.get("oos_auc"),
                metadata=metrics,
            )
            logger.info("  horizon=%-3d %s (AUC=%s)", horizon,
                        "ACCEPTED" if accepted else "rejected",
                        f"{metrics['oos_auc']:.4f}" if "oos_auc" in metrics else "n/a")
            if accepted and (best is None or metrics.get("oos_auc", 0.0) > best[1].get("oos_auc", 0.0)):
                best = (horizon, metrics)

        if best is None:
            logger.info(
                "model_retrain: no horizon showed validated edge — keeping the existing "
                "artifact (if any) and continuing to serve the MA baseline. This is the "
                "correct outcome, not a failure."
            )
            return

        # Refit the winning horizon and persist it. save() records accepted=True in
        # the metadata, which is what serving.py checks before it will load a model.
        best_horizon, best_metrics = best
        winner = GradientBoostedReturnForecaster(horizon=best_horizon)
        if not winner.train_pooled(bars_by_symbol):
            logger.warning("model_retrain: horizon %d did not re-validate on refit — not saving",
                           best_horizon)
            return
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        winner.save(str(MODEL_PATH))
        logger.info(
            "model_retrain: PROMOTED horizon=%d (OOS AUC=%.4f) -> %s. "
            "Restart the API to serve it; the running process loads models at startup.",
            best_horizon, best_metrics.get("oos_auc", float("nan")), MODEL_PATH,
        )
    except Exception as exc:
        logger.error("model_retrain failed: %s", exc, exc_info=True)


def _intraday_symbols() -> list[str]:
    """Symbols we can actually train on: everything cached, plus the rest if we have creds.

    fetch_angel() serves the .npz cache before it looks at credentials, so the
    cached basket is trainable offline. Asking it for an uncached symbol without
    credentials raises SystemExit, which would take the whole scheduler down —
    hence the split rather than always passing the full default list.
    """
    from trading_platform.config import load_settings  # noqa: F401  (see _intraday_symbols)
    from scripts.train_intraday_forecaster import CACHE, DEFAULT_SYMBOLS

    if load_settings().angel_one_configured:
        return list(DEFAULT_SYMBOLS)
    cached = sorted(p.name.split("__")[0]
                    for p in CACHE.glob(f"*__FIVE_MINUTE_{INTRADAY_DAYS}d.npz"))
    return cached


def run_intraday_retrain() -> None:
    """Re-validate the intraday (5-minute) forecaster after the close.

    Same gate and same promotion rule as run_model_retrain, on the other avenue:
    daily-bar TA is already known to have no out-of-sample edge, so intraday
    microstructure is where a real edge would have to come from. As of 2026-07-26
    it does not: horizons 3/6/12 scored OOS AUC 0.4934/0.4970/0.4948 against a
    0.5200 threshold on ~22k samples. Recording that verdict nightly is the point
    — a rejection that gets logged is how the honesty log stays true.

    Unlike the daily job this one runs without credentials, because fetch_angel
    serves the cached candles under data/intraday_research first.
    """
    logger.info("JOB: intraday_retrain — re-validating the intraday forecaster")
    try:
        from trading_platform.data.persistence import TradingDatabase
        from trading_platform.neural.intraday_forecaster import IntradayReturnForecaster
        from scripts.train_intraday_forecaster import MODEL_PATH, fetch_angel

        symbols = _intraday_symbols()
        if not symbols:
            logger.warning(
                "intraday_retrain skipped: no Angel One credentials and no cached 5-min "
                "candles under data/intraday_research — nothing real to train on."
            )
            return

        bars_by_symbol: dict[str, list[dict]] = {}
        for sym in symbols:
            try:
                bars = fetch_angel(sym, INTRADAY_DAYS, save_cache=True)
            except SystemExit as exc:      # uncached symbol, no credentials
                logger.warning("skip %s: %s", sym, exc)
                continue
            except Exception as exc:
                logger.warning("skip %s: %s", sym, str(exc)[:120])
                continue
            if len(bars) >= 200:
                bars_by_symbol[sym] = bars

        if not bars_by_symbol:
            logger.warning("intraday_retrain skipped: no symbol had enough 5-min bars")
            return

        total_bars = sum(len(v) for v in bars_by_symbol.values())
        logger.info("Re-validating intraday on %d symbols / %d 5-min bars",
                    len(bars_by_symbol), total_bars)

        db = TradingDatabase()
        best: tuple[int, dict] | None = None
        for horizon in INTRADAY_HORIZONS:
            forecaster = IntradayReturnForecaster(horizon=horizon)
            accepted = forecaster.train_pooled(bars_by_symbol)
            metrics = dict(forecaster.last_train_metrics or {})
            metrics["horizon_minutes"] = horizon * 5
            db.save_model_run(
                model_name="intraday_forecaster",
                symbol="__POOLED__",
                signal="accepted" if accepted else "rejected",
                confidence=metrics.get("oos_auc"),
                metadata=metrics,
            )
            logger.info("  horizon=%-3d (%2dmin) %s (AUC=%s)", horizon, horizon * 5,
                        "ACCEPTED" if accepted else "rejected",
                        f"{metrics['oos_auc']:.4f}" if "oos_auc" in metrics else "n/a")
            if accepted and (best is None or metrics.get("oos_auc", 0.0) > best[1].get("oos_auc", 0.0)):
                best = (horizon, metrics)

        if best is None:
            logger.info(
                "intraday_retrain: no horizon showed validated edge — nothing promoted. "
                "This is the correct outcome, not a failure."
            )
            return

        best_horizon, best_metrics = best
        winner = IntradayReturnForecaster(horizon=best_horizon)
        if not winner.train_pooled(bars_by_symbol):
            logger.warning("intraday_retrain: horizon %d did not re-validate on refit — not saving",
                           best_horizon)
            return
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        winner.save(str(MODEL_PATH))
        logger.info(
            "intraday_retrain: PROMOTED horizon=%d (%dmin, OOS AUC=%.4f) -> %s. Restart the API "
            "to serve it; it also needs AGENT_DIRECTIONAL_ENABLED=true to affect orders.",
            best_horizon, best_horizon * 5, best_metrics.get("oos_auc", float("nan")), MODEL_PATH,
        )
    except Exception as exc:
        logger.error("intraday_retrain failed: %s", exc, exc_info=True)


_JOB_FNS = {
    "instrument_refresh": run_instrument_refresh,
    "start_feed": run_start_feed,
    "eod_square_off": run_eod_square_off,
    "stop_feed": run_stop_feed,
    "daily_pnl_report": run_daily_pnl_report,
    "model_retrain": run_model_retrain,
    "intraday_retrain": run_intraday_retrain,
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
        try:
            _JOB_FNS[next_name]()
        finally:
            _checkpoint_sqlite_wal()

        # Brief pause so we don't re-trigger the same minute
        time.sleep(90)


if __name__ == "__main__":
    main()
