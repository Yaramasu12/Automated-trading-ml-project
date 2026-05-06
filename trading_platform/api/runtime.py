from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

from trading_platform.ai.agents import ModelPerformance, RetrainingAgent, RiskSupervisorAgent
from trading_platform.ai.feature_store import FeatureStore
from trading_platform.ai.models import GARCHForecaster, MetaModel, ModelRegistry, RegimeClassifier, SentimentAnalyzer, VolatilityForecaster
from trading_platform.data.live_feed import LiveTickFeed
from trading_platform.data.persistence import TradingDatabase
from trading_platform.backtesting.charges import ChargesModel
from trading_platform.backtesting.engine import BacktestConfig, BacktestEngine
from trading_platform.backtesting.evaluator import StrategyEvaluator, WalkForwardEvaluator
from trading_platform.broker.angel_one import AngelOneBrokerClient
from trading_platform.broker.capability_registry import BrokerCapabilityRegistry
from trading_platform.broker.simulated import SimulatedBrokerClient
from trading_platform.config import Settings, load_settings
from trading_platform.data.angel_one_history import AngelOneHistoricalDataProvider
from trading_platform.data.angel_one_instruments import AngelOneInstrumentMasterProvider
from trading_platform.data.instrument_master import build_default_universe
from trading_platform.data.market_data import SyntheticDataProvider
from trading_platform.decision.pipeline import DecisionPipeline
from trading_platform.derivatives.engine import ContractSelector, ExpiryCalendar, GreeksCalculator, IVSurfaceBuilder, OptionChainBuilder, RolloverPlanner
from trading_platform.domain.enums import ExecutionMode, OptionType, OrderPriority, OrderType, ProductType, Side
from trading_platform.domain.models import OrderIntent, Signal
from trading_platform.event_bus import InMemoryEventBus
from trading_platform.execution.emergency_square_off import EmergencySquareOff
from trading_platform.execution.fill_processor import FillProcessor
from trading_platform.execution.lock_manager import InstrumentLockManager
from trading_platform.execution.multi_leg_manager import MultiLegOrderManager
from trading_platform.execution.oms_store import OMSEventStore
from trading_platform.execution.rate_limiter import TokenBucketRateLimiter
from trading_platform.execution.reconciliation import PositionReconciliation
from trading_platform.execution.router import ExecutionRouter
from trading_platform.execution.scheduler import ExecutionScheduler
from trading_platform.exit.exit_manager import ExitManager
from trading_platform.exit.exit_plan import ExitPlan
from trading_platform.goal.governance import GoalGovernance
from trading_platform.goal.scaling import PositionScaler
from trading_platform.governance.live_readiness import (
    InstrumentFreshnessTracker,
    LiveReadinessAggregator,
)
from trading_platform.monitoring.metrics import OperationalMonitor
from trading_platform.news.calendar import EconomicCalendar
from trading_platform.news.intelligence import NewsIntelligence
from trading_platform.portfolio.target import AnnualTargetTracker
from trading_platform.portfolio.ledger import PortfolioLedger
from trading_platform.risk.capital_protection import CapitalProtection
from trading_platform.risk.compliance import ComplianceGuard
from trading_platform.risk.engine import RiskEngine, RiskLimits
from trading_platform.risk.event_risk import EventRiskGuard
from trading_platform.risk.manual_approval import ManualApprovalGate
from trading_platform.strategies.factory import StrategyFactory
from trading_platform.agent.trading_agent import TradingAgent, SCAN_UNDERLYINGS


@dataclass
class RuntimeState:
    execution_mode: ExecutionMode
    live_armed: bool
    kill_switch_active: bool
    broker: str
    angel_one_configured: bool


