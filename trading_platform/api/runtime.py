from __future__ import annotations

import collections
import json
import logging
import math
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
from trading_platform.broker.base import BrokerResult
from trading_platform.broker.capability_registry import BrokerCapabilityRegistry
from trading_platform.broker.simulated import SimulatedBrokerClient
from trading_platform.config import Settings, load_settings
from trading_platform.data.angel_one_history import AngelOneHistoricalDataProvider
from trading_platform.data.angel_one_instruments import AngelOneInstrumentMasterProvider
from trading_platform.data.instrument_master import build_default_universe
from trading_platform.data.market_data import SyntheticDataProvider
from trading_platform.decision.orchestrator import DecisionCycleOrchestrator
from trading_platform.decision.pipeline import DecisionPipeline
from trading_platform.derivatives.engine import ContractSelector, ExpiryCalendar, GreeksCalculator, IVSurfaceBuilder, OptionChainBuilder, RolloverPlanner
from trading_platform.domain.enums import ExecutionMode, OptionType, OrderPriority, OrderType, ProductType, Side
from trading_platform.domain.models import OrderIntent, Signal
from trading_platform.event_bus import InMemoryEventBus
from trading_platform.execution.emergency_square_off import EmergencySquareOff
from trading_platform.execution.fill_processor import FillProcessor
from trading_platform.execution.final_gate import FinalGateDecision
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
from trading_platform.agent.market_hours import now_ist

