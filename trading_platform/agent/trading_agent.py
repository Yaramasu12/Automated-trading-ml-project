from __future__ import annotations

"""Autonomous trading agent — runs the full pipeline every scan_interval seconds.

Pipeline per cycle:
  1. Market hours check — skip if closed / entry not allowed
  2. Sync live tick prices → exit manager marks
  3. Decision pipeline scan for all underlyings
  4. Auto-enqueue every approved, non-duplicate candidate
  5. Publish activity summary to event bus → WebSocket → dashboard

Pre-market (09:00 IST):  refresh instruments + start live feed
EOD (15:25 IST):         square-off all open positions
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, timedelta
from typing import TYPE_CHECKING

from trading_platform.agent.market_hours import (
    is_entry_allowed,
    is_eod_squareoff,
    is_mcx_entry_allowed,
    is_mcx_eod_squareoff,
    is_premarket,
    is_trading_day,
    market_status,
    now_ist,
    seconds_to_next_open,
)
from trading_platform.domain.enums import OrderPriority, OrderType, ProductType
from trading_platform.domain.models import OrderIntent

if TYPE_CHECKING:
    from trading_platform.api.runtime import TradingRuntime

logger = logging.getLogger(__name__)

# ── Equity underlyings (NSE/BSE F&O) — session 09:15–15:20 IST ───────────────
EQUITY_UNDERLYINGS = [
    # NSE Indices (F&O on NFO — weekly expiry Thursday)
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
    # BSE Indices (F&O on BFO — SENSEX weekly Friday, BANKEX weekly Monday)
    "SENSEX", "BANKEX",
    # ── Nifty 50 equities with F&O ───────────────────────────────────────────
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN",
    "WIPRO", "KOTAKBANK", "AXISBANK", "MARUTI", "SUNPHARMA",
    "TATAMOTORS", "BAJFINANCE", "HINDUNILVR", "BHARTIARTL", "NTPC",
    "ASIANPAINT", "LTIM", "ONGC", "POWERGRID", "TITAN",
    "ITC", "LT", "HCLTECH", "M&M", "COALINDIA",
    "HEROMOTOCO", "HINDALCO", "JSWSTEEL", "ULTRACEMCO", "GRASIM",
    "BPCL", "CIPLA", "DRREDDY", "EICHERMOT",
    # ── Nifty 100 additions ───────────────────────────────────────────────────
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "TATACONSUM", "TRENT",
    "BAJAJFINSV", "DIVISLAB", "SHRIRAMFIN",
]

# ── MCX commodity underlyings — session 09:00–23:25 IST ──────────────────────
COMMODITY_UNDERLYINGS = [
    "GOLD", "GOLDM",            # precious metals
    "SILVER", "SILVERMIC",
    "CRUDEOIL", "CRUDEOILM",    # energy
    "NATURALGAS",
    "COPPER", "ZINC", "NICKEL", # base metals
]

SCAN_UNDERLYINGS = EQUITY_UNDERLYINGS + COMMODITY_UNDERLYINGS
COMMODITY_SET = set(COMMODITY_UNDERLYINGS)
DEFAULT_SCAN_INTERVAL = 300   # 5 minutes
MIN_SCAN_INTERVAL = 30        # 30 seconds (frontend can lower for demo)
LOOKBACK_DAYS = 60


@dataclass
class AgentCycleResult:
    ts: str
    market_status: str
    underlyings_scanned: list[str]
    total_candidates: int
    approved: int
    rejected: int
    enqueued: int
    skipped_existing: int
    regimes: dict[str, str]
    signals: list[dict]
    errors: list[str]
    duration_ms: int


@dataclass
class AgentState:
    running: bool = False
    scan_interval: int = DEFAULT_SCAN_INTERVAL
    scan_count: int = 0
    enqueued_total: int = 0
    last_cycle: AgentCycleResult | None = None
    last_cycle_ts: str | None = None
    premarket_done: bool = False
    premarket_date: date | None = None
    eod_done: bool = False
    mcx_eod_done: bool = False
    started_at: str | None = None
    activity_log: list[dict] = field(default_factory=list)


class TradingAgent:
    """Fully autonomous trading agent running inside the FastAPI event loop."""

    def __init__(self, runtime: "TradingRuntime", scan_interval: int = DEFAULT_SCAN_INTERVAL) -> None:
        self._runtime = runtime
        self._state = AgentState(scan_interval=max(scan_interval, MIN_SCAN_INTERVAL))
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    # ──────────────────────────────────────────────────────────────── lifecycle

    def start(self, scan_interval: int | None = None) -> dict:
        if self._state.running:
            return self.status()
        if scan_interval is not None:
            self._state.scan_interval = max(scan_interval, MIN_SCAN_INTERVAL)
        self._state.running = True
        self._state.premarket_done = False
        self._state.eod_done = False
        self._state.started_at = datetime.now(timezone.utc).isoformat()
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="trading-agent")
        logger.info("TradingAgent started (interval=%ds)", self._state.scan_interval)
        self._publish("agent.started", {"scan_interval": self._state.scan_interval})
        return self.status()

    def stop(self) -> dict:
        if not self._state.running:
            return self.status()
        self._state.running = False
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("TradingAgent stopped")
        self._publish("agent.stopped", {})
        return self.status()

    def set_interval(self, seconds: int) -> dict:
        self._state.scan_interval = max(seconds, MIN_SCAN_INTERVAL)
        return self.status()

    def status(self) -> dict:
        now = now_ist()
        s = self._state
        return {
            "running": s.running,
            "scan_interval": s.scan_interval,
            "scan_count": s.scan_count,
            "enqueued_total": s.enqueued_total,
            "started_at": s.started_at,
            "market_status": market_status(now),
            "ist_time": now.strftime("%H:%M:%S"),
            "is_trading_day": is_trading_day(now.date()),
            "last_cycle": self._cycle_to_dict(s.last_cycle) if s.last_cycle else None,
            "activity_log": s.activity_log[-30:],
        }

    # ──────────────────────────────────────────────────────────────── main loop

    async def _run(self) -> None:
        try:
            while self._state.running:
                await self._tick()
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._state.scan_interval,
                    )
                    break  # stop event fired
                except asyncio.TimeoutError:
                    pass   # normal — next scan cycle
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("TradingAgent fatal error: %s", exc, exc_info=True)
        finally:
            self._state.running = False

    async def _tick(self) -> None:
        now = now_ist()
        ms = market_status(now)
        self._log_activity(f"Tick — market: {ms}")

        # Reset premarket flag on a new trading day so the routine re-runs each morning
        today = now.date()
        if self._state.premarket_date != today:
            self._state.premarket_done = False
            self._state.premarket_date = today

        # Pre-market routine
        if is_premarket(now) and not self._state.premarket_done:
            await self._premarket_routine()
            self._state.premarket_done = True
            return

        # Equity EOD square-off (15:25–15:30 IST)
        if is_eod_squareoff(now) and not self._state.eod_done:
            await self._eod_routine()
            self._state.eod_done = True
            return

        # Reset equity EOD flag once the window passes
        if not is_eod_squareoff(now):
            self._state.eod_done = False

        # MCX commodity EOD square-off (23:25–23:30 IST)
        if is_mcx_eod_squareoff(now) and not self._state.mcx_eod_done:
            await self._mcx_eod_routine()
            self._state.mcx_eod_done = True
            return

        if not is_mcx_eod_squareoff(now):
            self._state.mcx_eod_done = False

        # Determine which markets are open right now
        equity_open = is_entry_allowed(now)
        mcx_open = is_mcx_entry_allowed(now)

        if not equity_open and not mcx_open:
            wait = seconds_to_next_open(now)
            self._log_activity(f"Market {ms} — sleeping until next open ({wait/3600:.1f}h)")
            return

        # Filter underlyings to whichever session(s) are active
        active_underlyings = []
        if equity_open:
            active_underlyings.extend(EQUITY_UNDERLYINGS)
        if mcx_open:
            active_underlyings.extend(COMMODITY_UNDERLYINGS)

        # Main trading cycle
        await self._scan_and_execute(active_underlyings)

    # ──────────────────────────────────────────────────────────────── routines

    async def _premarket_routine(self) -> None:
        self._log_activity("PRE-MARKET: refreshing instruments + starting live feed")
        try:
            if self._runtime.settings.angel_one_configured:
                await asyncio.to_thread(self._runtime.refresh_angel_one_instruments)
                count = len(self._runtime.instrument_master.instruments)
                self._log_activity(f"Instruments refreshed from Angel One ({count} instruments loaded)")

            # Stop existing feed so we can resubscribe with fresh tokens
            self._runtime.live_feed.stop()
            await asyncio.sleep(1)

            # Subscribe the full cash/index universe plus near-expiry futures for scan underlyings.
            cash_symbols = [
                instrument.symbol
                for instrument in self._runtime.instrument_master.all()
                if not instrument.is_derivative
            ]
            futures_symbols: list[str] = []
            today = date.today() if True else None  # avoid conditional import
            from datetime import date as _date
            today = _date.today()
            for underlying in SCAN_UNDERLYINGS:
                try:
                    fut = self._runtime.instrument_master.select_future(underlying, today)
                    futures_symbols.append(fut.symbol)
                except Exception:
                    pass

            all_feed_symbols = cash_symbols + futures_symbols
            self._runtime.start_live_feed(symbols=all_feed_symbols)
            self._log_activity(
                f"Live feed started: {len(cash_symbols)} cash + {len(futures_symbols)} futures symbols"
            )
            self._publish("agent.premarket_done", {
                "ts": now_ist().isoformat(),
                "cash_symbols": len(cash_symbols),
                "futures_symbols": len(futures_symbols),
            })
        except Exception as exc:
            logger.error("Pre-market routine error: %s", exc)
            self._log_activity(f"Pre-market error: {exc}", level="error")

    async def _eod_routine(self) -> None:
        self._log_activity("EOD: squaring off all equity positions (15:25 IST)")
        try:
            positions = self._runtime.portfolio.position_symbols()
            # Only square off equity positions (not MCX — those close at 23:25)
            equity_positions = [s for s in positions if s not in COMMODITY_SET]
            if equity_positions:
                result = await self._runtime.square_off_manager.square_off()
                self._log_activity(
                    f"EOD square-off issued for {result.get('positions_targeted', 0)} positions "
                    f"({result.get('intents_enqueued', 0)} enqueued)"
                )
            else:
                self._log_activity("EOD: no open equity positions to square off")
                result = {}
            self._publish("agent.eod_done", {"positions_squared": len(equity_positions)})
        except Exception as exc:
            logger.error("EOD routine error: %s", exc)
            self._log_activity(f"EOD error: {exc}", level="error")

    async def _mcx_eod_routine(self) -> None:
        self._log_activity("MCX EOD: squaring off all commodity positions (23:25 IST)")
        try:
            positions = self._runtime.portfolio.position_symbols()
            commodity_positions = [s for s in positions if s in COMMODITY_SET]
            if commodity_positions:
                result = await self._runtime.square_off_manager.square_off()
                self._log_activity(
                    f"MCX EOD square-off issued for {result.get('positions_targeted', 0)} positions "
                    f"({result.get('intents_enqueued', 0)} enqueued)"
                )
            else:
                self._log_activity("MCX EOD: no open commodity positions to square off")
                result = {}
            self._publish("agent.mcx_eod_done", {"positions_squared": len(commodity_positions)})
        except Exception as exc:
            logger.error("MCX EOD routine error: %s", exc)
            self._log_activity(f"MCX EOD error: {exc}", level="error")

    async def _scan_and_execute(self, active_underlyings: list[str] | None = None) -> None:
        now = now_ist()
        underlyings = active_underlyings or SCAN_UNDERLYINGS
        start_ms = _now_ms()
        cycle = AgentCycleResult(
            ts=now.strftime("%H:%M:%S"),
            market_status=market_status(now),
            underlyings_scanned=underlyings,
            total_candidates=0,
            approved=0,
            rejected=0,
            enqueued=0,
            skipped_existing=0,
            regimes={},
            signals=[],
            errors=[],
            duration_ms=0,
        )

        # Sync live prices to exit manager
        self._sync_marks_to_exit_manager()

        # Scan decision pipeline
        try:
            scan_start = date.today() - timedelta(days=LOOKBACK_DAYS)
            scans = await asyncio.to_thread(
                self._runtime.signal_scan,
                {
                    "underlyings": underlyings,
                    "start": scan_start.isoformat(),
                    "days": LOOKBACK_DAYS,
                },
            )
        except Exception as exc:
            cycle.errors.append(f"Scan error: {exc}")
            logger.error("Agent scan error: %s", exc)
            self._finish_cycle(cycle, start_ms)
            return

        # Current open positions — don't double-enter
        open_positions = set(self._runtime.portfolio.position_symbols())

        for scan in scans.get("scans", []):
            underlying = scan.get("underlying", "")
            cycle.regimes[underlying] = scan.get("regime", "unknown")

            for candidate in scan.get("candidates", []):
                cycle.total_candidates += 1
                rd = candidate.get("risk_decision") or {}
                if not rd.get("approved"):
                    cycle.rejected += 1
                    continue
                cycle.approved += 1

                sig = candidate.get("signal") or {}
                inst = candidate.get("instrument") or {}
                symbol = inst.get("symbol", underlying)

                # Skip if already in this position
                if symbol in open_positions or underlying in open_positions:
                    cycle.skipped_existing += 1
                    self._log_activity(f"Skip {symbol} — position already open")
                    continue

                # Auto-enqueue
                try:
                    enqueued = await self._enqueue_candidate(candidate, scan.get("regime", "UNKNOWN"))
                    if enqueued:
                        cycle.enqueued += 1
                        open_positions.add(symbol)
                        self._state.enqueued_total += 1
                        signal_info = {
                            "underlying": underlying,
                            "symbol": symbol,
                            "strategy": candidate.get("strategy_name", ""),
                            "side": sig.get("side", ""),
                            "confidence": sig.get("confidence", 0),
                            "price": sig.get("price", 0),
                            "reason": candidate.get("reason", ""),
                        }
                        cycle.signals.append(signal_info)
                        self._log_activity(
                            f"ENQUEUED {sig.get('side','')} {symbol} "
                            f"via {candidate.get('strategy_name','')} "
                            f"conf={sig.get('confidence',0):.0%}"
                        )
                except Exception as exc:
                    cycle.errors.append(f"Enqueue {symbol}: {exc}")
                    logger.error("Enqueue error for %s: %s", symbol, exc)

        self._finish_cycle(cycle, start_ms)

        # Periodically attempt to train the ML regime classifier from accumulated data
        if self._state.scan_count % 10 == 0:
            await asyncio.to_thread(self._try_train_regime_classifier)

    async def _enqueue_candidate(self, candidate: dict, regime: str) -> bool:
        """Build OrderIntent from candidate dict and enqueue it."""
        sig_d = candidate.get("signal")
        inst_d = candidate.get("instrument")
        if not sig_d or not inst_d:
            return False

        # Look up the live Instrument object from the instrument master
        symbol = inst_d.get("symbol", "")
        try:
            instrument = self._runtime.instrument_master.get(symbol)
        except KeyError:
            logger.warning("Agent: instrument %s not found in master — skipping", symbol)
            return False

        from trading_platform.domain.enums import Side
        from trading_platform.domain.models import Signal

        signal = Signal(
            strategy_name=sig_d.get("strategy_name", "agent"),
            symbol=symbol,
            side=Side(sig_d.get("side", "BUY")),
            confidence=float(sig_d.get("confidence", 0.5)),
            price=float(sig_d.get("price", 0)),
            reason=sig_d.get("reason", "agent_auto"),
            created_at=datetime.now(timezone.utc),
            metadata={"regime": regime, "agent_auto": True},
        )
        intent = OrderIntent(
            signal=signal,
            instrument=instrument,
            quantity=int(candidate.get("quantity", 1)),
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            priority=OrderPriority.ENTRY,
        )
        result = await self._runtime._enqueue_intent_with_controls(intent)
        return result.get("enqueued", False)

    # ──────────────────────────────────────────────────────────────── helpers

    def _try_train_regime_classifier(self) -> None:
        """Derive forward-return labels from feature store and train RegimeClassifier.

        Uses momentum_5 of record[i+FORWARD_STEPS] as a proxy for what the
        market actually did after record[i], giving externally-derived labels
        that the RegimeClassifier accepts (label_source != "rule_based").
        """
        try:
            fs = self._runtime.feature_store
            classifier = self._runtime.regime_classifier
            if fs is None or classifier is None:
                return

            FORWARD_STEPS = 5
            BULLISH_THRESHOLD = 0.02
            BEARISH_THRESHOLD = -0.015
            LOW_VOL_THRESHOLD = 0.01
            HIGH_VOL_THRESHOLD = 0.03

            records_by_symbol: dict[str, list[dict]] = {}
            for r in fs.all_records():
                sym = r.get("symbol", "")
                records_by_symbol.setdefault(sym, []).append(r)

            labeled: list[dict] = []
            for recs in records_by_symbol.values():
                recs.sort(key=lambda r: r.get("date", ""))
                for i, rec in enumerate(recs):
                    if i + FORWARD_STEPS >= len(recs):
                        continue
                    future_mom = recs[i + FORWARD_STEPS].get("momentum_5", 0.0)
                    vol = rec.get("realized_volatility", 0.0)
                    if future_mom > BULLISH_THRESHOLD or future_mom < BEARISH_THRESHOLD:
                        label = "TRENDING"
                    elif vol > HIGH_VOL_THRESHOLD:
                        label = "HIGH_VOLATILITY"
                    elif abs(vol) < LOW_VOL_THRESHOLD:
                        label = "MEAN_REVERTING"
                    else:
                        label = "BREAKOUT"
                    labeled.append({**rec, "regime": label})

            if len(labeled) < 8:
                logger.debug("Regime classifier: only %d labeled records — skipping", len(labeled))
                return

            accepted = classifier.train(labeled, label_source="forward_return")
            if accepted:
                self._log_activity(
                    f"RegimeClassifier trained on {len(labeled)} forward-return records — "
                    f"acc={classifier.last_train_metrics.get('holdout_accuracy', 0):.0%}"
                )
                logger.info("RegimeClassifier accepted — holdout %s", classifier.last_train_metrics)
            else:
                logger.debug("RegimeClassifier training rejected: %s", classifier.last_train_metrics)
        except Exception as exc:
            logger.warning("_try_train_regime_classifier: %s", exc)

    def _sync_marks_to_exit_manager(self) -> None:
        """Push latest live tick prices into the exit manager."""
        try:
            marks: dict[str, float] = {}
            for sym in self._runtime.instrument_master.instruments:
                tick = self._runtime.live_feed.latest_tick(sym)
                if tick and tick.last_price > 0:
                    marks[sym] = tick.last_price
            if marks:
                self._runtime.exit_manager.update_marks(marks)
        except Exception as exc:
            logger.warning("mark sync error: %s", exc)

    def _finish_cycle(self, cycle: AgentCycleResult, start_ms: int) -> None:
        cycle.duration_ms = _now_ms() - start_ms
        self._state.last_cycle = cycle
        self._state.last_cycle_ts = cycle.ts
        self._state.scan_count += 1
        self._log_activity(
            f"Scan #{self._state.scan_count}: "
            f"{cycle.approved} approved, {cycle.enqueued} enqueued, "
            f"{cycle.rejected} rejected — {cycle.duration_ms}ms"
        )
        self._publish("agent.cycle_done", self._cycle_to_dict(cycle))

    def _log_activity(self, msg: str, level: str = "info") -> None:
        entry = {
            "ts": now_ist().strftime("%H:%M:%S"),
            "msg": msg,
            "level": level,
        }
        self._state.activity_log.append(entry)
        if len(self._state.activity_log) > 200:
            self._state.activity_log = self._state.activity_log[-200:]
        if level == "error":
            logger.error("Agent: %s", msg)
        else:
            logger.info("Agent: %s", msg)

    def _publish(self, event_name: str, payload: dict) -> None:
        try:
            self._runtime.event_bus.publish(event_name, payload, "agent")
        except Exception:
            pass

    @staticmethod
    def _cycle_to_dict(cycle: AgentCycleResult | None) -> dict | None:
        if cycle is None:
            return None
        return {
            "ts": cycle.ts,
            "market_status": cycle.market_status,
            "underlyings_scanned": cycle.underlyings_scanned,
            "total_candidates": cycle.total_candidates,
            "approved": cycle.approved,
            "rejected": cycle.rejected,
            "enqueued": cycle.enqueued,
            "skipped_existing": cycle.skipped_existing,
            "regimes": cycle.regimes,
            "signals": cycle.signals,
            "errors": cycle.errors,
            "duration_ms": cycle.duration_ms,
        }


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