class TradingRuntime:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()
        self.execution_mode = self.settings.execution_mode
        self.instrument_master = build_default_universe()
        self.angel_one_instruments = AngelOneInstrumentMasterProvider(self.settings)
        self.angel_one_history = AngelOneHistoricalDataProvider(self.settings)
        self.backtest_engine = BacktestEngine(self.instrument_master)
        self.strategy_evaluator = StrategyEvaluator(self.backtest_engine)
        self.strategy_factory = StrategyFactory()
        self.expiry_calendar = ExpiryCalendar(self.instrument_master)
        self.contract_selector = ContractSelector(self.instrument_master)
        self.option_chain_builder = OptionChainBuilder(self.instrument_master)
        self.greeks_calculator = GreeksCalculator()
        self.rollover_planner = RolloverPlanner(self.instrument_master)
        self.target_tracker = AnnualTargetTracker()
        self.portfolio = PortfolioLedger(self.settings.initial_capital)
        self.paper_broker = SimulatedBrokerClient()
        self.broker_capabilities = BrokerCapabilityRegistry()
        self.risk_engine = RiskEngine(
            RiskLimits(
                max_drawdown=self.settings.max_drawdown,
                max_daily_loss=self.settings.max_daily_loss,
                max_position_pct=self.settings.max_position_pct,
                max_margin_utilization=self.settings.max_margin_utilization,
            )
        )
        self.live_armed = False
        self.kill_switch_active = False
        self.retraining_agent = RetrainingAgent()
        self.risk_supervisor = RiskSupervisorAgent()
        self.volatility_forecaster = VolatilityForecaster()
        self.garch_forecaster = GARCHForecaster()
        self.sentiment_analyzer = SentimentAnalyzer()
        self.model_registry = ModelRegistry()
        self.regime_classifier = RegimeClassifier()
        self.meta_model = MetaModel()
        self._model_dir = "models"
        self.regime_classifier.load(f"{self._model_dir}/regime_classifier")
        self.meta_model.load(f"{self._model_dir}/meta_model.json")
        self.feature_store = FeatureStore()
        self.iv_surface_builder = IVSurfaceBuilder()
        # In-memory logs for dashboard visibility
        self._risk_rejection_log: list[dict] = []   # last 500 risk rejections
        self._agent_trade_log: list[dict] = []      # every entry/exit fill the agent sees
        self._entry_fill_context: dict[str, dict] = {}  # symbol -> {strategy, regime, entry_price}
        self.walk_forward_evaluator = WalkForwardEvaluator(self.backtest_engine)
        self.synthetic_data = SyntheticDataProvider()
        self.charges_model = ChargesModel()
        self.monitor = OperationalMonitor()
        self.db = TradingDatabase()
        self.live_feed = LiveTickFeed(self.settings)
        self.event_bus = InMemoryEventBus()
        self.news_intelligence = NewsIntelligence()
        # Live-readiness scaffolding: freshness tracker tells the gate
        # whether the instrument master is real, staleness tracker
        # tells the gate whether subscribed symbols are ticking.
        self.instrument_freshness = InstrumentFreshnessTracker()
        # Boot loaded synthetic universe — record it so the gate fails
        # until refresh_angel_one_instruments() succeeds.
        self.instrument_freshness.mark_synthetic(
            parsed_count=len(self.instrument_master.instruments)
        )
        self.live_readiness = LiveReadinessAggregator(
            instrument_freshness=self.instrument_freshness,
            feed_staleness=self.live_feed.staleness_tracker,
        )

        # Async execution layer
        self.oms = OMSEventStore()
        self.lock_manager = InstrumentLockManager()
        self.rate_limiter = TokenBucketRateLimiter()
        self.fill_processor = FillProcessor(self.portfolio, self.oms)
        self.reconciliation = PositionReconciliation(self.portfolio, self.oms)

        self.compliance = ComplianceGuard(max_orders_per_day=200)
        self.manual_approval = ManualApprovalGate(
            approval_threshold_notional=max(250_000.0, self.settings.initial_capital * 0.10)
        )
        self.capital_protection = CapitalProtection(
            max_position_pct=self.settings.max_position_pct,
            daily_loss_limit_pct=self.settings.max_daily_loss,
            drawdown_halt_pct=self.settings.max_drawdown,
        )
        self.economic_calendar = EconomicCalendar()
        self.event_risk = EventRiskGuard(buffer_days=1)
        self.event_risk.load_from_calendar(self.economic_calendar)

        self.scheduler = ExecutionScheduler(
            broker=self.paper_broker,
            oms=self.oms,
            fill_processor=self.fill_processor,
            lock_manager=self.lock_manager,
            rate_limiter=self.rate_limiter,
            compliance=self.compliance,
            capital_protection=self.capital_protection,
            event_risk=self.event_risk,
            portfolio=self.portfolio,
            event_bus=self.event_bus,
            charges_model=self.charges_model,
        )

        self.exit_manager = ExitManager(
            enqueue_fn=self.scheduler.enqueue,
            poll_interval=1.0,
        )
        self.multi_leg_manager = MultiLegOrderManager(self.scheduler.enqueue)
        self.square_off_manager = EmergencySquareOff(self.portfolio, self.scheduler.enqueue)

        # Register fill callback: create exit plan after every entry fill
        self.scheduler.register_fill_callback(self._on_fill)

        # Goal governance
        self.goal_governance = GoalGovernance(
            annual_target_pct=0.40,
            start_capital=self.settings.initial_capital,
            drawdown_halt_pct=self.settings.max_drawdown,
        )
        self.position_scaler = PositionScaler()

        self.monitor.record_event(
            "runtime_started",
            "Trading runtime initialized",
            metadata={"execution_mode": self.execution_mode.value, "broker": self.settings.broker},
        )
        self.event_bus.publish(
            "runtime.started.v1",
            {"execution_mode": self.execution_mode.value, "broker": self.settings.broker},
            "control",
        )
        self.decision_pipeline = DecisionPipeline(
            self.instrument_master,
            self.strategy_factory,
            self.risk_engine,
            self.portfolio,
            self.synthetic_data,
            live_feed=self.live_feed,
            history_provider=self.angel_one_history if self.settings.angel_one_configured else None,
            feature_store=self.feature_store,
            regime_classifier=self.regime_classifier,
            meta_model=self.meta_model,
        )
        self.agent = TradingAgent(self)

    def state(self) -> RuntimeState:
        return RuntimeState(
            execution_mode=self.execution_mode,
            live_armed=self.live_armed,
            kill_switch_active=self.kill_switch_active,
            broker=self.settings.broker,
            angel_one_configured=self.settings.angel_one_configured,
        )

    def state_payload(self) -> dict:
        state = self.state()
        return {
            "execution_mode": state.execution_mode.value,
            "live_armed": state.live_armed,
            "kill_switch_active": state.kill_switch_active,
            "broker": state.broker,
            "angel_one_configured": state.angel_one_configured,
            "live_order_confirmation_ready": self._can_submit_live_orders(),
        }

    def broker_client(self):
        if self.execution_mode.value.startswith("LIVE"):
            return AngelOneBrokerClient(self.settings)
        return self.paper_broker

    def set_execution_mode(self, mode: str) -> dict:
        next_mode = ExecutionMode(mode.upper())
        self.execution_mode = next_mode
        if not next_mode.value.startswith("LIVE"):
            self.live_armed = False
        # Hot-swap broker in the scheduler so it uses the right client
        self.scheduler.update_broker(self.broker_client())
        self.monitor.record_event("execution_mode_changed", f"Runtime mode set to {next_mode.value}")
        self.event_bus.publish("runtime.mode_changed.v1", self.state_payload(), "control")
        return self.state_payload()

    def arm_live(self, armed: bool) -> dict:
        if armed:
            readiness = self.live_readiness_payload()
            if not readiness["armed_eligible"]:
                reasons = ", ".join(readiness["blocking_reasons"]) or "unknown"
                raise ValueError(f"live_readiness_blocked: {reasons}")
        self.live_armed = armed
        self.monitor.record_event(
            "live_arm_changed",
            f"Live armed set to {armed}",
            severity="WARN" if armed else "INFO",
        )
        return self.state_payload()

    def live_readiness_payload(self) -> dict:
        """Evaluates the seven readiness gates and returns a dict
        suitable for ``GET /live/readiness``.
        """
        feed_snap = self.live_feed.snapshot()
        readiness = self.live_readiness.evaluate(
            execution_mode=self.execution_mode.value,
            live_trading_enabled=bool(self.settings.live_trading_enabled),
            real_money_confirmation=self.settings.live_order_confirmation or "",
            broker_configured=bool(self.settings.angel_one_configured),
            broker_name="angel_one" if self.settings.angel_one_configured else "simulated",
            kill_switch_active=self.kill_switch_active,
            feed_running=bool(feed_snap.get("running", False)),
            subscribed_symbols=feed_snap.get("subscribed_symbols", []),
        )
        return readiness.to_dict()

    def set_kill_switch(self, active: bool) -> dict:
        self.kill_switch_active = active
        self.scheduler.kill_switch_active = active
        if active:
            self.live_armed = False
        self.monitor.record_event(
            "kill_switch_changed",
            f"Kill switch set to {active}",
            severity="CRITICAL" if active else "INFO",
        )
        self.event_bus.publish(
            "kill_switch.triggered.v1" if active else "kill_switch.cleared.v1",
            {"active": active},
            "control",
        )
        return self.state_payload()

    async def _on_fill(self, trade, intent: OrderIntent) -> None:
        """Fill callback: creates ExitPlan on entry fills; records ML feedback on exit fills."""
        now_ts = datetime.now(timezone.utc).isoformat()
        symbol = intent.instrument.symbol
        strategy_name = intent.signal.strategy_name
        fill_price = getattr(trade, "price", None) or intent.signal.price
        side = intent.signal.side.value

        # ── Persist fill to DB + update monitor (both entry and exit) ──────
        exec_mode = self.execution_mode.value
        try:
            self.db.save_trade(trade, execution_mode=exec_mode)
            mark_prices: dict[str, float] = {}
            for sym in list(self.portfolio.positions.keys()):
                tick = self.live_feed.latest_tick(sym)
                if tick and tick.last_price > 0:
                    mark_prices[sym] = tick.last_price
            snap = self.portfolio.mark_to_market(datetime.now(timezone.utc), mark_prices)
            self.db.save_snapshot(snap, execution_mode=exec_mode)
            self.db.save_positions(self.portfolio.positions, execution_mode=exec_mode)
            self.monitor.record_order({"status": "FILLED"})
        except Exception as _persist_err:
            logger.warning("_on_fill DB persist error: %s", _persist_err)

        # ── Exit fill: compute P&L-based score → update MetaModel ──────────
        if intent.priority != OrderPriority.ENTRY:
            ctx = self._entry_fill_context.pop(symbol, None)
            if ctx:
                entry_price = ctx.get("entry_price", fill_price)
                regime = ctx.get("regime", "MEAN_REVERTING")
                strat = ctx.get("strategy", strategy_name)
                direction = 1 if ctx.get("side", "BUY") == "BUY" else -1
                pnl_pct = direction * (fill_price - entry_price) / max(entry_price, 1)
                # Normalise to [0,1] score: 0.5=breakeven, 1=+5%+, 0=−5%−
                score = min(1.0, max(0.0, 0.5 + pnl_pct * 10))
                try:
                    self.meta_model.update(regime, strat, score)
                except Exception:
                    pass
                self._agent_trade_log.append({
                    "ts": now_ts, "type": "EXIT", "symbol": symbol,
                    "strategy": strat, "regime": regime, "side": side,
                    "entry_price": entry_price, "exit_price": fill_price,
                    "pnl_pct": round(pnl_pct * 100, 2), "ml_score": round(score, 3),
                })
            else:
                self._agent_trade_log.append({
                    "ts": now_ts, "type": "EXIT", "symbol": symbol,
                    "strategy": strategy_name, "side": side, "exit_price": fill_price,
                })
            if len(self._agent_trade_log) > 500:
                self._agent_trade_log = self._agent_trade_log[-500:]
            # Remove persisted exit plan now that the position is closed
            try:
                self.db.delete_exit_plans_for_symbol(symbol, execution_mode=exec_mode)
            except Exception:
                pass
            return

        # ── Entry fill: record context for ML feedback later ────────────────
        self._entry_fill_context[symbol] = {
            "strategy": strategy_name,
            "regime": intent.signal.metadata.get("regime", "MEAN_REVERTING"),
            "entry_price": fill_price,
            "side": side,
        }
        self._agent_trade_log.append({
            "ts": now_ts, "type": "ENTRY", "symbol": symbol,
            "strategy": strategy_name, "side": side, "entry_price": fill_price,
            "quantity": intent.quantity,
        })
        if len(self._agent_trade_log) > 500:
            self._agent_trade_log = self._agent_trade_log[-500:]

        # ── Create ExitPlan — use strategy-specific exit rules and ATR stops ──
        expiry_date = intent.instrument.expiry
        # Read exit rules from the strategy that generated this signal
        strategy_name_raw = intent.signal.strategy_name.replace("exit_manager:", "")
        try:
            _strat = self.strategy_factory.get(strategy_name_raw)
            _rules = _strat.exit_rules()
            sl_pct = _rules.stop_loss_pct
            tgt_pct = _rules.target_pct
            trail_pct = sl_pct * 0.7   # trail at 70% of stop — locks in gains as price moves
        except Exception:
            sl_pct, tgt_pct, trail_pct = 0.015, 0.038, 0.010
        # ATR-based stop overrides percentage stop when available: 2× ATR is
        # adaptive to current volatility and avoids noise-induced exits
        atr_value = float(intent.signal.metadata.get("atr_14", 0.0))
        plan = ExitPlan.from_trade(
            trade,
            instrument=intent.instrument,
            stop_loss_pct=sl_pct,
            target_pct=tgt_pct,
            trailing_pct=trail_pct,
            expiry_date=expiry_date,
            atr=atr_value if atr_value > 0 else None,
            atr_stop_multiplier=2.0,
            partial_exit=True,   # book 50% at target, trail the rest for extended gains
        )
        self.exit_manager.register(plan)
        try:
            self.db.save_exit_plan(plan.to_dict(), execution_mode=exec_mode)
        except Exception:
            pass
        broker_name = getattr(self.broker_client(), "name", self.settings.broker)
        capabilities = self.broker_capabilities.get(broker_name)
        self.oms.append(
            event_type="exit_plan_created",
            order_id=intent.idempotency_key,
            symbol=symbol,
            metadata={
                **plan.to_dict(),
                "broker": broker_name,
                "gtt_mode": "broker_side" if capabilities.supports_gtt else "system_side",
                "oco_mode": "broker_side" if capabilities.supports_oco else "system_side",
                "trailing_stop_mode": "broker_side" if capabilities.supports_trailing_stop else "system_side",
            },
        )
        self.event_bus.publish(
            "position.protection_created.v1",
            {
                "symbol": plan.symbol,
                "strategy_name": plan.strategy_name,
                "gtt_supported": capabilities.supports_gtt,
                "oco_supported": capabilities.supports_oco,
            },
            "positions",
        )

    async def restore_state(self) -> None:
        """Reconstruct portfolio positions and exit plans from DB after a restart.

        Called once during lifespan startup so open positions and their
        stop-loss/target watches survive container restarts and redeployments.
        """
        exec_mode = self.execution_mode.value
        restored_positions = 0
        restored_plans = 0

        # ── Restore portfolio positions ────────────────────────────────────
        snapshot = self.db.latest_snapshot(execution_mode=exec_mode)
        if snapshot:
            self.portfolio.cash = snapshot["cash"]
            self.portfolio.peak_equity = max(self.portfolio.peak_equity, snapshot["equity"])

        from trading_platform.domain.models import Position
        for pos_data in self.db.load_positions(execution_mode=exec_mode):
            symbol = pos_data["symbol"]
            instrument = self.instrument_master.get(symbol)
            if instrument is None:
                logger.warning("restore_state: unknown instrument %s — position skipped", symbol)
                continue
            self.portfolio.positions[symbol] = Position(
                instrument=instrument,
                quantity=pos_data["quantity"],
                average_price=pos_data["average_price"],
                realized_pnl=pos_data["realized_pnl"],
            )
            restored_positions += 1

        # ── Restore active exit plans ──────────────────────────────────────
        for plan_data in self.db.load_exit_plans(execution_mode=exec_mode):
            symbol = plan_data["symbol"]
            instrument = self.instrument_master.get(symbol)
            if instrument is None:
                logger.warning("restore_state: unknown instrument %s — exit plan skipped", symbol)
                continue
            expiry = date.fromisoformat(plan_data["expiry_date"]) if plan_data.get("expiry_date") else None
            plan = ExitPlan(
                plan_id=plan_data["plan_id"],
                instrument=instrument,
                symbol=symbol,
                entry_price=plan_data["entry_price"],
                quantity=plan_data["quantity"],
                strategy_name=plan_data["strategy_name"],
                side=plan_data["side"],
                stop_loss_price=plan_data.get("stop_loss_price"),
                target_price=plan_data.get("target_price"),
                trailing_pct=plan_data.get("trailing_pct"),
                expiry_date=expiry,
                partial_exit_enabled=bool(plan_data.get("partial_exit_enabled", 0)),
            )
            self.exit_manager.register(plan)
            restored_plans += 1

        if restored_positions or restored_plans:
            logger.info(
                "restore_state: recovered %d position(s) and %d exit plan(s) for mode=%s",
                restored_positions, restored_plans, exec_mode,
            )
            self.monitor.record_event(
                "state_restored",
                f"Recovered {restored_positions} position(s) and {restored_plans} exit plan(s) from DB",
            )

    async def start_async_services(self) -> None:
        """Start scheduler, exit manager, live feed, and trading agent."""
        await self.scheduler.start()
        await self.exit_manager.start()
        if self.settings.angel_one_configured:
            self.start_live_feed()
        self.agent.start()
        await self.restore_state()
        self.monitor.record_event("async_services_started", "Scheduler, ExitManager, and Agent started")

    async def stop_async_services(self) -> None:
        """Stop scheduler and exit manager gracefully, then persist ML model state."""
        self.agent.stop()
        await self.scheduler.stop()
        await self.exit_manager.stop()
        self.live_feed.stop()
        try:
            import os as _os
            _os.makedirs(self._model_dir, exist_ok=True)
            self.meta_model.save(f"{self._model_dir}/meta_model.json")
            if self.regime_classifier.is_trained:
                self.regime_classifier.save(f"{self._model_dir}/regime_classifier")
        except Exception as _save_err:
            import logging as _log
            _log.getLogger(__name__).warning("Failed to persist ML models: %s", _save_err)
        self.monitor.record_event("async_services_stopped", "Scheduler and ExitManager stopped")

    # ------------------------------------------------------------------
    # Autonomous trading agent
    # ------------------------------------------------------------------

    def start_agent(self, scan_interval: int | None = None) -> dict:
        return self.agent.start(scan_interval=scan_interval)

    def stop_agent(self) -> dict:
        return self.agent.stop()

    def agent_status(self) -> dict:
        return self.agent.status()

    def set_agent_interval(self, seconds: int) -> dict:
        return self.agent.set_interval(seconds)

    def run_backtest(self, payload: dict | None = None) -> dict:
        payload = payload or {}
        underlyings = tuple(payload.get("underlyings") or SCAN_UNDERLYINGS)
        start_raw = payload.get("start", "2026-01-01")
        start = date.fromisoformat(start_raw) if isinstance(start_raw, str) else start_raw
        config = BacktestConfig(
            starting_capital=float(payload.get("starting_capital", self.settings.initial_capital)),
            start=start,
            days=int(payload.get("days", 30)),
            underlyings=underlyings,
            max_drawdown=float(payload.get("max_drawdown", self.settings.max_drawdown)),
            strategy_names=tuple(payload["strategy_names"]) if payload.get("strategy_names") else None,
        )
        result = self.backtest_engine.run(config)
        return result.to_dict()

    def universe(self) -> list[dict]:
        return self._serialize_universe(self.instrument_master)

    def data_status(self) -> dict:
        cache_path = self.angel_one_instruments.cache_path
        return {
            "instrument_source": "angel_one",
            "instrument_cache_path": str(cache_path),
            "instrument_cache_exists": cache_path.exists(),
            "current_universe_count": len(self.instrument_master.instruments),
            "current_universe_source": "runtime",
            "historical_data_requires_credentials": True,
            "angel_one_configured": self.settings.angel_one_configured,
        }

    def account_status(self) -> dict:
        return {
            "broker": self.settings.broker,
            "angel_one_configured": self.settings.angel_one_configured,
            "read_only_available": self.settings.angel_one_configured,
            "live_orders_possible": self._can_submit_live_orders(),
            "live_armed": self.live_armed,
            "kill_switch_active": self.kill_switch_active,
        }

    def account_snapshot(self) -> dict:
        if not self.settings.angel_one_configured:
            raise ValueError("Angel One credentials are required for account snapshot")
        snapshot = AngelOneBrokerClient(self.settings).read_only_snapshot()
        return {
            "broker": "ANGEL_ONE",
            "execution_mode": self.execution_mode.value,
            "live_orders_possible": self._can_submit_live_orders(),
            "live_armed": self.live_armed,
            "snapshot": snapshot,
        }

    def refresh_angel_one_instruments(self) -> dict:
        result = self.angel_one_instruments.refresh()
        self.instrument_master = self.angel_one_instruments.load_cached()
        self._rebuild_market_engines()
        # Record this as a real refresh so the readiness gate flips
        # from "synthetic" to "fresh".
        self.instrument_freshness.record_refresh(
            source=result.source,
            parsed_count=result.parsed_count,
        )
        return {
            "source": result.source,
            "cache_path": result.cache_path,
            "raw_count": result.raw_count,
            "parsed_count": result.parsed_count,
            "skipped_count": result.skipped_count,
        }

    def load_cached_angel_one_instruments(self) -> dict:
        self.instrument_master = self.angel_one_instruments.load_cached()
        self._rebuild_market_engines()
        parsed = len(self.instrument_master.instruments)
        # Cached load counts as a real refresh — the cache file came
        # from a real Angel One pull. Source stamps the cache path so
        # operators can tell which file was loaded.
        self.instrument_freshness.record_refresh(
            source=f"cache:{self.angel_one_instruments.cache_path}",
            parsed_count=parsed,
        )
        return {
            "cache_path": str(self.angel_one_instruments.cache_path),
            "parsed_count": parsed,
        }

    def historical_candles(self, payload: dict) -> dict:
        symbol = str(payload["symbol"])
        instrument = self.instrument_master.get(symbol)
        from_dt = datetime.fromisoformat(str(payload["from"]).replace("Z", "+00:00"))
        to_dt = datetime.fromisoformat(str(payload["to"]).replace("Z", "+00:00"))
        interval = str(payload.get("interval", "ONE_DAY"))
        bars = self.angel_one_history.get_candles(instrument, from_dt, to_dt, interval)
        return {
            "symbol": symbol,
            "interval": interval,
            "count": len(bars),
            "bars": [
                {
                    "timestamp": bar.timestamp.isoformat(),
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                }
                for bar in bars
            ],
        }

    def _serialize_universe(self, instrument_master) -> list[dict]:
        return [
            {
                "symbol": instrument.symbol,
                "exchange": instrument.exchange.value,
                "segment": instrument.segment.value,
                "type": instrument.instrument_type.value,
                "underlying": instrument.underlying,
                "expiry": instrument.expiry.isoformat() if instrument.expiry else None,
                "strike": instrument.strike,
                "option_type": instrument.option_type.value if instrument.option_type else None,
                "lot_size": instrument.lot_size,
            }
            for instrument in instrument_master.all()
        ]

    def _rebuild_market_engines(self) -> None:
        self.backtest_engine = BacktestEngine(self.instrument_master)
        self.strategy_evaluator = StrategyEvaluator(self.backtest_engine, self.strategy_factory)
        self.walk_forward_evaluator = WalkForwardEvaluator(self.backtest_engine)
        self.expiry_calendar = ExpiryCalendar(self.instrument_master)
        self.contract_selector = ContractSelector(self.instrument_master)
        self.option_chain_builder = OptionChainBuilder(self.instrument_master)
        self.rollover_planner = RolloverPlanner(self.instrument_master)
        self.decision_pipeline = DecisionPipeline(
            self.instrument_master,
            self.strategy_factory,
            self.risk_engine,
            self.portfolio,
            self.synthetic_data,
            live_feed=self.live_feed,
            history_provider=self.angel_one_history if self.settings.angel_one_configured else None,
            feature_store=self.feature_store,
            regime_classifier=self.regime_classifier,
            meta_model=self.meta_model,
        )

    def preview_order(self, payload: dict) -> dict:
        intent = self._intent_from_payload(payload)
        now = datetime.now(timezone.utc)
        mark_prices = self._mark_prices_for_intent(intent, payload)
        snapshot = self.portfolio.mark_to_market(now, mark_prices)
        decision = self.risk_engine.evaluate(
            intent=intent,
            portfolio=snapshot,
            now=now,
            execution_mode=self.execution_mode,
            live_armed=self.live_armed,
            kill_switch_active=self.kill_switch_active,
            daily_pnl=float(payload.get("daily_pnl", 0.0)),
            strategy_daily_pnl=float(payload.get("strategy_daily_pnl", 0.0)),
            options_short_exposure=float(payload.get("options_short_exposure", 0.0)),
            gamma_exposure=float(payload.get("gamma_exposure", 0.0)),
            symbol_exposure_pct=float(payload["symbol_exposure_pct"]) if payload.get("symbol_exposure_pct") is not None else None,
            correlated_exposure_pct=float(payload.get("correlated_exposure_pct", 0.0)),
            margin_utilization=float(payload.get("margin_utilization", 0.0)),
        )
        return {
            "mode": self.execution_mode.value,
            "approved": decision.approved,
            "reason": decision.reason,
            "risk_score": decision.risk_score,
            "intent": self._serialize_intent(intent),
            "portfolio": asdict(snapshot),
            "live_orders_possible": self._can_submit_live_orders(),
        }

    def simulate_order(self, payload: dict) -> dict:
        if self.execution_mode != ExecutionMode.PAPER:
            raise ValueError("Paper simulation requires runtime mode PAPER")
        intent = self._intent_from_payload(payload)
        now = datetime.now(timezone.utc)
        mark_prices = self._mark_prices_for_intent(intent, payload)
        router = ExecutionRouter(
            broker=self.paper_broker,
            risk_engine=self.risk_engine,
            portfolio=self.portfolio,
            execution_mode=self.execution_mode,
            live_armed=False,
            kill_switch_active=self.kill_switch_active,
        )
        report = router.submit(intent, now, mark_prices)
        self.monitor.record_order(self._serialize_order(report.order))
        self.monitor.record_event("paper_order", f"Paper order {report.order.status.value}", metadata={"symbol": intent.instrument.symbol})
        if report.trade:
            self.db.save_trade(report.trade, execution_mode="PAPER")
        snapshot = self.portfolio.mark_to_market(now, mark_prices)
        self.db.save_snapshot(snapshot, execution_mode="PAPER")
        self.db.save_risk_event(
            event_type="order_evaluated",
            reason=report.risk_decision.reason,
            symbol=intent.instrument.symbol,
            risk_score=report.risk_decision.risk_score,
            approved=report.risk_decision.approved,
        )
        return {
            "mode": self.execution_mode.value,
            "order": self._serialize_order(report.order),
            "risk_decision": asdict(report.risk_decision),
            "trade": self._serialize_trade(report.trade) if report.trade else None,
            "portfolio": asdict(snapshot),
        }

    async def enqueue_order(self, payload: dict) -> dict:
        intent = self._intent_from_payload(payload)
        return await self._enqueue_intent_with_controls(intent, payload)

    async def _enqueue_intent_with_controls(self, intent: OrderIntent, payload: dict | None = None) -> dict:
        payload = payload or {}
        if self._requires_manual_approval(intent) and not bool(payload.get("manual_approved", False)):
            expiry_seconds = int(payload.get("approval_expiry_seconds", 300))
            request = self.manual_approval.submit(intent, expiry_seconds=expiry_seconds)
            self.oms.append(
                event_type="manual_approval_requested",
                order_id=intent.idempotency_key,
                idempotency_key=intent.idempotency_key,
                symbol=intent.instrument.symbol,
                strategy_name=intent.signal.strategy_name,
                side=intent.signal.side.value,
                quantity=intent.quantity,
                price=intent.limit_price or intent.signal.price,
                priority=int(intent.priority),
                metadata={"request_id": request.request_id, "expires_at": request.expires_at.isoformat()},
            )
            self.event_bus.publish(
                "order.manual_approval_requested.v1",
                {
                    "request_id": request.request_id,
                    "idempotency_key": intent.idempotency_key,
                    "symbol": intent.instrument.symbol,
                    "notional_value": intent.notional_value,
                },
                "control",
            )
            return {
                "enqueued": False,
                "approval_required": True,
                "approval_request": request.to_dict(),
                "idempotency_key": intent.idempotency_key,
            }

        event_id = await self.scheduler.enqueue(intent)
        return {
            "enqueued": True,
            "approval_required": False,
            "event_id": event_id,
            "idempotency_key": intent.idempotency_key,
        }

    def _requires_manual_approval(self, intent: OrderIntent) -> bool:
        if intent.priority < OrderPriority.ENTRY:
            return False
        event_risk_state = self.event_risk.check()
        # LIVE_MANUAL_APPROVAL always gates every order through human review.
        if self.execution_mode == ExecutionMode.LIVE_MANUAL_APPROVAL:
            return True
        # Event-risk escalation requires approval regardless of source.
        if event_risk_state.recommended_action == "MANUAL_APPROVAL":
            return True
        # PAPER mode: agent-generated orders bypass notional threshold — no human
        # is available to approve paper trades and blocking them means the system
        # never generates fills, making the paper run useless.
        is_agent_auto = bool(intent.signal.metadata.get("agent_auto", False))
        if self.execution_mode == ExecutionMode.PAPER and is_agent_auto:
            return False
        # Notional-threshold check (applies to PAPER manual orders and all LIVE orders).
        if self.manual_approval.requires_approval(intent):
            return True
        # Plain LIVE mode: agent-generated orders bypass the blanket manual gate.
        if self.execution_mode == ExecutionMode.LIVE:
            return not is_agent_auto
        return False

    def manual_approval_status(self) -> dict:
        pending = self.manual_approval.pending_requests()
        return {
            "pending_count": len(pending),
            "approval_threshold_notional": self.manual_approval.approval_threshold_notional,
            "pending": pending,
        }

    async def approve_order(self, request_id: str, payload: dict | None = None) -> dict:
        payload = payload or {}
        request = self.manual_approval.approve(request_id, str(payload.get("approval_reason", "")))
        if request.intent is None:
            raise ValueError("Approval request has no order intent")
        self.oms.append(
            event_type="manual_approval_approved",
            order_id=request.intent.idempotency_key,
            idempotency_key=request.intent.idempotency_key,
            symbol=request.intent.instrument.symbol,
            strategy_name=request.intent.signal.strategy_name,
            metadata={"request_id": request.request_id, "reviewer_note": request.reviewer_note},
        )
        self.event_bus.publish(
            "order.manual_approval_approved.v1",
            {"request_id": request.request_id, "idempotency_key": request.intent.idempotency_key},
            "control",
        )
        enqueued = await self._enqueue_intent_with_controls(request.intent, {"manual_approved": True})
        return {"approved": True, "request": request.to_dict(), "enqueue": enqueued}

    def reject_order(self, request_id: str, payload: dict | None = None) -> dict:
        payload = payload or {}
        request = self.manual_approval.reject(request_id, str(payload.get("reason", "")))
        if request.intent is not None:
            self.oms.append(
                event_type="manual_approval_rejected",
                order_id=request.intent.idempotency_key,
                idempotency_key=request.intent.idempotency_key,
                symbol=request.intent.instrument.symbol,
                strategy_name=request.intent.signal.strategy_name,
                rejection_reason=request.reviewer_note,
                metadata={"request_id": request.request_id},
            )
        self.event_bus.publish(
            "order.manual_approval_rejected.v1",
            {"request_id": request.request_id},
            "control",
        )
        return {"rejected": True, "request": request.to_dict()}

    async def submit_multi_leg(self, payload: dict) -> dict:
        raw_legs = payload.get("legs") or []
        if len(raw_legs) < 2:
            raise ValueError("multi-leg order requires at least two legs")
        strategy_name = str(payload.get("strategy_name", "multi_leg_strategy"))
        intents = []
        group_id = str(payload.get("group_id") or f"ml_{datetime.now(timezone.utc).timestamp():.0f}")
        for index, raw_leg in enumerate(raw_legs, start=1):
            leg_payload = dict(raw_leg)
            leg_payload.setdefault("strategy_name", strategy_name)
            leg_payload.setdefault("priority", "PROTECTIVE_MULTI_LEG" if index == 1 else "HEDGE")
            metadata = dict(leg_payload.get("metadata") or {})
            metadata.update({"multi_leg_group": group_id, "leg_index": index})
            leg_payload["metadata"] = metadata
            intents.append(self._intent_from_payload(leg_payload))

        combined_notional = sum(intent.notional_value for intent in intents)
        if self.execution_mode.value.startswith("LIVE") and not bool(payload.get("manual_approved", False)):
            requests = [
                self.manual_approval.submit(intent, expiry_seconds=int(payload.get("approval_expiry_seconds", 300)))
                for intent in intents
            ]
            for request in requests:
                if request.intent:
                    self.oms.append(
                        event_type="manual_approval_requested",
                        order_id=request.intent.idempotency_key,
                        idempotency_key=request.intent.idempotency_key,
                        symbol=request.intent.instrument.symbol,
                        strategy_name=request.intent.signal.strategy_name,
                        metadata={"request_id": request.request_id, "multi_leg_group": group_id},
                    )
            return {
                "submitted": False,
                "approval_required": True,
                "group_id": group_id,
                "combined_notional": combined_notional,
                "approval_requests": [request.to_dict() for request in requests],
            }

        self.oms.append(
            event_type="multi_leg_created",
            order_id=group_id,
            metadata={"strategy_name": strategy_name, "leg_count": len(intents), "combined_notional": combined_notional},
        )
        result = await self.multi_leg_manager.submit(intents, strategy_name=strategy_name)
        self.oms.append(
            event_type="multi_leg_rolled_back" if result.rolled_back else "multi_leg_completed",
            order_id=result.order_id,
            metadata=result.to_dict(),
        )
        self.event_bus.publish(
            "multi_leg.completed.v1",
            result.to_dict(),
            "execution",
        )
        return {"submitted": True, "approval_required": False, "multi_leg_order": result.to_dict()}

    async def square_off(self, payload: dict | None = None) -> dict:
        payload = payload or {}
        from trading_platform.domain.enums import SquareOffScope

        scope = SquareOffScope(str(payload.get("scope", "GLOBAL")).upper())
        result = await self.square_off_manager.square_off(
            scope=scope,
            strategy_name=payload.get("strategy_id") or payload.get("strategy_name"),
            symbol=str(payload["symbol"]).upper() if payload.get("symbol") else None,
        )
        self.oms.append(
            event_type="square_off_requested",
            order_id=f"square_off:{result['timestamp']}",
            metadata={**result, "reason": payload.get("reason", "")},
        )
        self.event_bus.publish("square_off.requested.v1", result, "control")
        return result

    def broker_capability_status(self) -> dict:
        broker_name = getattr(self.broker_client(), "name", self.settings.broker)
        return {
            "active_broker": broker_name,
            "active_capabilities": self.broker_capabilities.get(broker_name).__dict__,
            "brokers": self.broker_capabilities.all_brokers(),
        }

    def event_bus_summary(self) -> dict:
        return self.event_bus.summary()

    def event_bus_events(self, limit: int = 100, stream: str | None = None) -> dict:
        events = self.event_bus.recent(limit=limit, stream=stream)
        return {"count": len(events), "events": events, "stream": stream}

    def _intent_from_payload(self, payload: dict) -> OrderIntent:
        symbol = str(payload["symbol"]).upper()
        instrument = self.instrument_master.get(symbol)
        side = Side(str(payload.get("side", "BUY")).upper())
        price = float(payload.get("price") or payload.get("limit_price") or 0)
        if price <= 0:
            raise ValueError("price must be positive")
        quantity = int(payload.get("quantity", 1))
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        metadata = dict(payload.get("metadata") or {})
        signal = Signal(
            strategy_name=str(payload.get("strategy_name", "manual_preview")),
            symbol=instrument.symbol,
            side=side,
            confidence=float(payload.get("confidence", 1.0)),
            price=price,
            reason=str(payload.get("reason", "manual order preview")),
            created_at=datetime.now(timezone.utc),
            metadata=metadata,
        )
        priority = payload.get("priority", OrderPriority.ENTRY)
        if isinstance(priority, str):
            priority = OrderPriority[priority.upper()] if not priority.isdigit() else OrderPriority(int(priority))
        elif isinstance(priority, int):
            priority = OrderPriority(priority)
        return OrderIntent(
            signal=signal,
            instrument=instrument,
            quantity=quantity,
            order_type=OrderType(str(payload.get("order_type", "MARKET")).upper()),
            product_type=ProductType(str(payload.get("product_type", "INTRADAY")).upper()),
            limit_price=float(payload["limit_price"]) if payload.get("limit_price") is not None else None,
            stop_loss=float(payload["stop_loss"]) if payload.get("stop_loss") is not None else None,
            target=float(payload["target"]) if payload.get("target") is not None else None,
            priority=priority,
        )

    def _serialize_intent(self, intent: OrderIntent) -> dict:
        return {
            "symbol": intent.instrument.symbol,
            "exchange": intent.instrument.exchange.value,
            "segment": intent.instrument.segment.value,
            "side": intent.signal.side.value,
            "quantity": intent.quantity,
            "lot_size": intent.instrument.lot_size,
            "price": intent.limit_price or intent.signal.price,
            "notional_value": intent.notional_value,
            "order_type": intent.order_type.value,
            "product_type": intent.product_type.value,
            "strategy_name": intent.signal.strategy_name,
            "priority": intent.priority.name,
            "idempotency_key": intent.idempotency_key,
        }

    def _serialize_trade(self, trade) -> dict:
        return {
            "trade_id": trade.trade_id,
            "order_id": trade.order_id,
            "symbol": trade.symbol,
            "side": trade.side.value,
            "quantity": trade.quantity,
            "price": trade.price,
            "charges": trade.charges,
            "timestamp": trade.timestamp.isoformat(),
            "strategy_name": trade.strategy_name,
        }

    def _serialize_order(self, order) -> dict:
        return {
            "status": order.status.value,
            "broker_order_id": order.broker_order_id,
            "average_price": order.average_price,
            "rejection_reason": order.rejection_reason,
            "latency_ms": order.latency_ms,
            "symbol": order.intent.instrument.symbol,
            "strategy_name": order.intent.signal.strategy_name,
            "side": order.intent.signal.side.value,
            "quantity": order.intent.quantity,
        }

    def _mark_prices_for_intent(self, intent: OrderIntent, payload: dict) -> dict[str, float]:
        marks = dict(payload.get("mark_prices") or {})
        price = intent.limit_price or intent.signal.price
        marks[intent.instrument.symbol] = price
        if intent.instrument.underlying:
            marks.setdefault(intent.instrument.underlying, float(payload.get("underlying_price", price)))
        return marks

    def _can_submit_live_orders(self) -> bool:
        return (
            self.execution_mode.value.startswith("LIVE")
            and self.settings.live_trading_enabled
            and self.settings.live_order_confirmation == "I_ACCEPT_REAL_MONEY_LIVE_ORDERS"
            and self.settings.angel_one_configured
        )

    def strategy_catalog(self) -> dict:
        catalog = self.strategy_factory.catalog()
        by_family: dict[str, int] = {}
        for strategy in catalog:
            by_family[strategy["family"]] = by_family.get(strategy["family"], 0) + 1
        return {
            "count": len(catalog),
            "by_family": by_family,
            "strategies": catalog,
        }

    def evaluate_strategies(self, payload: dict | None = None) -> dict:
        payload = payload or {}
        underlyings = tuple(payload.get("underlyings") or SCAN_UNDERLYINGS)
        start_raw = payload.get("start", "2026-01-01")
        start = date.fromisoformat(start_raw) if isinstance(start_raw, str) else start_raw
        names = tuple(payload["strategy_names"]) if payload.get("strategy_names") else None
        result = self.strategy_evaluator.evaluate(
            start=start,
            days=int(payload.get("days", 30)),
            underlyings=underlyings,
            starting_capital=float(payload.get("starting_capital", self.settings.initial_capital)),
            max_drawdown=float(payload.get("max_drawdown", self.settings.max_drawdown)),
            strategy_names=names,
        )
        payload = result.to_dict()
        payload["selection_policy"] = {
            "ranking_inputs": ["return_pct", "profit_factor", "sharpe_like", "max_drawdown"],
            "live_rule": "Only candidates with stable paper/live shadow metrics may progress to controlled live.",
        }
        return payload

    def signal_scan(self, payload: dict | None = None) -> dict:
        payload = payload or {}
        underlyings = [str(item).upper() for item in payload.get("underlyings", SCAN_UNDERLYINGS)]
        _default_start = (date.today() - timedelta(days=60)).isoformat()
        start = date.fromisoformat(str(payload.get("start", _default_start)))
        days = int(payload.get("days", 60))
        strategy_names = [str(item) for item in payload["strategy_names"]] if payload.get("strategy_names") else None
        # Allow caller to override execution_mode (e.g. BACKTEST to bypass market-hours gate)
        mode_override = payload.get("execution_mode")
        try:
            scan_mode = ExecutionMode(mode_override) if mode_override else self.execution_mode
        except ValueError:
            scan_mode = self.execution_mode
        scans = [
            self.decision_pipeline.scan(
                underlying=underlying,
                start=start,
                days=days,
                execution_mode=scan_mode,
                live_armed=self.live_armed,
                kill_switch_active=self.kill_switch_active,
                strategy_names=strategy_names,
            ).to_dict()
            for underlying in underlyings
        ]
        approved = 0
        rejected = 0
        now_ts = datetime.now(timezone.utc).isoformat()
        for scan in scans:
            for candidate in scan["candidates"]:
                rd = candidate.get("risk_decision")
                if not rd:
                    continue
                if rd["approved"]:
                    approved += 1
                else:
                    rejected += 1
                    self._risk_rejection_log.append({
                        "ts": now_ts,
                        "underlying": scan.get("underlying", ""),
                        "symbol": (candidate.get("instrument") or {}).get("symbol", ""),
                        "strategy": candidate.get("strategy_name", ""),
                        "reason": rd.get("reason", ""),
                        "risk_score": rd.get("risk_score", 0),
                        "regime": scan.get("regime", ""),
                    })
        if len(self._risk_rejection_log) > 500:
            self._risk_rejection_log = self._risk_rejection_log[-500:]
        return {
            "mode": self.execution_mode.value,
            "submitted_orders": 0,
            "approved_candidates": approved,
            "rejected_candidates": rejected,
            "scans": scans,
        }

    def shadow_run(self, payload: dict | None = None) -> dict:
        if self.execution_mode != ExecutionMode.PAPER:
            raise ValueError("Shadow paper run requires runtime mode PAPER")
        payload = payload or {}
        underlyings = [str(item).upper() for item in payload.get("underlyings", SCAN_UNDERLYINGS)]
        _default_start = (date.today() - timedelta(days=60)).isoformat()
        start = date.fromisoformat(str(payload.get("start", _default_start)))
        days = int(payload.get("days", 60))
        strategy_names = [str(item) for item in payload["strategy_names"]] if payload.get("strategy_names") else None
        scans = [
            self.decision_pipeline.scan(
                underlying=underlying,
                start=start,
                days=days,
                execution_mode=ExecutionMode.PAPER,
                live_armed=False,
                kill_switch_active=self.kill_switch_active,
                strategy_names=strategy_names,
            )
            for underlying in underlyings
        ]
        router = ExecutionRouter(
            broker=self.paper_broker,
            risk_engine=self.risk_engine,
            portfolio=self.portfolio,
            execution_mode=ExecutionMode.PAPER,
            live_armed=False,
            kill_switch_active=self.kill_switch_active,
        )
        executions = []
        for scan in scans:
            for candidate in scan.candidates:
                if not candidate.signal or not candidate.risk_decision or not candidate.risk_decision.approved:
                    continue
                base_intent = OrderIntent(
                    signal=candidate.signal,
                    instrument=candidate.instrument,
                    quantity=candidate.quantity,
                    order_type=OrderType.MARKET,
                    product_type=ProductType.INTRADAY,
                    priority=OrderPriority.ENTRY,
                )
                snapshot_for_scale = self.portfolio.mark_to_market(
                    scan.as_of, {candidate.instrument.symbol: candidate.signal.price}
                )
                goal_state = self.goal_governance.evaluate(
                    current_equity=snapshot_for_scale.equity,
                    drawdown=snapshot_for_scale.drawdown,
                    as_of=scan.as_of.date() if hasattr(scan.as_of, "date") else scan.as_of,
                )
                intent = self.position_scaler.scale(base_intent, goal_state)
                mark_prices = {
                    scan.underlying: candidate.signal.price,
                    candidate.instrument.symbol: candidate.signal.price,
                }
                charges = self.charges_model.estimate(intent, candidate.signal.price)
                report = router.submit(intent, scan.as_of, mark_prices, charges)
                executions.append(
                    {
                        "underlying": scan.underlying,
                        "order": self._serialize_order(report.order),
                        "risk_decision": asdict(report.risk_decision),
                        "trade": self._serialize_trade(report.trade) if report.trade else None,
                    }
                )
        self.monitor.record_orders([item["order"] for item in executions])
        self.monitor.record_event(
            "shadow_run",
            "Shadow paper run completed",
            metadata={"submitted_orders": len(executions), "underlyings": underlyings},
        )
        filled = sum(1 for item in executions if item["order"]["status"] == "FILLED")
        rejected = sum(1 for item in executions if item["order"]["status"] in {"REJECTED", "RISK_REJECTED"})
        latencies = [
            item["order"]["latency_ms"]
            for item in executions
            if item["order"]["latency_ms"] is not None
        ]
        snapshot = self.portfolio.mark_to_market(datetime.now(timezone.utc), self._shadow_marks(scans))
        return {
            "mode": self.execution_mode.value,
            "submitted_orders": len(executions),
            "filled_orders": filled,
            "rejected_orders": rejected,
            "rejection_rate": rejected / len(executions) if executions else 0.0,
            "average_latency_ms": sum(latencies) / len(latencies) if latencies else 0.0,
            "scans": [scan.to_dict() for scan in scans],
            "executions": executions,
            "portfolio": asdict(snapshot),
        }

    def monitoring_metrics(self) -> dict:
        return self.monitor.snapshot(
            execution_mode=self.execution_mode.value,
            live_armed=self.live_armed,
            kill_switch_active=self.kill_switch_active,
            stale_market_data=False,
        ).to_dict()

    def monitoring_events(self, limit: int = 20) -> dict:
        return {
            "count": len(self.monitor.events),
            "events": self.monitor.recent_events(limit),
        }

    def _shadow_marks(self, scans) -> dict[str, float]:
        marks = {}
        for scan in scans:
            marks[scan.underlying] = scan.features.close
            for candidate in scan.candidates:
                if candidate.signal:
                    marks[candidate.instrument.symbol] = candidate.signal.price
        return marks

    def expiries(self, underlying: str) -> dict:
        today = date.today()
        expiries = self.expiry_calendar.expiries(underlying.upper(), today)
        return {
            "underlying": underlying.upper(),
            "count": len(expiries),
            "expiries": [expiry.isoformat() for expiry in expiries],
            "nearest": expiries[0].isoformat() if expiries else None,
        }

    def option_chain(self, underlying: str, expiry: str | None = None, spot_price: float | None = None) -> dict:
        underlying = underlying.upper()
        if spot_price is None:
            tick = self.live_feed.latest_tick(underlying)
            if tick and tick.last_price > 0:
                spot_price = tick.last_price
        spot = float(spot_price or 0.0)
        expiry_date = date.fromisoformat(expiry) if expiry else self.expiry_calendar.nearest(underlying, date.today())
        chain = self.option_chain_builder.build(underlying, expiry_date)
        dte = max(1, (expiry_date - date.today()).days)
        liquid_strikes = chain.liquid_strikes(spot) if spot else chain.strikes

        def enrich(instrument, option_type: str) -> dict:
            payload = self._serialize_instrument(instrument)
            tick = self.live_feed.latest_tick(instrument.symbol)
            ltp = tick.last_price if tick and tick.last_price > 0 else None
            delta = None
            if spot > 0 and instrument.strike:
                try:
                    greeks = self.calculate_greeks(
                        {
                            "spot_price": spot,
                            "strike": instrument.strike,
                            "days_to_expiry": dte,
                            "volatility": 0.18,
                            "option_type": option_type,
                        }
                    )
                    delta = round(float(greeks.get("delta", 0.0)), 3)
                except Exception:
                    delta = None
            payload["ltp"] = ltp
            payload["delta"] = delta
            payload["live"] = ltp is not None
            return payload

        calls = [enrich(instrument, "CE") for instrument in chain.calls]
        puts = [enrich(instrument, "PE") for instrument in chain.puts]
        calls_by_strike = {item["strike"]: item for item in calls}
        puts_by_strike = {item["strike"]: item for item in puts}
        rows = [
            {
                "strike": strike,
                "call": calls_by_strike.get(strike),
                "put": puts_by_strike.get(strike),
            }
            for strike in chain.strikes
            if not liquid_strikes or strike in liquid_strikes
        ]
        option_symbols = [
            leg["symbol"]
            for row in rows
            for leg in (row.get("call"), row.get("put"))
            if leg and leg.get("symbol")
        ]
        if option_symbols:
            self.live_feed.add_subscriptions(option_symbols)
        return {
            "underlying": chain.underlying,
            "expiry": chain.expiry.isoformat(),
            "dte": dte,
            "spot_price": spot,
            "call_count": len(chain.calls),
            "put_count": len(chain.puts),
            "strikes": chain.strikes,
            "liquid_strikes": liquid_strikes,
            "calls": calls,
            "puts": puts,
            "rows": rows,
        }

    def calculate_greeks(self, payload: dict) -> dict:
        greeks = self.greeks_calculator.calculate(
            spot_price=float(payload["spot_price"]),
            strike=float(payload["strike"]),
            days_to_expiry=int(payload["days_to_expiry"]),
            volatility=float(payload.get("volatility", 0.20)),
            option_type=OptionType(str(payload.get("option_type", "CE")).upper()),
            risk_free_rate=float(payload.get("risk_free_rate", 0.06)),
        )
        return asdict(greeks)

    def target_progress(self, payload: dict | None = None) -> dict:
        payload = payload or {}
        start_raw = payload.get("start_date", "2026-01-01")
        as_of_raw = payload.get("as_of", date.today().isoformat())
        progress = self.target_tracker.evaluate(
            start_date=date.fromisoformat(start_raw),
            as_of=date.fromisoformat(as_of_raw),
            start_capital=float(payload.get("start_capital", self.settings.initial_capital)),
            current_equity=float(payload.get("current_equity", self.portfolio.cash)),
            drawdown=float(payload.get("drawdown", 0.0)),
            profit_factor=float(payload.get("profit_factor", 1.0)),
            sharpe=float(payload.get("sharpe", 0.0)),
        )
        return asdict(progress)

    def supervisor_decision(self, payload: dict) -> dict:
        performance = None
        if payload.get("model_performance"):
            model_payload = payload["model_performance"]
            performance = ModelPerformance(
                model_name=model_payload.get("model_name", "candidate"),
                profit_factor=float(model_payload.get("profit_factor", 1.0)),
                sharpe=float(model_payload.get("sharpe", 0.0)),
                drawdown=float(model_payload.get("drawdown", 0.0)),
                iv_interval_coverage=float(model_payload.get("iv_interval_coverage", 0.95)),
                sentiment_precision=float(model_payload.get("sentiment_precision", 0.90)),
                sample_size=int(model_payload.get("sample_size", 0)),
                feature_drift_score=float(model_payload.get("feature_drift_score", 0.0)),
            )
        decision = self.risk_supervisor.decide(
            drawdown=float(payload.get("drawdown", 0.0)),
            daily_loss_pct=float(payload.get("daily_loss_pct", 0.0)),
            rejection_rate=float(payload.get("rejection_rate", 0.0)),
            stale_market_data=bool(payload.get("stale_market_data", False)),
            model_performance=performance,
        )
        return asdict(decision)

    def option_chain_live(self, underlying: str, expiry: str | None = None, spot_price: float | None = None) -> dict:
        """Option chain enriched with live tick prices and BS-calculated greeks."""
        underlying = underlying.upper()
        # Use live feed spot if not provided
        if spot_price is None:
            tick = self.live_feed.latest_tick(underlying)
            if tick and tick.last_price > 0:
                spot_price = tick.last_price
        spot = spot_price or 0.0

        expiry_date = (
            date.fromisoformat(expiry) if expiry
            else self.expiry_calendar.nearest(underlying, date.today())
        )
        chain = self.option_chain_builder.build(underlying, expiry_date)
        dte = max(1, (expiry_date - date.today()).days)

        def _enrich(instrument, opt_type: str) -> dict:
            base = self._serialize_instrument(instrument)
            tick = self.live_feed.latest_tick(instrument.symbol)
            ltp = tick.last_price if (tick and tick.last_price > 0) else None
            delta = None
            if spot > 0:
                try:
                    g = self.calculate_greeks({
                        "spot_price": spot,
                        "strike": instrument.strike,
                        "days_to_expiry": dte,
                        "volatility": 0.18,
                        "option_type": opt_type,
                    })
                    delta = round(g.get("delta", 0.0), 3)
                except Exception:
                    pass
            base["ltp"] = ltp
            base["delta"] = delta
            base["live"] = ltp is not None
            return base

        liquid = chain.liquid_strikes(spot) if spot else chain.strikes
        calls_map = {i.strike: _enrich(i, "CE") for i in chain.calls}
        puts_map  = {i.strike: _enrich(i, "PE") for i in chain.puts}

        strikes = sorted(set(calls_map) | set(puts_map))
        rows = []
        for s in strikes:
            if liquid and s not in liquid:
                continue
            rows.append({
                "strike": s,
                "call": calls_map.get(s),
                "put": puts_map.get(s),
            })
        return {
            "underlying": underlying,
            "expiry": expiry_date.isoformat(),
            "dte": dte,
            "spot_price": spot,
            "rows": rows,
        }

    def _serialize_instrument(self, instrument) -> dict:
        return {
            "symbol": instrument.symbol,
            "exchange": instrument.exchange.value,
            "segment": instrument.segment.value,
            "type": instrument.instrument_type.value,
            "underlying": instrument.underlying,
            "expiry": instrument.expiry.isoformat() if instrument.expiry else None,
            "strike": instrument.strike,
            "option_type": instrument.option_type.value if instrument.option_type else None,
            "lot_size": instrument.lot_size,
            "token": instrument.token,
        }

    def retraining_decision(self, payload: dict) -> dict:
        performance = ModelPerformance(
            model_name=payload.get("model_name", "candidate"),
            profit_factor=float(payload.get("profit_factor", 1.0)),
            sharpe=float(payload.get("sharpe", 0.0)),
            drawdown=float(payload.get("drawdown", 0.0)),
            iv_interval_coverage=float(payload.get("iv_interval_coverage", 0.95)),
            sentiment_precision=float(payload.get("sentiment_precision", 0.90)),
            sample_size=int(payload.get("sample_size", 0)),
            feature_drift_score=float(payload.get("feature_drift_score", 0.0)),
        )
        should_retrain, reason = self.retraining_agent.should_retrain(
            performance,
            target_gap_pct=float(payload.get("target_gap_pct", 0.0)),
        )
        return {
            "should_retrain": should_retrain,
            "reason": reason,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }

    def model_catalog(self) -> dict:
        candidates = self.model_registry.candidates()
        by_family: dict[str, int] = {}
        for candidate in candidates:
            by_family[candidate["family"]] = by_family.get(candidate["family"], 0) + 1
        return {
            "count": len(candidates),
            "by_family": by_family,
            "models": candidates,
        }

    def volatility_forecast(self, payload: dict | None = None) -> dict:
        payload = payload or {}
        closes = self._closes_from_payload(payload)
        model_name = str(payload.get("model_name", "ewma_volatility"))
        forecast = self.volatility_forecaster.forecast(closes, model_name=model_name)
        in_sample_eval = self.volatility_forecaster.evaluate_interval(
            closes[-min(len(closes), 20):], forecast, in_sample=True
        )
        result = {
            "forecast": asdict(forecast),
            "interval_evaluation": asdict(in_sample_eval),
            "policy": {
                "promotion_gate": "Candidate volatility models must keep OUT-OF-SAMPLE interval coverage above 85% before live use.",
                "current_use": "baseline risk sizing and IV sanity checks",
                "interval_evaluation_note": (
                    "interval_evaluation.in_sample=True; this number is computed on the same "
                    "closes the forecast was derived from. Use walk_forward_evaluation for an "
                    "honest forecast hit-rate."
                ),
            },
        }
        try:
            wf = self.volatility_forecaster.walk_forward_evaluate(closes, model_name=model_name)
            result["walk_forward_evaluation"] = asdict(wf)
        except ValueError as exc:
            result["walk_forward_evaluation"] = {"error": str(exc)}
        return result

    def sentiment(self, payload: dict) -> dict:
        text = str(payload.get("text", ""))
        if not text.strip():
            raise ValueError("text is required")
        result = self.sentiment_analyzer.analyze(text)
        return {
            "sentiment": asdict(result),
            "policy": {
                "live_gate": "Sentiment models can influence ranking only after precision exceeds 90% on labeled data.",
            },
        }

    def model_selection(self, payload: dict) -> dict:
        performances = [
            ModelPerformance(
                model_name=item.get("model_name", item.get("name", "candidate")),
                profit_factor=float(item.get("profit_factor", 1.0)),
                sharpe=float(item.get("sharpe", 0.0)),
                drawdown=float(item.get("drawdown", 0.0)),
                iv_interval_coverage=float(item.get("iv_interval_coverage", 0.95)),
                sentiment_precision=float(item.get("sentiment_precision", 0.90)),
                sample_size=int(item.get("sample_size", 0)),
                feature_drift_score=float(item.get("feature_drift_score", 0.0)),
            )
            for item in payload.get("performances", [])
        ]
        result = self.model_registry.select(performances)
        return {
            "selected_model": result.selected_model,
            "reason": result.reason,
            "candidates": [asdict(candidate) for candidate in result.candidates],
        }

    def _closes_from_payload(self, payload: dict) -> list[float]:
        if payload.get("closes"):
            return [float(value) for value in payload["closes"]]
        symbol = str(payload.get("symbol", "NIFTY")).upper()
        start_raw = str(payload.get("start", "2026-01-01"))
        days = int(payload.get("days", 30))
        bars = self.synthetic_data.generate_daily_bars(symbol, date.fromisoformat(start_raw), days)
        return [bar.close for bar in bars]

    # ------------------------------------------------------------------
    # GARCH forecast
    # ------------------------------------------------------------------

    def garch_forecast(self, payload: dict | None = None) -> dict:
        payload = payload or {}
        closes = self._closes_from_payload(payload)
        forecast = self.garch_forecaster.forecast(closes)
        in_sample_eval = self.volatility_forecaster.evaluate_interval(
            closes[-min(len(closes), 20):], forecast, in_sample=True
        )
        params = self.garch_forecaster.fitted_params(closes)
        result = {
            "forecast": asdict(forecast),
            "garch_params": params,
            "interval_evaluation": asdict(in_sample_eval),
            "interval_evaluation_note": (
                "in_sample=True — fitted-on-itself coverage. Use walk_forward_evaluation for a "
                "genuine out-of-sample forecast hit-rate."
            ),
        }
        try:
            wf = self.garch_forecaster.walk_forward_evaluate(closes)
            result["walk_forward_evaluation"] = asdict(wf)
        except ValueError as exc:
            result["walk_forward_evaluation"] = {"error": str(exc)}
        return result

    # ------------------------------------------------------------------
    # IV surface
    # ------------------------------------------------------------------

    def iv_surface_compute(self, payload: dict) -> dict:
        underlying = str(payload["underlying"]).upper()
        spot_price = float(payload["spot_price"])
        expiry_raw = payload.get("expiry")
        market_prices: dict[str, float] = {str(k): float(v) for k, v in (payload.get("market_prices") or {}).items()}
        risk_free_rate = float(payload.get("risk_free_rate", 0.06))

        as_of = date.fromisoformat(str(payload.get("as_of", date.today().isoformat())))
        if expiry_raw:
            expiry_date = date.fromisoformat(str(expiry_raw))
        else:
            expiry_date = self.expiry_calendar.nearest(underlying, as_of)

        chain = self.option_chain_builder.build(underlying, expiry_date)

        if not market_prices:
            # Synthetic premiums using Black-Scholes with a flat 20% vol assumption
            volatility = float(payload.get("volatility", 0.20))
            dte = max((expiry_date - as_of).days, 1)
            for instrument in [*chain.calls, *chain.puts]:
                if instrument.strike and instrument.option_type:
                    greeks = self.greeks_calculator.calculate(
                        spot_price=spot_price,
                        strike=instrument.strike,
                        days_to_expiry=dte,
                        volatility=volatility,
                        option_type=instrument.option_type,
                        risk_free_rate=risk_free_rate,
                    )
                    # Use vega-scaled price as synthetic premium
                    intrinsic = max(0.0, (spot_price - instrument.strike) if instrument.option_type.value == "CE" else (instrument.strike - spot_price))
                    premium = max(intrinsic + greeks.vega * volatility * 100, 0.50)
                    market_prices[instrument.symbol] = premium

        surface = self.iv_surface_builder.build(
            underlying=underlying,
            option_chain=chain,
            spot_price=spot_price,
            market_prices=market_prices,
            as_of=as_of,
            risk_free_rate=risk_free_rate,
        )
        return surface.to_dict()

    # ------------------------------------------------------------------
    # Walk-forward backtesting
    # ------------------------------------------------------------------

    def walk_forward_backtest(self, payload: dict | None = None) -> dict:
        payload = payload or {}
        strategy_name = str(payload.get("strategy_name", "futures_trend"))
        underlyings = tuple(payload.get("underlyings") or SCAN_UNDERLYINGS)
        start_raw = payload.get("start", "2026-01-01")
        start = date.fromisoformat(start_raw) if isinstance(start_raw, str) else start_raw
        result = self.walk_forward_evaluator.evaluate(
            strategy_name=strategy_name,
            start=start,
            total_days=int(payload.get("total_days", 60)),
            underlyings=underlyings,
            starting_capital=float(payload.get("starting_capital", self.settings.initial_capital)),
            max_drawdown=float(payload.get("max_drawdown", self.settings.max_drawdown)),
            train_days=int(payload.get("train_days", 20)),
            test_days=int(payload.get("test_days", 10)),
        )
        wf_dict = result.to_dict()
        wf_dict["retraining_recommended"] = result.degradation_detected
        return wf_dict

    # ------------------------------------------------------------------
    # ML regime classification
    # ------------------------------------------------------------------

    def regime_classify(self, payload: dict) -> dict:
        symbol = str(payload.get("symbol", "NIFTY")).upper()
        start_raw = str(payload.get("start", "2026-01-01"))
        days = int(payload.get("days", 30))

        if payload.get("features"):
            from trading_platform.ai.features import FeatureSnapshot
            f = payload["features"]
            features = FeatureSnapshot(
                symbol=symbol,
                close=float(f.get("close", 0)),
                momentum_5=float(f.get("momentum_5", 0)),
                momentum_20=float(f.get("momentum_20", 0)),
                realized_volatility=float(f.get("realized_volatility", 0)),
                volume_ratio=float(f.get("volume_ratio", 1)),
                trend_strength=float(f.get("trend_strength", 0)),
            )
        else:
            bars = self.synthetic_data.generate_daily_bars(symbol, date.fromisoformat(start_raw), days)
            from trading_platform.ai.features import FeatureEngine
            features = FeatureEngine().compute(bars)

        # We deliberately do NOT auto-train on FeatureStore records here:
        # those records are labelled by the rule-based regime agent, and
        # fitting sklearn to its own teacher's labels has no validation
        # value (see RegimeClassifier.train docstring). The classifier
        # therefore stays in deterministic rule mode unless an external
        # caller invokes `regime_classifier.train(records, label_source=...)`
        # with non-rule labels.
        regime = self.regime_classifier.predict(features)
        proba = self.regime_classifier.predict_proba(features)
        return {
            "symbol": symbol,
            "regime": regime,
            "probabilities": proba,
            "classifier_trained": self.regime_classifier.is_trained,
            "label_source": self.regime_classifier.label_source,
            "training_metrics": self.regime_classifier.last_train_metrics,
            "features": asdict(features),
        }

    # ------------------------------------------------------------------
    # Feature store
    # ------------------------------------------------------------------

    def feature_record(self, payload: dict) -> dict:
        from trading_platform.ai.features import FeatureEngine, FeatureSnapshot
        symbol = str(payload.get("symbol", "")).upper()
        if not symbol:
            raise ValueError("symbol is required")
        as_of_raw = str(payload.get("as_of", date.today().isoformat()))
        as_of = date.fromisoformat(as_of_raw)
        regime = str(payload.get("regime", "UNKNOWN"))

        if payload.get("features"):
            f = payload["features"]
            features = FeatureSnapshot(
                symbol=symbol,
                close=float(f.get("close", 0)),
                momentum_5=float(f.get("momentum_5", 0)),
                momentum_20=float(f.get("momentum_20", 0)),
                realized_volatility=float(f.get("realized_volatility", 0)),
                volume_ratio=float(f.get("volume_ratio", 1)),
                trend_strength=float(f.get("trend_strength", 0)),
            )
        else:
            start_raw = str(payload.get("start", "2026-01-01"))
            days = int(payload.get("days", 30))
            bars = self.synthetic_data.generate_daily_bars(symbol, date.fromisoformat(start_raw), days)
            features = FeatureEngine().compute(bars)
            if regime == "UNKNOWN":
                from trading_platform.ai.agents import MarketRegimeAgent
                regime = MarketRegimeAgent().classify(features)

        self.feature_store.append(symbol, as_of, features, regime)
        return {
            "recorded": True,
            "symbol": symbol,
            "as_of": as_of.isoformat(),
            "regime": regime,
            "total_records": self.feature_store.count(symbol),
        }

    def feature_history(self, symbol: str, limit: int = 100) -> dict:
        symbol = symbol.upper()
        records = self.feature_store.load(symbol, limit=limit)
        drift = self.feature_store.feature_drift_score(symbol)
        dist = self.feature_store.regime_distribution(symbol)
        return {
            "symbol": symbol,
            "record_count": len(records),
            "feature_drift_score": drift,
            "drift_alert": drift > 0.25,
            "regime_distribution": dist,
            "records": records,
        }

    # ------------------------------------------------------------------
    # Meta-model strategy ranking
    # ------------------------------------------------------------------

    def meta_model_rank(self, payload: dict) -> dict:
        regime = str(payload.get("regime", "MEAN_REVERTING"))
        strategy_names = [str(n) for n in payload.get("strategy_names", self.strategy_factory.names())]
        ranked = self.meta_model.rank(regime, strategy_names)
        return {
            "regime": regime,
            "ranked_strategies": [
                {"strategy_name": item.strategy_name, "score": item.score, "rank": item.rank}
                for item in ranked
            ],
            "summary": self.meta_model.summary(),
        }

    def meta_model_update(self, payload: dict) -> dict:
        regime = str(payload["regime"])
        strategy_name = str(payload["strategy_name"])
        score = float(payload["score"])
        self.meta_model.update(regime, strategy_name, score)
        return {"updated": True, "regime": regime, "strategy_name": strategy_name, "score": score}

    # ------------------------------------------------------------------
    # Live tick feed
    # ------------------------------------------------------------------

    def start_live_feed(self, symbols: list[str] | None = None) -> dict:
        symbols = symbols or [inst.symbol for inst in self.instrument_master.all() if not inst.is_derivative]
        # Register ALL instruments so the feed has token+exchange mappings for everything
        self.live_feed.register_instruments(self.instrument_master.all())
        self.live_feed.subscribe(symbols)
        self.live_feed.start()
        self.monitor.record_event("live_feed_started", f"Live tick feed started for {len(symbols)} symbols")
        return {"started": True, "symbols": symbols, "symbol_count": len(symbols)}

    def stop_live_feed(self) -> dict:
        self.live_feed.stop()
        self.monitor.record_event("live_feed_stopped", "Live tick feed stopped")
        return {"stopped": True}

    def live_feed_snapshot(self) -> dict:
        return self.live_feed.snapshot()

    def latest_tick(self, symbol: str) -> dict:
        tick = self.live_feed.latest_tick(symbol.upper())
        if tick is None:
            return {"symbol": symbol.upper(), "available": False}
        return {"available": True, **tick.to_dict()}

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------

    def db_summary(self) -> dict:
        return self.db.summary()

    def db_trades(self, symbol: str | None = None, execution_mode: str | None = None, limit: int = 100) -> dict:
        trades = self.db.trades(symbol=symbol, execution_mode=execution_mode, limit=limit)
        return {"count": len(trades), "trades": trades}

    def db_equity_curve(self, execution_mode: str | None = None, limit: int = 200) -> dict:
        curve = self.db.equity_curve(execution_mode=execution_mode, limit=limit)
        return {"count": len(curve), "curve": curve}

    def db_daily_pnl(self, limit: int = 30) -> dict:
        history = self.db.daily_pnl_history(limit=limit)
        return {"count": len(history), "history": history}

    def db_risk_events(self, limit: int = 50) -> dict:
        events = self.db.recent_risk_events(limit=limit)
        return {"count": len(events), "events": events}

    def health(self) -> dict:
        return {
            "status": "healthy",
            "state": self.state_payload(),
            "risk_limits": asdict(self.risk_engine.limits),
            "operational_status": self.monitor.snapshot(
                execution_mode=self.execution_mode.value,
                live_armed=self.live_armed,
                kill_switch_active=self.kill_switch_active,
            ).status,
            "scheduler": self.scheduler.stats,
            "event_bus": self.event_bus.summary(),
            "manual_approval": self.manual_approval_status(),
            "broker_capabilities": self.broker_capability_status(),
            "exit_manager": {
                "active_plans": self.exit_manager.active_plan_count,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Execution scheduler
    # ------------------------------------------------------------------

    def scheduler_stats(self) -> dict:
        return self.scheduler.stats

    def oms_events(self, limit: int = 50) -> dict:
        events = self.oms.recent_events(limit)
        return {"count": len(events), "events": events}

    def oms_order_events(self, order_id: str) -> dict:
        events = self.oms.events_for_order(order_id)
        return {"order_id": order_id, "count": len(events), "events": events}

    # ------------------------------------------------------------------
    # Exit plans
    # ------------------------------------------------------------------

    def active_exit_plans(self) -> dict:
        plans = self.exit_manager.active_plans()
        return {"count": len(plans), "plans": plans}

    def update_exit_marks(self, prices: dict[str, float]) -> dict:
        self.exit_manager.update_marks(prices)
        return {"updated": True, "symbol_count": len(prices)}

    def portfolio_positions(self) -> dict:
        """All open positions with live mark-to-market P&L."""
        now = datetime.now(timezone.utc)
        mark_prices: dict[str, float] = {}
        for symbol in list(self.portfolio.positions.keys()):
            tick = self.live_feed.latest_tick(symbol)
            if tick and tick.last_price > 0:
                mark_prices[symbol] = tick.last_price
        snapshot = self.portfolio.mark_to_market(now, mark_prices)
        positions = []
        for symbol, pos in self.portfolio.positions.items():
            if pos.quantity == 0:
                continue
            mark = mark_prices.get(symbol, pos.average_price)
            lot_size = getattr(pos.instrument, "lot_size", 1) if hasattr(pos, "instrument") else 1
            unrealized = pos.unrealized_pnl(mark)
            pnl_pct = unrealized / max(pos.average_price * abs(pos.quantity) * lot_size, 1)
            positions.append({
                "symbol": symbol,
                "quantity": pos.quantity,
                "side": "BUY" if pos.quantity > 0 else "SELL",
                "average_price": round(pos.average_price, 2),
                "mark_price": round(mark, 2),
                "unrealized_pnl": round(unrealized, 2),
                "realized_pnl": round(pos.realized_pnl, 2),
                "pnl_pct": round(pnl_pct * 100, 2),
                "live": symbol in mark_prices,
            })
        return {
            "count": len(positions),
            "positions": positions,
            "portfolio": {
                "cash": round(snapshot.cash, 2),
                "equity": round(snapshot.equity, 2),
                "unrealized_pnl": round(snapshot.unrealized_pnl, 2),
                "realized_pnl": round(snapshot.realized_pnl, 2),
                "drawdown": round(snapshot.drawdown * 100, 2),
                "peak_equity": round(snapshot.peak_equity, 2),
                "open_positions": snapshot.open_positions,
            },
        }

    def agent_trade_log(self, limit: int = 100) -> dict:
        """History of every entry/exit fill the agent processed."""
        log = self._agent_trade_log[-limit:]
        log.reverse()
        return {"count": len(log), "trades": log}

    def risk_rejection_log(self, limit: int = 100) -> dict:
        """Recent risk engine rejections with reason codes."""
        log = self._risk_rejection_log[-limit:]
        log.reverse()
        return {"count": len(log), "rejections": log}

    def governance_dashboard(self) -> dict:
        """Full governance state: goal phase, ML model health, risk supervisor, feature drift."""
        snapshot = self.portfolio.mark_to_market(datetime.now(timezone.utc), {})
        goal = self.goal_governance.evaluate(snapshot.equity, snapshot.drawdown)
        # Feature drift across scan underlyings
        drift_scores = {}
        for sym in list(self.feature_store.all_symbols())[:20]:
            drift_scores[sym] = round(self.feature_store.feature_drift_score(sym), 4)
        # MetaModel summary
        meta_summary = self.meta_model.summary()
        # Regime distribution per symbol
        regime_dist = {sym: self.feature_store.regime_distribution(sym) for sym in list(self.feature_store.all_symbols())[:10]}
        return {
            "goal": {
                "phase": goal.phase.value,
                "annual_target_pct": round(goal.annual_target_pct * 100, 1),
                "actual_run_rate": round(goal.actual_run_rate * 100, 2),
                "required_run_rate": round(goal.required_run_rate * 100, 2),
                "scaling_factor": goal.scaling_factor,
                "gap_pct": round(goal.gap_pct * 100, 2),
                "current_equity": round(goal.current_equity, 2),
                "target_equity": round(goal.target_equity, 2),
                "days_elapsed": goal.days_elapsed,
                "days_remaining": goal.days_remaining,
                "message": goal.message,
            },
            "portfolio": {
                "equity": round(snapshot.equity, 2),
                "drawdown_pct": round(snapshot.drawdown * 100, 2),
                "unrealized_pnl": round(snapshot.unrealized_pnl, 2),
                "realized_pnl": round(snapshot.realized_pnl, 2),
                "open_positions": snapshot.open_positions,
            },
            "ml_models": {
                "meta_model_summary": meta_summary,
                "regime_classifier_trained": self.regime_classifier.is_trained,
                "regime_classifier_metrics": self.regime_classifier.last_train_metrics,
                "feature_store_symbols": self.feature_store.all_symbols(),
                "feature_drift_scores": drift_scores,
                "regime_distribution": regime_dist,
            },
            "risk": {
                "kill_switch": self.kill_switch_active,
                "live_armed": self.live_armed,
                "compliance_orders_today": self.compliance.orders_today,
                "compliance_max_orders": self.compliance.max_orders_per_day,
                "rejection_count_session": len(self._risk_rejection_log),
            },
        }

    # ------------------------------------------------------------------
    # Compliance / event risk
    # ------------------------------------------------------------------

    def compliance_status(self) -> dict:
        return {
            "orders_today": self.compliance.orders_today,
            "max_orders_per_day": self.compliance.max_orders_per_day,
            "banned_symbols": list(self.compliance.banned_symbols),
        }

    def event_risk_check(self, as_of: str | None = None) -> dict:
        from datetime import date as _date
        as_of_date = _date.fromisoformat(as_of) if as_of else None
        result = self.event_risk.check(as_of_date)
        return {
            "blocked": result.blocked,
            "reason": result.reason,
            "nearest_event": result.nearest_event,
            "days_to_event": result.days_to_event,
            "recommended_action": result.recommended_action,
        }

    def economic_calendar_events(self, from_date: str | None = None, days: int = 30) -> dict:
        from datetime import date as _date
        start = _date.fromisoformat(from_date) if from_date else _date.today()
        events = self.economic_calendar.upcoming(start, days)
        return {
            "from_date": start.isoformat(),
            "days": days,
            "count": len(events),
            "events": [
                {"date": e.event_date.isoformat(), "name": e.name, "impact": e.impact, "country": e.country}
                for e in events
            ],
        }

    def news_analyze(self, payload: dict) -> dict:
        analysis = self.news_intelligence.analyze(payload)
        if analysis.recommended_action in {"BLOCK_ENTRIES", "MANUAL_APPROVAL", "REDUCE_SIZE"}:
            self.event_risk.register_temporary_event(
                reason=analysis.reason,
                expires_at=analysis.expires_at,
                recommended_action=analysis.recommended_action,
            )
        self.monitor.record_event(
            "news_event_analyzed",
            analysis.reason,
            severity="WARN" if analysis.recommended_action != "MONITOR" else "INFO",
            metadata={
                "event_id": analysis.event_id,
                "recommended_action": analysis.recommended_action,
                "global_risk_score": analysis.global_risk_score,
            },
        )
        self.event_bus.publish("news.event_received.v1", analysis.to_dict(), "news")
        return analysis.to_dict()

    def news_events(self, limit: int = 50) -> dict:
        events = self.news_intelligence.recent_events(limit)
        return {"count": len(events), "events": events, "features": self.news_intelligence.feature_snapshot()}

    def news_features(self) -> dict:
        return self.news_intelligence.feature_snapshot()

    def current_regime(self, symbol: str = "NIFTY") -> dict:
        features_payload = self.regime_classify({"symbol": symbol, "days": 30})
        news_features = self.news_intelligence.feature_snapshot()
        action = news_features["recommended_action"]
        adjusted_regime = features_payload["regime"]
        if action in {"BLOCK_ENTRIES", "MANUAL_APPROVAL"}:
            adjusted_regime = "EVENT_RISK"
        elif action == "REDUCE_SIZE" and adjusted_regime != "HIGH_VOLATILITY":
            adjusted_regime = f"{adjusted_regime}_EVENT_RISK"
        return {
            **features_payload,
            "adjusted_regime": adjusted_regime,
            "news_features": news_features,
        }

    def performance_summary(self, payload: dict | None = None) -> dict:
        payload = payload or {}
        days = int(payload.get("days", 30))
        underlyings = tuple(payload.get("underlyings") or SCAN_UNDERLYINGS)
        evaluation = self.strategy_evaluator.evaluate(
            start=date.fromisoformat(str(payload.get("start", "2026-01-01"))),
            days=days,
            underlyings=underlyings,
            starting_capital=float(payload.get("starting_capital", self.settings.initial_capital)),
            max_drawdown=float(payload.get("max_drawdown", self.settings.max_drawdown)),
            strategy_names=tuple(payload["strategy_names"]) if payload.get("strategy_names") else None,
        ).to_dict()
        quality_scores = []
        for row in evaluation["leaderboard"]:
            metrics = row["metrics"]
            score = float(row["score"])
            scaling_eligible = (
                metrics["profit_factor"] >= 1.15
                and metrics["sharpe_like"] >= 0.25
                and metrics["max_drawdown"] <= self.settings.max_drawdown
                and row["rejected_orders"] == 0
            )
            quality_scores.append(
                {
                    "strategy_id": row["strategy_name"],
                    "mode": self.execution_mode.value,
                    "lookback_days": days,
                    "profit_factor": metrics["profit_factor"],
                    "sharpe": metrics["sharpe_like"],
                    "max_drawdown": metrics["max_drawdown"],
                    "win_rate": metrics["win_rate"],
                    "score": score,
                    "scaling_eligible": scaling_eligible,
                }
            )

        oms_events = self.oms.recent_events(500)
        submitted = [event for event in oms_events if event["event_type"] == "broker_submitted"]
        fills = [event for event in oms_events if event["event_type"] == "broker_filled"]
        rejects = [event for event in oms_events if event["event_type"] in {"broker_rejected", "compliance_rejected", "capital_check_failed"}]
        return {
            "mode": self.execution_mode.value,
            "lookback_days": days,
            "best_strategy": evaluation["best_strategy"],
            "strategy_quality_scores": quality_scores,
            "execution_quality": {
                "submitted_orders": len(submitted),
                "filled_orders": len(fills),
                "rejected_orders": len(rejects),
                "rejection_rate": len(rejects) / max(1, len(submitted) + len(rejects)),
                "oms_event_count": self.oms.event_count(),
            },
            "goal": self.goal_state({}),
            "policy": {
                "scaling_rule": "Scale only when strategy and execution quality are stable; target gap never overrides risk.",
            },
        }

    # ------------------------------------------------------------------
    # Goal governance
    # ------------------------------------------------------------------

    def goal_state(self, payload: dict | None = None) -> dict:
        payload = payload or {}
        from datetime import date as _date
        as_of_raw = payload.get("as_of")
        as_of = _date.fromisoformat(as_of_raw) if as_of_raw else None
        snapshot = self.portfolio.mark_to_market(datetime.now(timezone.utc), {})
        state = self.goal_governance.evaluate(
            current_equity=float(payload.get("current_equity", snapshot.equity)),
            drawdown=float(payload.get("drawdown", snapshot.drawdown)),
            as_of=as_of,
        )
        return asdict(state)

    # ------------------------------------------------------------------
    # Position reconciliation
    # ------------------------------------------------------------------

    def reconcile_positions(self, broker_positions: dict[str, int]) -> dict:
        results = self.reconciliation.reconcile(broker_positions)
        return {
            "count": len(results),
            "has_drift": any(r.drift != 0 for r in results),
            "results": [
                {
                    "symbol": r.symbol,
                    "local_qty": r.local_qty,
                    "broker_qty": r.broker_qty,
                    "drift": r.drift,
                    "action_taken": r.action_taken,
                }
                for r in results
            ],
        }