# ── Phase 1–9: new components (lazy-safe imports) ─────────────────────────────
from trading_platform.trace.ids import new_trace_id
from trading_platform.trace.models import DecisionTrace
from trading_platform.trace.store import TraceStore
from trading_platform.trace.label_factory import OutcomeFactory
from trading_platform.trace.learning_journal import PaperLearningJournal
from trading_platform.api.trace_replay_service import TraceReplayService, decode_oms_event
from trading_platform.api.model_research_service import ModelResearchService
from trading_platform.api.db_query_service import DbQueryService
from trading_platform.api.news_service import NewsService
from trading_platform.api.policy_service import PolicyService
from trading_platform.api.options_service import OptionsService
from trading_platform.api.regime_meta_service import RegimeMetaService
from trading_platform.api.quantum_lab_service import QuantumLabService
from trading_platform.api.live_feed_service import LiveFeedService
from trading_platform.api.ai_capabilities import ai_capabilities, log_capabilities_at_startup
from trading_platform.logging_safety import note_swallowed, swallowed_error_count
from trading_platform.ai.meta_labeler import MetaLabeler
from trading_platform.agents.model_gateway import LocalModelGateway
from trading_platform.agents.supervisor import AgentCouncilSupervisor
from trading_platform.agents.schemas import AgentInputContext
from trading_platform.agents.vector_memory import RAGRetriever, VectorMemoryStore
from trading_platform.neural.serving import NeuralPredictionService
from trading_platform.quantum.service import QuantumOptimizationService
from trading_platform.quantum.schemas import PortfolioOptimizationRequest, QuantumCandidate
from trading_platform.quantum.quantum_kernel import QuantumKernelResearchService
from trading_platform.decision_fusion.fusion import EnsembleDecisionEngine
from trading_platform.decision_fusion.goal_governor import GoalGovernor
from trading_platform.rl.policies import MockPolicy, PolicyRecord, PolicyRegistry, SimpleMomentumPolicy
from trading_platform.rl.evaluator import EvalResult, RLEvaluator
from trading_platform.streaming.topics import (
    BusTopic, TypedTopicBus,
    publish_tick, publish_features, publish_order_event, publish_risk_veto,
)


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
        if self.settings.auto_load_models:
            self.regime_classifier.load(f"{self._model_dir}/regime_classifier")
            self.meta_model.load(f"{self._model_dir}/meta_model.json")
        self.feature_store = FeatureStore()
        self.iv_surface_builder = IVSurfaceBuilder()
        # In-memory logs for dashboard visibility
        self._risk_rejection_log: collections.deque[dict] = collections.deque(maxlen=500)
        self._agent_trade_log: collections.deque[dict] = collections.deque(maxlen=500)
        self._entry_fill_context: dict[str, dict] = {}  # symbol -> {strategy, regime, entry_price}
        # Stores the last OrchestratorState per symbol so post-trade reflection can update
        # SpecialistCrew agent weights, MarketRAG win rates, and ProfitGuard rolling stats.
        self._orchestrator_trade_states: dict[str, Any] = {}
        self.walk_forward_evaluator = WalkForwardEvaluator(self.backtest_engine)
        self.synthetic_data = SyntheticDataProvider()
        self.charges_model = ChargesModel()
        self.monitor = OperationalMonitor()
        self.db = TradingDatabase(
            database_url=self.settings.database_url or None
        )
        self.live_feed = LiveTickFeed(self.settings)
        # Wire live Angel One prices into the paper broker so fills use real market prices
        self.paper_broker.set_live_feed(self.live_feed)
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
            max_futures_margin_pct=RiskLimits().max_futures_margin_pct,
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
            final_gate=self._evaluate_final_execution_gate,
            # Audit fix H3: intents are stamped with the mode they were created
            # under and rejected at dequeue if the runtime has since switched.
            get_execution_mode=lambda: self.execution_mode.value,
        )

        self.exit_manager = ExitManager(
            enqueue_fn=self.scheduler.enqueue,
            poll_interval=1.0,
            portfolio=self.portfolio,
        )
        self.multi_leg_manager = MultiLegOrderManager(self.scheduler.enqueue)
        # Short-vol strategy executor (the one validated-edge strategy). Built here
        # because it uses submit_multi_leg + decision_pipeline + instrument_master.
        from trading_platform.strategies.short_vol_executor import ShortVolExecutor
        self.short_vol_executor = ShortVolExecutor(self)
        self.square_off_manager = EmergencySquareOff(
            self.portfolio,
            self.scheduler.enqueue,
            # Exit pricing: live tick first, then exit-manager sticky mark.
            # Never price a square-off at entry price when the market is known.
            mark_source=self._best_mark_for_symbol,
        )

        # Register fill callbacks
        self.scheduler.register_fill_callback(self._on_fill)
        # Wire multi-leg fill notifications so spread legs don't time out and roll back
        self.scheduler.register_fill_callback(
            lambda trade, intent: self.multi_leg_manager.notify_fill(intent.idempotency_key, trade)
        )

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

        # ── Phase 1–9 components (all disabled by default; flags guard activation) ──
        self.trace_store = TraceStore()

        # ── Gap 1: Label / Outcome Factory + MetaLabeler ─────────────────────
        self._outcome_factory = OutcomeFactory()
        self._meta_labeler = MetaLabeler()
        self.paper_learning_journal = PaperLearningJournal()
        # Per-symbol neural direction probabilities refreshed by high_end_signal_scan
        self._latest_neural_probs: dict[str, float] = {}
        if self.settings.auto_load_models:
            try:
                self._meta_labeler.load(f"{self._model_dir}/meta_labeler.json")
            except Exception as exc:
                note_swallowed("init.meta_labeler_load", exc)

        # ── Gap 3: Typed streaming bus ────────────────────────────────────────
        # Wraps existing InMemoryEventBus; backward-compat — all existing
        # callers of self.event_bus continue to work unchanged.
        self.typed_bus = TypedTopicBus(self.event_bus)

        # Register live-feed tick handler → publishes BusTopic.TICK_RAW
        self.live_feed.add_handler(self._on_tick)

        # Register feature-store publish hook → publishes BusTopic.FEATURES
        self.feature_store.set_publish_hook(
            lambda sym, rec: publish_features(self.typed_bus, sym, rec)
        )

        # ── Gap 2: VectorMemory + RAG (only needed when AI council is active) ──
        if self.settings.enable_ai_council:
            self._vector_store = VectorMemoryStore()
            self._vector_store.seed_defaults()
            self._rag_retriever = RAGRetriever(self._vector_store)
            self._llm_gateway = LocalModelGateway(
                runtime=self.settings.local_llm_runtime,
                base_url=self.settings.local_llm_base_url,
                timeout=self.settings.local_llm_timeout_seconds,
                max_tokens=self.settings.local_llm_max_output_tokens,
                rag_retriever=self._rag_retriever,
            )
            self._agent_council = AgentCouncilSupervisor(
                gateway=self._llm_gateway,
                trace_store=self.trace_store,
            )
        else:
            self._vector_store = None
            self._rag_retriever = None
            self._llm_gateway = None
            self._agent_council = None

        if self.settings.enable_neural_lab:
            self._neural_service = NeuralPredictionService(
                trace_store=self.trace_store,
                model_dir=self._model_dir,   # loads models/return_forecaster if validated
            )
        else:
            self._neural_service = None

        if self.settings.enable_quantum_lab:
            self._quantum_service = QuantumOptimizationService(
                backend=self.settings.quantum_backend,
                timeout=self.settings.quantum_timeout_seconds,
                min_baseline_improvement=self.settings.quantum_min_baseline_improvement,
                trace_store=self.trace_store,
            )
        else:
            self._quantum_service = None
        # Kernel service is shadow-only and has no external deps — always instantiate.
        self._quantum_kernel_service = QuantumKernelResearchService()

        # Ensemble engine and goal governor are lightweight; always instantiate.
        self._ensemble_engine = EnsembleDecisionEngine()
        self._goal_governor = GoalGovernor(
            yearly_target=self.settings.yearly_profit_target,
            initial_capital=self.settings.initial_capital,
        )

        # Phase 5 (MARL): policy registry + RL evaluator — seeded with built-in policies
        self._policy_registry = PolicyRegistry()
        self._rl_evaluator = RLEvaluator()
        self._policy_promotion_requirements = {
            "min_paper_trading_days": 5,
            "min_paper_fills": 20,
            "min_paper_labels": 20,
            "min_slippage_records": 20,
            "min_learning_updates": 20,
            "min_complete_replays": 1,
            "min_profit_factor": 1.10,
            "min_sharpe": 0.50,
            "max_drawdown_pct": self.settings.max_drawdown,
            "max_avg_slippage_surprise": 0.0015,
        }
        _seed_policies = [
            (PolicyRecord("momentum_v1", role="entry", status="paper", version=1),     SimpleMomentumPolicy()),
            (PolicyRecord("noop_baseline", role="exit",  status="shadow", version=1),  MockPolicy()),
        ]
        for record, policy in _seed_policies:
            self._policy_registry.register(record, policy)

        # Canonical scan/orchestration path: baseline first, advisory systems second.
        self._decision_orchestrator = DecisionCycleOrchestrator(self)

        # ── Master Orchestrator (LangGraph-style profit-first pipeline) ───────
        from trading_platform.orchestrator.master_orchestrator import MasterOrchestrator
        self.master_orchestrator = MasterOrchestrator(self)
        # Wire pgvector DB into all learning components so state survives restarts
        self.master_orchestrator._market_rag.set_db(self.db)
        self.master_orchestrator._market_rag.load_from_db()
        self.master_orchestrator._profit_guard.set_db(self.db)
        self.master_orchestrator._profit_guard.load_from_db()
        self.master_orchestrator._reflection_engine.set_db(self.db)
        self.master_orchestrator._reflection_engine.load_weights_from_db()

        # Extracted read-only services (Phase 2 runtime decomposition).
        self._trace_replay_service = TraceReplayService(
            trace_store=self.trace_store,
            oms=self.oms,
            portfolio=self.portfolio,
            journal_getter=lambda: self.paper_learning_journal,
            outcome_factory=self._outcome_factory,
            serialize_trade=self._serialize_trade,
        )
        self._model_research_service = ModelResearchService(
            retraining_agent=self.retraining_agent,
            model_registry=self.model_registry,
            volatility_forecaster=self.volatility_forecaster,
            sentiment_analyzer=self.sentiment_analyzer,
            garch_forecaster=self.garch_forecaster,
            synthetic_data=self.synthetic_data,
        )
        self._db_query_service = DbQueryService(db=self.db)
        self._news_service = NewsService(
            news_intelligence=self.news_intelligence,
            event_risk=self.event_risk,
            monitor=self.monitor,
            event_bus=self.event_bus,
        )
        self._policy_service = PolicyService(
            policy_registry=self._policy_registry,
            policy_promotion_requirements=self._policy_promotion_requirements,
            journal_getter=lambda: self.paper_learning_journal,
            trace_replay=self.trace_replay,
            float_or_none=self._float_or_none,
        )
        self._options_service = self._build_options_service()
        self._live_feed_service = self._build_live_feed_service()
        self._regime_meta_service = RegimeMetaService(
            regime_classifier=self.regime_classifier,
            synthetic_data=self.synthetic_data,
            news_intelligence=self.news_intelligence,
            meta_model=self.meta_model,
            strategy_factory=self.strategy_factory,
        )
        self._quantum_lab_service = QuantumLabService(
            quantum_service=self._quantum_service,
            quantum_kernel_service=self._quantum_kernel_service,
            settings=self.settings,
            trace_store=self.trace_store,
        )

        # Honest startup banner: which "advanced" AI layers are inert/advisory.
        log_capabilities_at_startup(self)

    def _build_options_service(self) -> OptionsService:
        """Build the OptionsService from the current market engines.

        Re-invoked by _rebuild_market_engines because expiry_calendar and
        option_chain_builder are replaced on instrument refresh.
        """
        return OptionsService(
            expiry_calendar=self.expiry_calendar,
            option_chain_builder=self.option_chain_builder,
            live_feed=self.live_feed,
            greeks_calculator=self.greeks_calculator,
            iv_surface_builder=self.iv_surface_builder,
        )

    def _build_live_feed_service(self) -> LiveFeedService:
        """Build the LiveFeedService from the current instrument master.

        Re-invoked by _rebuild_market_engines because instrument_master is
        replaced on instrument refresh. execution_mode and live-order eligibility
        change without a rebuild, so they are injected as callables that read the
        live runtime state.
        """
        return LiveFeedService(
            live_feed=self.live_feed,
            instrument_master=self.instrument_master,
            instrument_freshness=self.instrument_freshness,
            monitor=self.monitor,
            settings=self.settings,
            can_submit_live_orders=self._can_submit_live_orders,
            load_cached_instruments=self._load_cached_instruments_if_available,
            get_execution_mode=lambda: self.execution_mode,
        )

    # ── Orchestrator-facing property shortcuts ────────────────────────────────

    @property
    def neural_service(self):
        return self._neural_service

    @property
    def quantum_service(self):
        return self._quantum_service

    @property
    def goal_governor(self):
        return self._goal_governor

    @property
    def event_risk_guard(self):
        return self.event_risk

    @property
    def agent_council(self):
        return self._agent_council

    @property
    def policy_registry(self):
        return self._policy_registry

    # ── Gap 3: Live-feed tick → TICK_RAW ─────────────────────────────────────

    def _on_tick(self, tick) -> None:
        """Tick handler registered with LiveTickFeed → publishes BusTopic.TICK_RAW.

        Runs in the WebSocket thread; must never block or raise.
        """
        try:
            # Audit fix C3: exit protection must see every tick, not just the
            # agent's scan-cycle sync. Stop-losses now evaluate against prices
            # at most one exit-manager poll (1s) old instead of up to 5 minutes.
            if tick.last_price and tick.last_price > 0:
                self.exit_manager.update_marks({tick.symbol: float(tick.last_price)})
            publish_tick(
                self.typed_bus,
                symbol=tick.symbol,
                last_price=tick.last_price,
                open=tick.open,
                high=tick.high,
                low=tick.low,
                close=tick.close,
                volume=tick.volume,
                exchange=tick.exchange,
                ts=tick.timestamp.isoformat(),
            )
        except Exception as exc:
            note_swallowed("on_tick.publish", exc)  # never disrupt the tick stream

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
            canary_readiness = self.live_canary_readiness_payload()
            if not canary_readiness.get("can_consider_live_canary", False):
                blockers = canary_readiness.get("blocking_reasons") or ["not_ready"]
                reasons = ", ".join(str(reason) for reason in blockers)
                raise ValueError(f"live_canary_readiness_blocked: {reasons}")
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

    def set_kill_switch(self, active: bool, reason: str = "") -> dict:
        reason = reason.strip() or ("runtime_direct_activation" if active else "manual_clear")
        self.kill_switch_active = active
        self.scheduler.kill_switch_active = active
        if active:
            self.live_armed = False
            # FREEZE semantics (audit fix C2): the kill switch blocks new entries
            # (enforced in scheduler.enqueue and RiskEngine) but exit plans stay
            # registered and stop-loss/target exits continue to flow — position-
            # reducing orders are explicitly allowed during kill switch. Flattening
            # everything is a separate explicit action: POST /execution/square-off.
        self.monitor.record_event(
            "kill_switch_changed",
            f"Kill switch set to {active}: {reason}",
            severity="CRITICAL" if active else "INFO",
        )
        try:
            self.db.save_risk_event(
                event_type="kill_switch_activated" if active else "kill_switch_cleared",
                reason=reason,
                approved=not active,
            )
        except Exception as exc:
            note_swallowed("kill_switch.audit_emit", exc)
        self.event_bus.publish(
            "kill_switch.triggered.v1" if active else "kill_switch.cleared.v1",
            {"active": active, "reason": reason},
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
        self._append_trace_event_for_intent(
            intent,
            "broker_filled",
            "ExecutionScheduler",
            {
                "trade_id": getattr(trade, "trade_id", ""),
                "fill_price": fill_price,
                "quantity": intent.quantity,
                "side": side,
            },
        )
        slippage_payload = self._record_paper_fill(
            intent,
            trade,
            source="ExecutionScheduler",
        )

        # ── Persist fill to DB + update monitor (both entry and exit) ──────
        exec_mode = self.execution_mode.value
        try:
            import asyncio as _aio
            # Compute market feature_vector for pgvector similarity queries (non-fatal)
            _fv: list | None = None
            try:
                _ctx_for_fv = self._entry_fill_context.get(symbol, {})
                _feats_for_fv = self.feature_store.get_features(
                    getattr(intent.instrument, "underlying", None) or symbol
                ) or {}
                if _feats_for_fv:
                    from trading_platform.orchestrator.market_rag import _feature_vector as _mkfv
                    _fv = _mkfv(
                        regime=_ctx_for_fv.get("regime", "NEUTRAL"),
                        news_sentiment=_feats_for_fv.get("news_sentiment", 0.0),
                        volatility=_feats_for_fv.get("realized_volatility",
                                    _feats_for_fv.get("predicted_volatility", 0.20)),
                        momentum=_feats_for_fv.get("momentum_20",
                                  _feats_for_fv.get("momentum_5", 0.0)),
                        rsi=_feats_for_fv.get("rsi_14", 50.0),
                        neural_direction_prob=float(_ctx_for_fv.get("neural_direction_probability", 0.5)),
                        neural_uncertainty=0.5,
                    )
            except Exception as exc:
                note_swallowed("trade_feature_vector", exc)
            _aio.ensure_future(_aio.to_thread(self.db.save_trade, trade, execution_mode=exec_mode, feature_vector=_fv))
            mark_prices: dict[str, float] = {}
            for sym in list(self.portfolio.positions.keys()):
                tick = self.live_feed.latest_tick(sym)
                if tick and tick.last_price > 0:
                    mark_prices[sym] = tick.last_price
            snap = self.portfolio.mark_to_market(datetime.now(timezone.utc), mark_prices)
            _aio.ensure_future(_aio.to_thread(self.db.save_snapshot, snap, execution_mode=exec_mode))
            _aio.ensure_future(_aio.to_thread(self.db.save_positions, dict(self.portfolio.positions), execution_mode=exec_mode))
            self.monitor.record_order({"status": "FILLED"})
        except Exception as _persist_err:
            logger.warning("_on_fill DB persist error: %s", _persist_err)

        # ── Exit fill: compute P&L-based score → update MetaModel + OutcomeLabel ─
        if intent.priority != OrderPriority.ENTRY:
            ctx = self._entry_fill_context.pop(symbol, None)
            if ctx:
                entry_price = ctx.get("entry_price", fill_price)
                regime = ctx.get("regime", "MEAN_REVERTING")
                strat = ctx.get("strategy", strategy_name)
                direction = 1 if ctx.get("side", "BUY") == "BUY" else -1
                pnl_pct = direction * (fill_price - entry_price) / max(entry_price, 1)
                # Normalise to [0,1] score: fallback used only when OutcomeFactory returns None
                score = min(1.0, max(0.0, 0.5 + pnl_pct * 10))

                # ── Gap 1: compute full outcome label from OutcomeFactory ──
                outcome_label = None
                try:
                    trace_id = ctx.get("trace_id", "")
                    outcome_label = self._outcome_factory.compute_exit_label(
                        symbol=symbol,
                        exit_price=fill_price,
                        bars_held=1,
                        realized_slippage_pct=float(slippage_payload.get("realized_slippage_pct", 0.0)),
                        trace_id=trace_id,
                    )
                    if outcome_label:
                        self._append_trace_event_for_intent(
                            intent,
                            "outcome_label_created",
                            "OutcomeFactory",
                            outcome_label.to_dict(),
                        )
                        # Feed meta_label_score back to goal governor daily PnL
                        try:
                            equity = self.portfolio.equity
                            self._goal_governor.record_daily_pnl(
                                pnl=pnl_pct * entry_price,
                                equity=equity,
                            )
                        except Exception as exc:
                            note_swallowed("on_fill.goal_governor_daily_pnl", exc)
                        # Champion/challenger: update with richer label score + neural quality
                        try:
                            neural_dir_prob = ctx.get("neural_direction_probability", 0.5)
                            champion_record = self._meta_labeler.record_outcome(
                                strategy_name=strat,
                                regime=regime,
                                label=outcome_label,
                                neural_direction_probability=neural_dir_prob,
                            )
                            # Upgrade meta_model with the label-derived score (replaces simple P&L score)
                            self.meta_model.update(regime, strat, outcome_label.meta_label_score)
                            learning_payload = {
                                "strategy_name": strat,
                                "regime": regime,
                                "meta_label_score": outcome_label.meta_label_score,
                                "neural_direction_probability": neural_dir_prob,
                                "champion_record": champion_record.to_dict(),
                            }
                            self._append_trace_event_for_intent(
                                intent,
                                "post_trade_learning_updated",
                                "MetaLabeler",
                                learning_payload,
                            )
                            self._record_paper_learning_update(
                                intent,
                                learning_payload,
                                source="MetaLabeler",
                                trade_id=getattr(trade, "trade_id", ""),
                            )
                        except Exception as _ml_err:
                            logger.debug("_on_fill: meta_labeler error: %s", _ml_err)
                        self._record_paper_label(intent, outcome_label)
                        # Publish label to typed bus
                        self.typed_bus.publish(
                            BusTopic.LABEL_OUTCOME,
                            outcome_label.to_dict(),
                            source="outcome_factory",
                        )
                except Exception as _lf_err:
                    logger.debug("_on_fill: outcome_factory error: %s", _lf_err)

                # Fallback: if OutcomeFactory returned nothing, still update meta_model with simple score
                if not outcome_label:
                    try:
                        self.meta_model.update(regime, strat, score)
                        learning_payload = {"strategy_name": strat, "regime": regime, "score": score}
                        self._append_trace_event_for_intent(
                            intent,
                            "post_trade_learning_updated",
                            "MetaModel",
                            learning_payload,
                        )
                        self._record_paper_learning_update(
                            intent,
                            learning_payload,
                            source="MetaModel",
                            trade_id=getattr(trade, "trade_id", ""),
                        )
                    except Exception as exc:
                        note_swallowed("on_fill.meta_model_trace", exc)

                # Wire MasterOrchestrator reflection: update RAG patterns + ProfitGuard
                # rolling window so future cycles benefit from this trade's outcome.
                try:
                    orch = getattr(self, "master_orchestrator", None)
                    if orch is not None:
                        underlying_sym = getattr(intent.instrument, "underlying", None) or symbol
                        crew_consensus = float(ctx.get("crew_consensus",
                            intent.signal.metadata.get("crew_consensus", 0.5)))
                        market_feats: dict = {}
                        try:
                            market_feats = self.feature_store.get_features(underlying_sym) or {}
                        except Exception as exc:
                            note_swallowed("on_fill.feature_store_lookup", exc)
                        orch._reflection_engine.reflect(
                            trace_id=ctx.get("trace_id", ""),
                            underlying=underlying_sym,
                            action_taken=ctx.get("side", "BUY"),
                            pnl_pct=pnl_pct,
                            crew_votes=[],   # vote objects not stored at entry; weight updates skipped
                            crew_consensus=crew_consensus,
                            regime=regime,
                            market_features=market_feats,
                            market_rag=orch._market_rag,
                            profit_guard=orch._profit_guard,
                        )
                except Exception as _refl_err:
                    logger.debug("_on_fill: reflection error: %s", _refl_err)

                self._agent_trade_log.append({
                    "ts": now_ts, "type": "EXIT", "symbol": symbol,
                    "strategy": strat, "regime": regime, "side": side,
                    "entry_price": entry_price, "exit_price": fill_price,
                    "pnl_pct": round(pnl_pct * 100, 2), "ml_score": round(score, 3),
                    "barrier_outcome": outcome_label.barrier_outcome if outcome_label else None,
                    "forward_bucket": outcome_label.forward_bucket if outcome_label else None,
                    "meta_label_score": outcome_label.meta_label_score if outcome_label else None,
                })

                # ── Orchestrator reflection: update SpecialistCrew weights +
                #    MarketRAG win rates + ProfitGuard rolling window ─────────
                orch_state = (
                    self._orchestrator_trade_states.pop(symbol, None)
                    or self._orchestrator_trade_states.pop(ctx.get("underlying", ""), None)
                )
                if orch_state is not None and hasattr(self, "master_orchestrator"):
                    import asyncio as _asyncio
                    _action_taken = ctx.get("side", "BUY")
                    try:
                        _asyncio.ensure_future(
                            self.master_orchestrator.reflect_on_outcome(
                                state=orch_state,
                                pnl_pct=pnl_pct,
                                action_taken=_action_taken,
                            )
                        )
                    except Exception as _ref_err:
                        logger.debug("orchestrator reflection failed (non-critical): %s", _ref_err)
            else:
                self._agent_trade_log.append({
                    "ts": now_ts, "type": "EXIT", "symbol": symbol,
                    "strategy": strategy_name, "side": side, "exit_price": fill_price,
                })
            # Confirmed exit fill: this is the ONLY event that releases the
            # exit plan (audit fix H1 — emitting an exit order is not being flat).
            exit_trigger = intent.signal.metadata.get("exit_trigger")
            exit_plan_id = intent.signal.metadata.get("exit_plan_id")
            self.exit_manager.on_exit_fill(symbol, plan_id=exit_plan_id, trigger=exit_trigger)
            try:
                if exit_trigger == "PARTIAL_TARGET" and self.exit_manager.active_plan_count:
                    # Half booked, remainder still protected: persist the reduced plan.
                    for live_plan in self.exit_manager.active_plans():
                        if live_plan.get("symbol") == symbol:
                            self.db.save_exit_plan(live_plan, execution_mode=exec_mode)
                else:
                    self.db.delete_exit_plans_for_symbol(symbol, execution_mode=exec_mode)
            except Exception as exc:
                note_swallowed("on_fill.delete_exit_plans", exc)

            # Gap 3: publish ORDER_EVENT fill to typed bus
            try:
                publish_order_event(
                    self.typed_bus, event="filled",
                    symbol=symbol, side=side,
                    price=fill_price, quantity=intent.quantity,
                    strategy=strategy_name,
                    intent_id=intent.idempotency_key,
                )
            except Exception as exc:
                note_swallowed("on_fill.publish_exit_order_event", exc)
            return

        # ── Entry fill: record context for ML feedback later ────────────────
        trace_id = intent.signal.metadata.get("trace_id", "")
        entry_regime = intent.signal.metadata.get("regime", "MEAN_REVERTING")
        # neural_direction_probability is injected by signal scan when a
        # NeuralPredictionBundle was available; default 0.5 = neutral.
        neural_dir_prob = float(
            intent.signal.metadata.get("neural_direction_probability", 0.5)
        )
        self._entry_fill_context[symbol] = {
            "strategy": strategy_name,
            "regime": entry_regime,
            "entry_price": fill_price,
            "side": side,
            "trace_id": trace_id,
            "neural_direction_probability": neural_dir_prob,
            "crew_consensus": float(intent.signal.metadata.get("crew_consensus", 0.5)),
            "underlying": intent.signal.metadata.get("underlying", symbol),
        }

        # ── Gap 1: register entry in OutcomeFactory for later label computation ──
        try:
            predicted_return = float(intent.signal.metadata.get(
                "predicted_return", intent.signal.confidence * 0.03
            ))
            self._outcome_factory.record_entry(
                symbol=symbol,
                side=side,
                entry_price=fill_price,
                predicted_return=predicted_return,
                expected_slippage_pct=float(slippage_payload.get("expected_slippage_pct", 0.001)),
                trace_id=trace_id,
                metadata={
                    "strategy": strategy_name,
                    "regime": entry_regime,
                    "neural_direction_probability": neural_dir_prob,
                },
            )
        except Exception as _of_err:
            logger.debug("_on_fill: outcome_factory.record_entry error: %s", _of_err)

        # Gap 3: publish ORDER_EVENT entry fill to typed bus
        try:
            publish_order_event(
                self.typed_bus, event="filled",
                symbol=symbol, side=side,
                price=fill_price, quantity=intent.quantity,
                strategy=strategy_name,
                intent_id=intent.idempotency_key,
            )
        except Exception as exc:
            note_swallowed("on_fill.publish_entry_order_event", exc)
        self._agent_trade_log.append({
            "ts": now_ts, "type": "ENTRY", "symbol": symbol,
            "strategy": strategy_name, "side": side, "entry_price": fill_price,
            "quantity": intent.quantity,
        })

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
        plan.trace_id = trace_id
        self.exit_manager.register(plan)
        self._append_trace_event_for_intent(
            intent,
            "exit_plan_created",
            "ExitManager",
            {
                "plan_id": plan.plan_id,
                "stop_loss_price": plan.stop_loss_price,
                "target_price": plan.target_price,
                "trailing_pct": plan.trailing_pct,
                "broker_side_available": False,
            },
        )
        try:
            self.db.save_exit_plan(plan.to_dict(), execution_mode=exec_mode)
        except Exception as exc:
            # A lost exit-plan persist means no SL/TP protection survives a restart —
            # must not vanish silently. Log + count (still non-fatal to the fill).
            note_swallowed("save_exit_plan", exc)
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

        # Audit fix M9: restoring against a synthetic instrument master would
        # skip real contract symbols and DISCARD their exit plans. Try the real
        # cache first; if the master is still synthetic, defer restore entirely
        # (leave the DB untouched) so a later restart can recover cleanly.
        if self.instrument_freshness.status()["is_synthetic"]:
            self._load_cached_instruments_if_available()
        if self.instrument_freshness.status()["is_synthetic"]:
            logger.critical(
                "restore_state DEFERRED: instrument master is synthetic — positions/exit plans "
                "left in DB untouched. Refresh instruments and restart to restore state."
            )
            self.monitor.record_event(
                "state_restore_deferred",
                "Instrument master is synthetic; position/exit-plan restore deferred to protect DB state",
                severity="CRITICAL",
            )
            return

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
            # Guard: skip exit plans for positions that are no longer open.
            # Stale plans from previous sessions cause phantom sells against flat positions.
            live_pos = self.portfolio.positions.get(symbol)
            if live_pos is None or live_pos.quantity == 0:
                logger.info(
                    "restore_state: exit plan for %s has no matching open position — discarding stale plan",
                    symbol,
                )
                try:
                    self.db.delete_exit_plans_for_symbol(symbol, execution_mode=exec_mode)
                except Exception as exc:
                    note_swallowed("restore_state.delete_stale_exit_plans", exc)
                continue
            # SQLite returns DATE columns as ISO strings; Postgres returns date
            # objects. Handle both — a str-only parse crash-loops startup.
            _raw_expiry = plan_data.get("expiry_date")
            if not _raw_expiry:
                expiry = None
            elif isinstance(_raw_expiry, date):
                expiry = _raw_expiry
            else:
                expiry = date.fromisoformat(str(_raw_expiry)[:10])
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
        """Start core async services and optional broker-facing loops."""
        await self.scheduler.start()
        await self.exit_manager.start()

        if self.settings.auto_load_instrument_cache:
            self._load_cached_instruments_if_available()

        # Restore state BEFORE starting the feed/agent so the agent sees
        # already-open positions and doesn't create duplicates on its first tick.
        await self.restore_state()
        # Seed the session-start equity so the daily-loss circuit breaker has a baseline.
        # Must be called after restore_state() so portfolio cash is accurate.
        self.scheduler.set_session_start_equity(self.portfolio.equity)

        live_feed_started = False
        agent_started = False
        if self.settings.auto_start_live_feed and self.settings.angel_one_configured:
            self.start_live_feed()
            live_feed_started = True
        elif self.settings.auto_start_live_feed:
            logger.info("Live feed auto-start requested but Angel One credentials are incomplete")

        if self.settings.auto_start_agent:
            self.agent.start()
            agent_started = True
        self.monitor.record_event(
            "async_services_started",
            (
                "Scheduler and ExitManager started; "
                f"agent_auto_started={agent_started}; live_feed_auto_started={live_feed_started}"
            ),
        )

    def _load_cached_instruments_if_available(self) -> bool:
        cache_path = self.angel_one_instruments.cache_path
        if not cache_path.exists():
            logger.info("Angel One instrument cache not found; using synthetic universe")
            return False
        try:
            self.load_cached_angel_one_instruments()
            logger.info("Loaded Angel One instrument cache from %s", cache_path)
            return True
        except Exception as exc:
            logger.warning("Could not load Angel One instrument cache %s: %s", cache_path, exc)
            return False

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
            self._meta_labeler.save(f"{self._model_dir}/meta_labeler.json")
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
        freshness = self.instrument_freshness.status()
        return {
            "instrument_source": "angel_one",
            "instrument_cache_path": str(cache_path),
            "instrument_cache_exists": cache_path.exists(),
            "current_universe_count": len(self.instrument_master.instruments),
            "current_universe_source": "runtime",
            "instrument_master_is_synthetic": freshness["is_synthetic"],
            "instrument_last_refresh_at": freshness["last_refresh_at"],
            "instrument_refresh_source": freshness["source"],
            "auto_load_instrument_cache": self.settings.auto_load_instrument_cache,
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
        # Guard against a partial/truncated download silently replacing a good
        # chain (the full Angel One master is ~120k instruments). A tiny parse
        # means the download was cut short — keep the existing master.
        if result.parsed_count < 50_000:
            logger.critical(
                "Instrument refresh returned only %d parsed (<50k) — likely a truncated "
                "download; keeping existing master (%d instruments).",
                result.parsed_count, len(self.instrument_master.instruments),
            )
            self.monitor.record_event(
                "instrument_refresh_truncated",
                f"Refresh parsed only {result.parsed_count}; kept existing master",
                severity="CRITICAL",
            )
            return {"source": result.source, "parsed_count": result.parsed_count,
                    "kept_existing": True}
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
        # Rebuild the options service so it uses the fresh calendar/chain builder.
        if hasattr(self, "_options_service"):
            self._options_service = self._build_options_service()
        # Rebuild the live-feed service so it resolves symbols against the fresh master.
        if hasattr(self, "_live_feed_service"):
            self._live_feed_service = self._build_live_feed_service()
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
        preview_payload = {**payload, "_gate_phase": "preview", "_trace_side_effects": False}
        decision = self._evaluate_final_execution_gate(intent, now, mark_prices, preview_payload)
        return {
            "mode": self.execution_mode.value,
            "approved": decision.approved,
            "reason": decision.reason,
            "risk_score": decision.risk_score,
            "final_gate": decision.to_dict(),
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
            final_gate=self._evaluate_final_execution_gate,
        )
        report = router.submit(intent, now, mark_prices)
        if report.trade:
            raw = report.broker_result.raw if report.broker_result and report.broker_result.raw else {}
            self._append_trace_event_for_intent(
                intent,
                "broker_filled",
                "ExecutionRouter",
                {
                    "trade_id": report.trade.trade_id,
                    "fill_price": report.trade.price,
                    "quantity": report.trade.quantity,
                    "side": report.trade.side.value,
                    "slippage_pct": raw.get("slippage_pct"),
                    "reference_price": raw.get("reference_price"),
                },
            )
            self._record_paper_fill(
                intent,
                report.trade,
                source="paper_order",
                broker_result=report.broker_result,
            )
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
            "trace_id": intent.signal.metadata.get("trace_id", ""),
            "portfolio": asdict(snapshot),
        }

    async def enqueue_order(self, payload: dict) -> dict:
        intent = self._intent_from_payload(payload)
        return await self._enqueue_intent_with_controls(intent, payload)

    async def _enqueue_intent_with_controls(self, intent: OrderIntent, payload: dict | None = None) -> dict:
        payload = payload or {}
        trace_id = self._ensure_trace_for_intent(intent, source="runtime_enqueue")
        gate_payload = {**payload}
        gate_payload.setdefault("_gate_phase", "enqueue_preflight")
        final_decision = self._evaluate_final_execution_gate(intent, payload=gate_payload)
        if not final_decision.approved:
            self._record_final_gate_rejection(intent, final_decision)
            return {
                "enqueued": False,
                "approval_required": False,
                "risk_rejected": True,
                "final_gate": final_decision.to_dict(),
                "trace_id": trace_id,
                "idempotency_key": intent.idempotency_key,
            }

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
                metadata={
                    "trace_id": intent.signal.metadata.get("trace_id", ""),
                    "request_id": request.request_id,
                    "expires_at": request.expires_at.isoformat(),
                },
            )
            self._append_trace_event_for_intent(
                intent,
                "manual_approval_requested",
                "ManualApprovalGate",
                {"request_id": request.request_id, "expires_at": request.expires_at.isoformat()},
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
                "trace_id": trace_id,
                "idempotency_key": intent.idempotency_key,
            }

        event_id = await self.scheduler.enqueue(intent)
        self._append_trace_event_for_intent(
            intent,
            "order_queued",
            "ExecutionScheduler",
            {"event_id": event_id},
        )
        return {
            "enqueued": True,
            "approval_required": False,
            "event_id": event_id,
            "trace_id": trace_id,
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

    # Max drift between the price the order was decided at and the live price
    # at approval time. Beyond this, "approve" would execute a materially
    # different trade than the one the human reviewed (audit fix M8).
    _APPROVAL_MAX_PRICE_DRIFT_PCT = 0.01

    async def approve_order(self, request_id: str, payload: dict | None = None) -> dict:
        payload = payload or {}
        pending = self.manual_approval._pending.get(request_id)
        if pending is not None and pending.intent is not None:
            decided_price = float(pending.intent.signal.price or 0.0)
            tick = self.live_feed.latest_tick(pending.intent.instrument.symbol)
            live_price = float(getattr(tick, "last_price", 0.0) or 0.0) if tick else 0.0
            if decided_price > 0 and live_price > 0:
                drift = abs(live_price - decided_price) / decided_price
                if drift > self._APPROVAL_MAX_PRICE_DRIFT_PCT:
                    self.manual_approval.reject(
                        request_id,
                        note=f"auto-rejected: price drifted {drift:.2%} during approval "
                             f"(decided {decided_price:.2f}, now {live_price:.2f})",
                    )
                    raise ValueError(
                        f"price_drift_rejected: market moved {drift:.2%} since this order was "
                        f"submitted for approval (limit {self._APPROVAL_MAX_PRICE_DRIFT_PCT:.0%}). "
                        "Re-run the scan to generate a fresh order."
                    )
        request = self.manual_approval.approve(request_id, str(payload.get("approval_reason", "")))
        if request.intent is None:
            raise ValueError("Approval request has no order intent")
        self.oms.append(
            event_type="manual_approval_approved",
            order_id=request.intent.idempotency_key,
            idempotency_key=request.intent.idempotency_key,
            symbol=request.intent.instrument.symbol,
            strategy_name=request.intent.signal.strategy_name,
            metadata={
                "trace_id": request.intent.signal.metadata.get("trace_id", ""),
                "request_id": request.request_id,
                "reviewer_note": request.reviewer_note,
            },
        )
        self._append_trace_event_for_intent(
            request.intent,
            "manual_approval_approved",
            "ManualApprovalGate",
            {"request_id": request.request_id, "reviewer_note": request.reviewer_note},
        )
        self.event_bus.publish(
            "order.manual_approval_approved.v1",
            {"request_id": request.request_id, "idempotency_key": request.intent.idempotency_key},
            "control",
        )
        enqueued = await self._enqueue_intent_with_controls(
            request.intent,
            {"manual_approved": True, "_gate_phase": "manual_approval_enqueue"},
        )
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
                metadata={
                    "trace_id": request.intent.signal.metadata.get("trace_id", ""),
                    "request_id": request.request_id,
                },
            )
            self._append_trace_event_for_intent(
                request.intent,
                "manual_approval_rejected",
                "ManualApprovalGate",
                {"request_id": request.request_id, "reason": request.reviewer_note},
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
            reason=str(payload.get("reason") or "manual_square_off"),
        )
        self.oms.append(
            event_type="square_off_requested",
            order_id=f"square_off:{result['timestamp']}",
            metadata={**result, "reason": payload.get("reason", "")},
        )
        try:
            self.db.save_risk_event(
                event_type="square_off_requested",
                reason=str(payload.get("reason") or "manual_square_off"),
                symbol=str(payload["symbol"]).upper() if payload.get("symbol") else None,
                approved=True,
            )
        except Exception as exc:
            note_swallowed("square_off.save_risk_event", exc)
        if self.execution_mode == ExecutionMode.PAPER:
            try:
                self.paper_learning_journal.append(
                    "emergency_square_off",
                    trace_id=str(payload.get("trace_id") or ""),
                    symbol=str(payload.get("symbol") or ""),
                    strategy_name=str(payload.get("strategy_name") or payload.get("strategy_id") or ""),
                    execution_mode=self.execution_mode.value,
                    source="runtime.square_off",
                    payload={**result, "reason": payload.get("reason", "")},
                )
            except Exception as exc:
                note_swallowed("square_off.paper_learning_journal", exc)
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
        # Accept side as a Side enum (Pydantic model_dump passes the enum) or a
        # raw string; str(Side.BUY) yields "Side.BUY", so normalise via .value.
        _raw_side = payload.get("side", "BUY")
        side = _raw_side if isinstance(_raw_side, Side) else Side(str(_raw_side).upper())
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

    def _trace_id_for_intent(self, intent: OrderIntent) -> str:
        return str(intent.signal.metadata.get("trace_id") or "")

    def _ensure_trace_for_intent(self, intent: OrderIntent, source: str) -> str:
        trace_id = self._trace_id_for_intent(intent)
        if not trace_id:
            trace_id = new_trace_id("order")
            intent.signal.metadata["trace_id"] = trace_id

        trace = self.trace_store.get(trace_id)
        if trace is None:
            trace = DecisionTrace(
                trace_id=trace_id,
                created_at=datetime.now(timezone.utc),
                execution_mode=self.execution_mode.value,
                symbol_universe=[intent.instrument.underlying or intent.instrument.symbol],
            )
            trace.add_event(
                "order_trace_created",
                source,
                {
                    "symbol": intent.instrument.symbol,
                    "strategy_name": intent.signal.strategy_name,
                    "source": source,
                },
            )

        if intent.idempotency_key not in trace.order_intent_ids:
            trace.order_intent_ids.append(intent.idempotency_key)
            trace.add_event(
                "order_intent_created",
                source,
                {
                    "order_id": intent.idempotency_key,
                    "symbol": intent.instrument.symbol,
                    "side": intent.signal.side.value,
                    "quantity": intent.quantity,
                    "price": intent.limit_price or intent.signal.price,
                    "strategy_name": intent.signal.strategy_name,
                    "priority": intent.priority.name,
                    "notional_value": intent.notional_value,
                },
            )
        self.trace_store.save(trace)
        return trace_id

    def _append_trace_event_for_intent(
        self,
        intent: OrderIntent,
        event_type: str,
        component: str,
        data: dict | None = None,
    ) -> None:
        trace_id = self._ensure_trace_for_intent(intent, source=component)
        trace = self.trace_store.get(trace_id)
        if not trace:
            return
        trace.add_event(
            event_type,
            component,
            {
                "order_id": intent.idempotency_key,
                "symbol": intent.instrument.symbol,
                **(data or {}),
            },
        )
        self.trace_store.save(trace)

    def _latest_broker_payload_for_order(self, order_id: str) -> tuple[str, dict]:
        """Return broker_order_id and raw broker payload from the latest OMS broker event."""
        try:
            events = [decode_oms_event(event) for event in self.oms.events_for_order(order_id)]
        except Exception:
            return "", {}
        for event in reversed(events):
            if event.get("event_type") not in {"broker_filled", "broker_submitted"}:
                continue
            metadata = event.get("metadata") or {}
            raw = metadata.get("raw") if isinstance(metadata, dict) else {}
            return str(event.get("broker_order_id") or ""), raw if isinstance(raw, dict) else {}
        return "", {}

    @staticmethod
    def _float_or_none(value: object) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def _slippage_payload(
        self,
        intent: OrderIntent,
        fill_price: float,
        *,
        broker_result: BrokerResult | None = None,
        broker_raw: dict | None = None,
    ) -> dict:
        raw = dict(broker_raw or {})
        if broker_result and isinstance(broker_result.raw, dict):
            raw = {**raw, **broker_result.raw}

        reference_price = self._float_or_none(raw.get("reference_price"))
        if reference_price is None or reference_price <= 0:
            reference_price = float(intent.limit_price or intent.signal.price or fill_price)

        realized = self._float_or_none(raw.get("slippage_pct"))
        if realized is None:
            realized = abs(float(fill_price) - reference_price) / max(reference_price, 1e-9)

        expected = self._float_or_none(intent.signal.metadata.get("expected_slippage_pct"))
        if expected is None:
            expected = 0.001

        return {
            "reference_price": reference_price,
            "fill_price": float(fill_price),
            "expected_slippage_pct": expected,
            "realized_slippage_pct": realized,
            "slippage_surprise": realized - expected,
            "side": intent.signal.side.value,
            "raw": raw,
        }

    def _record_paper_fill(
        self,
        intent: OrderIntent,
        trade,
        *,
        source: str,
        broker_result: BrokerResult | None = None,
    ) -> dict:
        if self.execution_mode != ExecutionMode.PAPER or trade is None:
            return {}
        try:
            trace_id = self._ensure_trace_for_intent(intent, source=source)
            broker_order_id = broker_result.broker_order_id if broker_result else ""
            broker_raw: dict = {}
            if not broker_order_id:
                broker_order_id, broker_raw = self._latest_broker_payload_for_order(intent.idempotency_key)
            fill_price = float(getattr(trade, "price", None) or intent.signal.price)
            slippage = self._slippage_payload(
                intent,
                fill_price,
                broker_result=broker_result,
                broker_raw=broker_raw,
            )
            context = {
                "trace_id": trace_id,
                "order_id": intent.idempotency_key,
                "trade_id": getattr(trade, "trade_id", ""),
                "symbol": intent.instrument.symbol,
                "strategy_name": intent.signal.strategy_name,
                "regime": str(intent.signal.metadata.get("regime", "")),
                "side": intent.signal.side.value,
                "quantity": intent.quantity,
                "execution_mode": self.execution_mode.value,
                "source": source,
            }
            fill_payload = {
                "trade": self._serialize_trade(trade),
                "broker_order_id": broker_order_id,
                "priority": intent.priority.name,
                "opens_position": bool(intent.signal.metadata.get("opens_position", True)),
                "slippage": slippage,
            }
            self.paper_learning_journal.append(
                "fill_recorded",
                **context,
                payload=fill_payload,
            )
            self.paper_learning_journal.append(
                "slippage_recorded",
                **context,
                payload={
                    **slippage,
                    "broker_order_id": broker_order_id,
                    "priority": intent.priority.name,
                },
            )
            return slippage
        except Exception as exc:
            logger.debug("paper learning fill journal error: %s", exc)
            return {}

    def _record_paper_label(self, intent: OrderIntent, label, *, source: str = "OutcomeFactory") -> None:
        if self.execution_mode != ExecutionMode.PAPER or label is None:
            return
        try:
            label_payload = label.to_dict()
            label_payload["metadata"] = dict(getattr(label, "metadata", {}) or {})
            self.paper_learning_journal.append(
                "outcome_label_created",
                trace_id=label.trace_id or self._trace_id_for_intent(intent),
                order_id=intent.idempotency_key,
                symbol=label.symbol,
                strategy_name=intent.signal.strategy_name,
                regime=str(intent.signal.metadata.get("regime", "")),
                side=label.side,
                quantity=intent.quantity,
                execution_mode=self.execution_mode.value,
                source=source,
                payload=label_payload,
            )
        except Exception as exc:
            logger.debug("paper learning label journal error: %s", exc)

    def _record_paper_learning_update(
        self,
        intent: OrderIntent,
        payload: dict,
        *,
        source: str,
        trade_id: str = "",
    ) -> None:
        if self.execution_mode != ExecutionMode.PAPER:
            return
        try:
            self.paper_learning_journal.append(
                "post_trade_learning_updated",
                trace_id=self._trace_id_for_intent(intent),
                order_id=intent.idempotency_key,
                trade_id=trade_id,
                symbol=intent.instrument.symbol,
                strategy_name=str(payload.get("strategy_name") or intent.signal.strategy_name),
                regime=str(payload.get("regime") or intent.signal.metadata.get("regime", "")),
                side=intent.signal.side.value,
                quantity=intent.quantity,
                execution_mode=self.execution_mode.value,
                source=source,
                payload=payload,
            )
        except Exception as exc:
            logger.debug("paper learning update journal error: %s", exc)

    def _record_trace_final_gate(
        self,
        intent: OrderIntent,
        decision: FinalGateDecision,
    ) -> None:
        trace_id = self._ensure_trace_for_intent(intent, source="FinalExecutionGate")
        trace = self.trace_store.get(trace_id)
        if not trace:
            return
        payload = {
            "component": "FinalExecutionGate",
            "trace_id": trace_id,
            "order_id": intent.idempotency_key,
            "symbol": intent.instrument.symbol,
            "strategy_name": intent.signal.strategy_name,
            "approved": decision.approved,
            "reason": decision.reason,
            "risk_score": decision.risk_score,
            "stage": decision.stage,
        }
        trace.risk_decisions.append(payload)
        trace.add_event(
            "final_gate_approved" if decision.approved else "final_gate_rejected",
            "FinalExecutionGate",
            payload,
        )
        self.trace_store.save(trace)

    def _evaluate_final_execution_gate(
        self,
        intent: OrderIntent,
        now: datetime | None = None,
        mark_prices: dict[str, float] | None = None,
        payload: dict | None = None,
    ) -> FinalGateDecision:
        """Single runtime-level authorization check before any broker call.

        This is intentionally stricter than manual approval. Human approval can
        acknowledge intent, but it cannot override kill switch, live readiness,
        capital limits, or the core RiskEngine.
        """
        payload = payload or {}
        gate_phase = str(payload.get("_gate_phase", "final_execution_gate"))
        trace_side_effects = bool(payload.get("_trace_side_effects", True))
        trace_id = self._trace_id_for_intent(intent)
        if trace_side_effects:
            trace_id = self._ensure_trace_for_intent(intent, source=gate_phase)
        now = now or datetime.now(timezone.utc)
        mark_prices = dict(mark_prices or self._mark_prices_for_intent(intent, payload))
        snapshot = self.portfolio.mark_to_market(now, mark_prices)
        opens_position = bool(intent.signal.metadata.get("opens_position", True))

        protection = {
            "opens_position": opens_position,
            "priority": intent.priority.name,
            "explicit_stop_loss": intent.stop_loss is not None,
            "explicit_target": intent.target is not None,
            "system_exit_plan_after_fill": opens_position,
        }
        common_details = {
            "execution_mode": self.execution_mode.value,
            "symbol": intent.instrument.symbol,
            "strategy_name": intent.signal.strategy_name,
            "notional_value": intent.notional_value,
            "portfolio_equity": snapshot.equity,
            "portfolio_drawdown": snapshot.drawdown,
            "trace_id": trace_id,
            "protection": protection,
        }

        # ALL MODES: an entry without a positive price has no price source
        # (e.g. option premium unavailable). Filling at 0.00 pollutes the
        # ledger, fabricates P&L and can cascade into circuit breakers —
        # 50 zero-price paper fills observed 2026-07-14 before this gate.
        if opens_position and (intent.limit_price or intent.signal.price or 0) <= 0:
            decision = FinalGateDecision.reject(
                "no_valid_price",
                stage=gate_phase,
                details={**common_details, "reason": "entry price is zero/unknown (premium source missing)"},
            )
            if trace_side_effects:
                self._record_trace_final_gate(intent, decision)
            return decision

        if self.execution_mode.value.startswith("LIVE"):
            if not self.live_armed:
                decision = FinalGateDecision.reject(
                    "live_mode_not_armed",
                    stage=gate_phase,
                    details={**common_details, "live_armed": False},
                )
                if trace_side_effects:
                    self._record_trace_final_gate(intent, decision)
                return decision
            if not self._can_submit_live_orders():
                decision = FinalGateDecision.reject(
                    "live_order_configuration_blocked",
                    stage=gate_phase,
                    details={
                        **common_details,
                        "live_trading_enabled": self.settings.live_trading_enabled,
                        "angel_one_configured": self.settings.angel_one_configured,
                        "confirmation_ready": (
                            self.settings.live_order_confirmation
                            == "I_ACCEPT_REAL_MONEY_LIVE_ORDERS"
                        ),
                    },
                )
                if trace_side_effects:
                    self._record_trace_final_gate(intent, decision)
                return decision
            readiness = self.live_readiness_payload()
            if not readiness["armed_eligible"]:
                blockers = readiness.get("blocking_reasons", [])
                decision = FinalGateDecision.reject(
                    "live_readiness_blocked:" + ",".join(blockers),
                    stage=gate_phase,
                    details={**common_details, "readiness": readiness},
                )
                if trace_side_effects:
                    self._record_trace_final_gate(intent, decision)
                return decision
            if opens_position:
                canary_readiness = self.live_canary_readiness_payload()
                if not canary_readiness.get("can_consider_live_canary", False):
                    blockers = [str(item) for item in (canary_readiness.get("blocking_reasons") or ["not_ready"])]
                    decision = FinalGateDecision.reject(
                        "live_canary_readiness_blocked:" + ",".join(blockers),
                        stage=gate_phase,
                        details={**common_details, "canary_readiness": canary_readiness},
                    )
                    if trace_side_effects:
                        self._record_trace_final_gate(intent, decision)
                    return decision
            broker = self.broker_client()
            if hasattr(broker, "is_ready") and not broker.is_ready():
                decision = FinalGateDecision.reject(
                    "broker_not_ready",
                    stage=gate_phase,
                    details={**common_details, "broker": getattr(broker, "name", "unknown")},
                )
                if trace_side_effects:
                    self._record_trace_final_gate(intent, decision)
                return decision
            if opens_position:
                # Audit fix M7: a LIVE entry priced without a real market tick is
                # priced from fantasy (signal price may fall back to static base
                # prices). Never open a real-money position on an unconfirmed price.
                live_tick = self.live_feed.latest_tick(intent.instrument.symbol)
                if live_tick is None or not getattr(live_tick, "last_price", 0) > 0:
                    decision = FinalGateDecision.reject(
                        "no_live_price_confirmation",
                        stage=gate_phase,
                        details={**common_details, "reason": "no live tick for symbol; refusing live entry"},
                    )
                    if trace_side_effects:
                        self._record_trace_final_gate(intent, decision)
                    return decision

        # Audit fix M1: the scheduler calls this gate with no payload, which used
        # to zero out daily_pnl and neuter the daily-loss check at the last line
        # of defense. Compute today's P&L from session-start equity when the
        # caller didn't supply it.
        if "daily_pnl" in payload:
            gate_daily_pnl = float(payload["daily_pnl"])
        else:
            session_start = float(getattr(self.scheduler, "_session_start_equity", 0.0) or 0.0) if hasattr(self, "scheduler") else 0.0
            gate_daily_pnl = (snapshot.equity - session_start) if session_start > 0 else 0.0

        risk_decision = self.risk_engine.evaluate(
            intent=intent,
            portfolio=snapshot,
            now=now,
            execution_mode=self.execution_mode,
            live_armed=self.live_armed,
            kill_switch_active=self.kill_switch_active,
            daily_pnl=gate_daily_pnl,
            strategy_daily_pnl=float(payload.get("strategy_daily_pnl", 0.0)),
            options_short_exposure=float(payload.get("options_short_exposure", 0.0)),
            gamma_exposure=float(payload.get("gamma_exposure", 0.0)),
            symbol_exposure_pct=(
                float(payload["symbol_exposure_pct"])
                if payload.get("symbol_exposure_pct") is not None
                else None
            ),
            correlated_exposure_pct=float(payload.get("correlated_exposure_pct", 0.0)),
            margin_utilization=float(payload.get("margin_utilization", 0.0)),
            orders_sent_today=self.compliance.orders_today,
            trades_today=int(self.scheduler.stats.get("processed", 0)),
        )
        if not risk_decision.approved:
            decision = FinalGateDecision.reject(
                risk_decision.reason,
                stage=gate_phase,
                risk_score=risk_decision.risk_score,
                details=common_details,
            )
            if trace_side_effects:
                self._record_trace_final_gate(intent, decision)
            return decision

        if self.capital_protection and opens_position:
            cap_result = self.capital_protection.check(
                intent,
                snapshot,
                daily_pnl=float(payload.get("daily_pnl", 0.0)),
            )
            if not cap_result.approved:
                decision = FinalGateDecision.reject(
                    "capital_protection_failed:" + cap_result.reason,
                    stage=gate_phase,
                    risk_score=min(1.0, cap_result.utilization_pct),
                    details={**common_details, "capital_check": asdict(cap_result)},
                )
                if trace_side_effects:
                    self._record_trace_final_gate(intent, decision)
                return decision

        decision = FinalGateDecision.approve(
            stage=gate_phase,
            risk_score=risk_decision.risk_score,
            details=common_details,
        )
        if trace_side_effects:
            self._record_trace_final_gate(intent, decision)
        return decision

    def _record_final_gate_rejection(
        self,
        intent: OrderIntent,
        decision: FinalGateDecision,
    ) -> None:
        now_ts = datetime.now(timezone.utc).isoformat()
        row = {
            "ts": now_ts,
            "underlying": intent.instrument.underlying or intent.instrument.symbol,
            "symbol": intent.instrument.symbol,
            "strategy": intent.signal.strategy_name,
            "reason": decision.reason,
            "risk_score": decision.risk_score,
            "regime": intent.signal.metadata.get("regime", ""),
        }
        self._risk_rejection_log.append(row)
        self.oms.append(
            event_type="risk_rejected",
            order_id=intent.idempotency_key,
            idempotency_key=intent.idempotency_key,
            symbol=intent.instrument.symbol,
            strategy_name=intent.signal.strategy_name,
            side=intent.signal.side.value,
            quantity=intent.quantity,
            price=intent.limit_price or intent.signal.price,
            priority=int(intent.priority),
            rejection_reason=decision.reason,
            metadata=decision.to_dict(),
        )
        try:
            publish_risk_veto(
                self.typed_bus,
                symbol=intent.instrument.symbol,
                reason=decision.reason,
                component="final_execution_gate",
                intent_id=intent.idempotency_key,
            )
        except Exception as exc:
            note_swallowed("final_gate_rejection.trace_emit", exc)
        try:
            self.db.save_risk_event(
                event_type="final_gate_rejected",
                reason=decision.reason,
                symbol=intent.instrument.symbol,
                risk_score=decision.risk_score,
                approved=False,
            )
        except Exception as exc:
            note_swallowed("final_gate_rejection.save_risk_event", exc)

    def _best_mark_for_symbol(self, symbol: str) -> float | None:
        """Best available market price: live tick → exit-manager sticky mark.

        Used by EmergencySquareOff so exits are priced at market, not entry.
        Returns None when no genuine price exists (caller decides fallback).
        """
        try:
            tick = self.live_feed.latest_tick(symbol)
            if tick is not None and tick.last_price and tick.last_price > 0:
                return float(tick.last_price)
        except Exception as exc:
            note_swallowed("best_mark.live_tick", exc)
        try:
            return self.exit_manager.current_mark(symbol)
        except Exception as exc:
            note_swallowed("best_mark.exit_manager", exc)
        return None

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
        return self._decision_orchestrator.run_signal_scan(payload).baseline_payload

    def shadow_run(self, payload: dict | None = None) -> dict:
        if self.execution_mode != ExecutionMode.PAPER:
            raise ValueError("Shadow paper run requires runtime mode PAPER")
        payload = payload or {}
        underlyings = [str(item).upper() for item in payload.get("underlyings", SCAN_UNDERLYINGS)]
        # Keep the default shadow-paper demo deterministic for tests and CI.
        # Callers that want a rolling or live-history paper run can pass
        # ``start`` and optionally ``use_live_history=true`` explicitly.
        _default_start = "2026-01-01"
        start = date.fromisoformat(str(payload.get("start", _default_start)))
        days = int(payload.get("days", 60))
        strategy_names = [str(item) for item in payload["strategy_names"]] if payload.get("strategy_names") else None
        use_live_history = bool(payload.get("use_live_history", False))
        history_provider = self.decision_pipeline.history_provider
        if not use_live_history:
            self.decision_pipeline.history_provider = None
        try:
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
        finally:
            self.decision_pipeline.history_provider = history_provider
        router = ExecutionRouter(
            broker=self.paper_broker,
            risk_engine=self.risk_engine,
            portfolio=self.portfolio,
            execution_mode=ExecutionMode.PAPER,
            live_armed=False,
            kill_switch_active=self.kill_switch_active,
            final_gate=self._evaluate_final_execution_gate,
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
                if report.trade:
                    raw = report.broker_result.raw if report.broker_result and report.broker_result.raw else {}
                    self._append_trace_event_for_intent(
                        intent,
                        "broker_filled",
                        "ExecutionRouter",
                        {
                            "trade_id": report.trade.trade_id,
                            "fill_price": report.trade.price,
                            "quantity": report.trade.quantity,
                            "side": report.trade.side.value,
                            "slippage_pct": raw.get("slippage_pct"),
                            "reference_price": raw.get("reference_price"),
                        },
                    )
                    self._record_paper_fill(
                        intent,
                        report.trade,
                        source="shadow_run",
                        broker_result=report.broker_result,
                    )
                executions.append(
                    {
                        "underlying": scan.underlying,
                        "trace_id": intent.signal.metadata.get("trace_id", ""),
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
        return self._options_service.expiries(underlying)

    def option_chain(self, underlying: str, expiry: str | None = None, spot_price: float | None = None) -> dict:
        return self._options_service.option_chain(underlying, expiry, spot_price)

    def calculate_greeks(self, payload: dict) -> dict:
        return self._options_service.calculate_greeks(payload)

    def target_progress(self, payload: dict | None = None) -> dict:
        payload = payload or {}
        start_raw = payload.get("start_date", "2026-01-01")
        as_of_raw = payload.get("as_of", now_ist().date().isoformat())
        progress = self.target_tracker.evaluate(
            start_date=date.fromisoformat(start_raw),
            as_of=date.fromisoformat(as_of_raw),
            start_capital=float(payload.get("start_capital", self.settings.initial_capital)),
            current_equity=float(payload.get("current_equity", self.portfolio.equity)),
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
        return self._options_service.option_chain_live(underlying, expiry, spot_price)

    def retraining_decision(self, payload: dict) -> dict:
        return self._model_research_service.retraining_decision(payload)

    def model_catalog(self) -> dict:
        return self._model_research_service.model_catalog()

    def volatility_forecast(self, payload: dict | None = None) -> dict:
        return self._model_research_service.volatility_forecast(payload)

    def sentiment(self, payload: dict) -> dict:
        return self._model_research_service.sentiment(payload)

    def model_selection(self, payload: dict) -> dict:
        return self._model_research_service.model_selection(payload)

    def garch_forecast(self, payload: dict | None = None) -> dict:
        return self._model_research_service.garch_forecast(payload)


    # ------------------------------------------------------------------
    # IV surface
    # ------------------------------------------------------------------

    def iv_surface_compute(self, payload: dict) -> dict:
        return self._options_service.iv_surface_compute(payload)

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
        return self._regime_meta_service.regime_classify(payload)

    # ------------------------------------------------------------------
    # Feature store
    # ------------------------------------------------------------------

    def feature_record(self, payload: dict) -> dict:
        from trading_platform.ai.features import FeatureEngine, FeatureSnapshot
        symbol = str(payload.get("symbol", "")).upper()
        if not symbol:
            raise ValueError("symbol is required")
        as_of_raw = str(payload.get("as_of", now_ist().date().isoformat()))
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
        return self._regime_meta_service.meta_model_rank(payload)

    def meta_model_update(self, payload: dict) -> dict:
        return self._regime_meta_service.meta_model_update(payload)

    # ------------------------------------------------------------------
    # Live tick feed
    # ------------------------------------------------------------------

    def start_live_feed(self, symbols: list[str] | None = None) -> dict:
        return self._live_feed_service.start_live_feed(symbols)

    def stop_live_feed(self) -> dict:
        return self._live_feed_service.stop_live_feed()

    def live_feed_snapshot(self) -> dict:
        return self._live_feed_service.live_feed_snapshot()

    def latest_tick(self, symbol: str) -> dict:
        return self._live_feed_service.latest_tick(symbol)

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------

    def db_summary(self) -> dict:
        return self._db_query_service.db_summary()

    def db_trades(self, symbol: str | None = None, execution_mode: str | None = None, limit: int = 100) -> dict:
        return self._db_query_service.db_trades(symbol=symbol, execution_mode=execution_mode, limit=limit)

    def db_equity_curve(self, execution_mode: str | None = None, limit: int = 200) -> dict:
        return self._db_query_service.db_equity_curve(execution_mode=execution_mode, limit=limit)

    def db_daily_pnl(self, limit: int = 30) -> dict:
        return self._db_query_service.db_daily_pnl(limit=limit)

    def db_risk_events(self, limit: int = 50) -> dict:
        return self._db_query_service.db_risk_events(limit=limit)


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
            # Honest report of which "advanced" AI layers are real vs stub/heuristic.
            "ai_capabilities": ai_capabilities(self),
            # Count of deliberately-swallowed exceptions — a rising number means
            # hot-path failures are being silently absorbed; investigate.
            "swallowed_errors": swallowed_error_count(),
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
        import itertools
        log = list(itertools.islice(reversed(self._agent_trade_log), limit))
        return {"count": len(log), "trades": log}

    def risk_rejection_log(self, limit: int = 100) -> dict:
        """Recent risk engine rejections with reason codes."""
        import itertools
        log = list(itertools.islice(reversed(self._risk_rejection_log), limit))
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
        start = _date.fromisoformat(from_date) if from_date else now_ist().date()
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
        return self._news_service.news_analyze(payload)

    def news_events(self, limit: int = 50) -> dict:
        return self._news_service.news_events(limit=limit)

    def news_features(self) -> dict:
        return self._news_service.news_features()


    def current_regime(self, symbol: str = "NIFTY") -> dict:
        return self._regime_meta_service.current_regime(symbol)

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

    # ------------------------------------------------------------------
    # Phase 7: High-end quantum / AI multi-agent signal scan
    # ------------------------------------------------------------------

    def high_end_signal_scan(self, payload: dict) -> dict:
        """Run the canonical baseline-first advisory decision cycle."""
        return self._decision_orchestrator.run_high_end_scan(payload).high_end_payload

    # ------------------------------------------------------------------
    # Phase 1: Trace API helpers
    # ------------------------------------------------------------------

    def get_trace(self, trace_id: str) -> dict | None:
        return self._trace_replay_service.get_trace(trace_id)

    def list_traces(self, max_traces: int = 50) -> dict:
        return self._trace_replay_service.list_traces(max_traces)

    def trace_replay(self, trace_id: str) -> dict | None:
        return self._trace_replay_service.trace_replay(trace_id)


    # ------------------------------------------------------------------
    # Phase 2: AI Council API helpers
    # ------------------------------------------------------------------

    def ai_council_status(self) -> dict:
        if self._llm_gateway is None:
            return {
                "enabled": self.settings.enable_ai_council,
                "gateway_runtime": self.settings.local_llm_runtime,
                "primary_model": self.settings.local_llm_primary_model,
                "gateway_available": False,
                "fallback_active": True,
                "gateway_note": "AI Council is disabled; manual previews use the deterministic stub gateway.",
                "models": {
                    "primary": self.settings.local_llm_primary_model,
                    "coordinator": self.settings.local_llm_coordinator_model,
                    "fast": self.settings.local_llm_fast_model,
                },
                "rag": {"enabled": False},
            }
        gw = self._llm_gateway.status()
        return {
            "enabled": self.settings.enable_ai_council,
            "gateway_runtime": self.settings.local_llm_runtime,
            "primary_model": self.settings.local_llm_primary_model,
            "gateway_available": gw["available"],
            "fallback_active": gw["fallback_active"],
            "gateway_note": gw["note"],
            "models": gw["models"],
            "rag": gw.get("rag", {"enabled": False}),
        }

    def label_factory_status(self) -> dict:
        """Return OutcomeFactory statistics for dashboard/monitoring."""
        return {
            "label_count": self._outcome_factory.count(),
            "pending_symbols": self._outcome_factory.pending_symbols(),
            "barrier_distribution": self._outcome_factory.barrier_distribution(),
            "bucket_distribution": self._outcome_factory.bucket_distribution(),
            "average_meta_score": round(self._outcome_factory.average_meta_score(), 4),
            "slippage_surprise_mean": round(self._outcome_factory.slippage_surprise_mean(), 6),
            "recent_labels": self._outcome_factory.recent_labels(n=10),
            "durable_journal": self.paper_learning_journal.summary(limit=10),
        }

    def paper_learning_journal_status(self, limit: int = 50, trace_id: str | None = None) -> dict:
        """Return durable paper/shadow fill, slippage, label, and learning records."""
        return self.paper_learning_journal.summary(limit=limit, trace_id=trace_id)

    def live_canary_readiness_payload(self) -> dict:
        return self._policy_service.live_canary_readiness_payload()

    def streaming_bus_status(self) -> dict:
        """Return TypedTopicBus topic counts and status."""
        return self.typed_bus.topic_status()

    def ai_council_preview(self, payload: dict) -> dict:
        symbols = payload.get("symbols", ["NIFTY"])
        regime = payload.get("regime", "unknown")
        trace_id = new_trace_id("preview")
        ctx = AgentInputContext(
            trace_id=trace_id,
            symbols=list(symbols),
            execution_mode=self.execution_mode.value,
            market_regime=regime,
        )
        council = self._agent_council
        if council is None:
            gateway = LocalModelGateway(runtime="stub")
            council = AgentCouncilSupervisor(gateway=gateway, trace_store=self.trace_store)
        decision = council.run(ctx)
        return decision.to_dict()

    # ------------------------------------------------------------------
    # Phase 4: Quantum API helpers
    # ------------------------------------------------------------------

    def quantum_status(self) -> dict:
        return self._quantum_lab_service.quantum_status()

    def quantum_kernel_status(self) -> dict:
        return self._quantum_lab_service.quantum_kernel_status()

    def quantum_optimize_preview(self, payload: dict) -> dict:
        return self._quantum_lab_service.quantum_optimize_preview(payload)


    # ------------------------------------------------------------------
    # Phase 6: Goal governor API helpers
    # ------------------------------------------------------------------

    def goal_governor_status(self) -> dict:
        return {
            "enabled": self.settings.enable_goal_governor,
            **self._goal_governor.status(),
        }

    # ------------------------------------------------------------------
    # Phase 5: MARL advisory helpers
    # ------------------------------------------------------------------

    def marl_status(self) -> dict:
        records = self._policy_registry.list_all()
        active = [r for r in records if r["status"] in {"shadow", "paper", "live_canary", "live_approved"}]
        return {
            "enabled": self.settings.enable_marl_lab,
            "policy_count": len(records),
            "active_policy_count": len(active),
            "policies": records,
            "note": "MARL policies are advisory-only. Live order submission requires live_approved status + ExecutionScheduler.",
        }

    def marl_advisory_preview(self, payload: dict) -> dict:
        from trading_platform.rl.env import EnvObservation
        portfolio_pnl = float(payload.get("portfolio_pnl", 0.0))
        open_positions = int(payload.get("open_positions", 0))
        drawdown = float(payload.get("drawdown", 0.0))
        obs = EnvObservation(
            features=[drawdown, portfolio_pnl / max(self.settings.initial_capital, 1.0)],
            portfolio_pnl=portfolio_pnl,
            open_positions=open_positions,
            volatility=float(payload.get("volatility", 0.0)),
            liquidity=float(payload.get("liquidity", 1.0)),
            step=0,
        )
        action_labels = {0: "NOOP", 1: "PROPOSE_ENTRY", 2: "PROPOSE_EXIT", 3: "PROPOSE_HEDGE", 4: "SIZE_UP", 5: "SIZE_DOWN"}
        votes = []
        for rec in self._policy_registry._records.values():
            policy = self._policy_registry.get(rec.policy_id)
            if policy is None:
                continue
            action = policy.act(obs)
            votes.append({
                "policy_id": rec.policy_id,
                "role": rec.role,
                "status": rec.status,
                "action": action,
                "action_label": action_labels.get(action, "UNKNOWN"),
            })
        if votes:
            from collections import Counter
            vote_counts = Counter(v["action"] for v in votes)
            majority_action, majority_count = vote_counts.most_common(1)[0]
            majority = {"action": majority_action, "action_label": action_labels.get(majority_action, "UNKNOWN"), "confidence": majority_count / len(votes)}
        else:
            majority = {"action": 0, "action_label": "NOOP", "confidence": 1.0}
        return {"advisory_only": True, "majority": majority, "votes": votes}

    # ------------------------------------------------------------------
    # Phase 9: Policy management helpers
    # ------------------------------------------------------------------

    def list_policies(self) -> dict:
        return self._policy_service.list_policies()

    def promote_policy(self, payload: dict) -> dict:
        return self._policy_service.promote_policy(payload)

    def rollback_policy(self, payload: dict) -> dict:
        return self._policy_service.rollback_policy(payload)

    def neural_status(self) -> dict:
        return {
            "enabled": self.settings.enable_neural_lab,
            "models": {
                "forecaster": "baseline_ma",
                "volatility": "garch_1_1",
                "tail_risk": "quantile_baseline",
                "correlation": "rolling_corr_30",
            },
        }

    def neural_predict_preview(self, payload: dict) -> dict:
        symbols = payload.get("symbols", ["NIFTY"])
        trace_id = new_trace_id("npreview")
        service = self._neural_service
        if service is None:
            service = NeuralPredictionService(trace_store=self.trace_store)
        bundle = service.predict(trace_id, symbols, {})
        return bundle.to_dict()

    def meta_labeler_status(self) -> dict:
        """Return champion/challenger summary for monitoring and policy UI."""
        return self._meta_labeler.summary()
