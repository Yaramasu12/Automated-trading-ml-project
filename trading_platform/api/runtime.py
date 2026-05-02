from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone

from trading_platform.ai.agents import ModelPerformance, RetrainingAgent, RiskSupervisorAgent
from trading_platform.ai.feature_store import FeatureStore
from trading_platform.ai.models import GARCHForecaster, MetaModel, ModelRegistry, RegimeClassifier, SentimentAnalyzer, VolatilityForecaster
from trading_platform.data.live_feed import LiveTickFeed
from trading_platform.data.persistence import TradingDatabase
from trading_platform.backtesting.charges import ChargesModel
from trading_platform.backtesting.engine import BacktestConfig, BacktestEngine
from trading_platform.backtesting.evaluator import StrategyEvaluator, WalkForwardEvaluator
from trading_platform.broker.angel_one import AngelOneBrokerClient
from trading_platform.broker.simulated import SimulatedBrokerClient
from trading_platform.config import Settings, load_settings
from trading_platform.data.angel_one_history import AngelOneHistoricalDataProvider
from trading_platform.data.angel_one_instruments import AngelOneInstrumentMasterProvider
from trading_platform.data.instrument_master import build_default_universe
from trading_platform.data.market_data import SyntheticDataProvider
from trading_platform.decision.pipeline import DecisionPipeline
from trading_platform.derivatives.engine import ContractSelector, ExpiryCalendar, GreeksCalculator, IVSurfaceBuilder, OptionChainBuilder, RolloverPlanner
from trading_platform.domain.enums import ExecutionMode, OptionType, OrderType, ProductType, Side
from trading_platform.domain.models import OrderIntent, Signal
from trading_platform.execution.router import ExecutionRouter
from trading_platform.monitoring.metrics import OperationalMonitor
from trading_platform.portfolio.target import AnnualTargetTracker
from trading_platform.portfolio.ledger import PortfolioLedger
from trading_platform.risk.engine import RiskEngine, RiskLimits
from trading_platform.strategies.factory import StrategyFactory


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
        self.feature_store = FeatureStore()
        self.iv_surface_builder = IVSurfaceBuilder()
        self.walk_forward_evaluator = WalkForwardEvaluator(self.backtest_engine)
        self.synthetic_data = SyntheticDataProvider()
        self.charges_model = ChargesModel()
        self.monitor = OperationalMonitor()
        self.db = TradingDatabase()
        self.live_feed = LiveTickFeed(self.settings)
        self.monitor.record_event(
            "runtime_started",
            "Trading runtime initialized",
            metadata={"execution_mode": self.execution_mode.value, "broker": self.settings.broker},
        )
        self.decision_pipeline = DecisionPipeline(
            self.instrument_master,
            self.strategy_factory,
            self.risk_engine,
            self.portfolio,
            self.synthetic_data,
        )

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
        if self.execution_mode == ExecutionMode.LIVE:
            return AngelOneBrokerClient(self.settings)
        return self.paper_broker

    def set_execution_mode(self, mode: str) -> dict:
        next_mode = ExecutionMode(mode.upper())
        self.execution_mode = next_mode
        if next_mode != ExecutionMode.LIVE:
            self.live_armed = False
        self.monitor.record_event("execution_mode_changed", f"Runtime mode set to {next_mode.value}")
        return self.state_payload()

    def arm_live(self, armed: bool) -> dict:
        if armed and not self._can_submit_live_orders():
            raise ValueError(
                "Live trading requires EXECUTION_MODE=LIVE, LIVE_TRADING_ENABLED=true, "
                "LIVE_ORDER_CONFIRMATION=I_ACCEPT_REAL_MONEY_LIVE_ORDERS, and Angel One credentials"
            )
        self.live_armed = armed
        self.monitor.record_event("live_arm_changed", f"Live armed set to {armed}", severity="WARN" if armed else "INFO")
        return self.state_payload()

    def set_kill_switch(self, active: bool) -> dict:
        self.kill_switch_active = active
        if active:
            self.live_armed = False
        self.monitor.record_event(
            "kill_switch_changed",
            f"Kill switch set to {active}",
            severity="CRITICAL" if active else "INFO",
        )
        return self.state_payload()

    def run_backtest(self, payload: dict | None = None) -> dict:
        payload = payload or {}
        underlyings = tuple(payload.get("underlyings") or ("NIFTY", "BANKNIFTY", "MIDCPNIFTY", "RELIANCE", "TCS"))
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
        return {
            "cache_path": str(self.angel_one_instruments.cache_path),
            "parsed_count": len(self.instrument_master.instruments),
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
        return OrderIntent(
            signal=signal,
            instrument=instrument,
            quantity=quantity,
            order_type=OrderType(str(payload.get("order_type", "MARKET")).upper()),
            product_type=ProductType(str(payload.get("product_type", "INTRADAY")).upper()),
            limit_price=float(payload["limit_price"]) if payload.get("limit_price") is not None else None,
            stop_loss=float(payload["stop_loss"]) if payload.get("stop_loss") is not None else None,
            target=float(payload["target"]) if payload.get("target") is not None else None,
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
            self.execution_mode == ExecutionMode.LIVE
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
        underlyings = tuple(payload.get("underlyings") or ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "RELIANCE", "TCS"))
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
        underlyings = [str(item).upper() for item in payload.get("underlyings", ["NIFTY", "RELIANCE"])]
        start = date.fromisoformat(str(payload.get("start", "2026-01-01")))
        days = int(payload.get("days", 30))
        strategy_names = [str(item) for item in payload["strategy_names"]] if payload.get("strategy_names") else None
        scans = [
            self.decision_pipeline.scan(
                underlying=underlying,
                start=start,
                days=days,
                execution_mode=self.execution_mode,
                live_armed=self.live_armed,
                kill_switch_active=self.kill_switch_active,
                strategy_names=strategy_names,
            ).to_dict()
            for underlying in underlyings
        ]
        approved = sum(
            1
            for scan in scans
            for candidate in scan["candidates"]
            if candidate["risk_decision"] and candidate["risk_decision"]["approved"]
        )
        rejected = sum(
            1
            for scan in scans
            for candidate in scan["candidates"]
            if candidate["risk_decision"] and not candidate["risk_decision"]["approved"]
        )
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
        underlyings = [str(item).upper() for item in payload.get("underlyings", ["NIFTY", "RELIANCE"])]
        start = date.fromisoformat(str(payload.get("start", "2026-01-01")))
        days = int(payload.get("days", 30))
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
                intent = OrderIntent(
                    signal=candidate.signal,
                    instrument=candidate.instrument,
                    quantity=candidate.quantity,
                    order_type=OrderType.MARKET,
                    product_type=ProductType.INTRADAY,
                )
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
        expiry_date = date.fromisoformat(expiry) if expiry else self.expiry_calendar.nearest(underlying, date.today())
        chain = self.option_chain_builder.build(underlying, expiry_date)
        liquid_strikes = chain.liquid_strikes(spot_price) if spot_price else chain.strikes
        return {
            "underlying": chain.underlying,
            "expiry": chain.expiry.isoformat(),
            "call_count": len(chain.calls),
            "put_count": len(chain.puts),
            "strikes": chain.strikes,
            "liquid_strikes": liquid_strikes,
            "calls": [self._serialize_instrument(instrument) for instrument in chain.calls],
            "puts": [self._serialize_instrument(instrument) for instrument in chain.puts],
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
        forecast = self.volatility_forecaster.forecast(
            closes,
            model_name=str(payload.get("model_name", "ewma_volatility")),
        )
        evaluation = self.volatility_forecaster.evaluate_interval(closes[-min(len(closes), 20):], forecast)
        return {
            "forecast": asdict(forecast),
            "interval_evaluation": asdict(evaluation),
            "policy": {
                "promotion_gate": "Candidate volatility models must keep interval coverage above 90% before live use.",
                "current_use": "baseline risk sizing and IV sanity checks",
            },
        }

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
        evaluation = self.volatility_forecaster.evaluate_interval(closes[-min(len(closes), 20):], forecast)
        params = self.garch_forecaster.fitted_params(closes)
        return {
            "forecast": asdict(forecast),
            "garch_params": params,
            "interval_evaluation": asdict(evaluation),
        }

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
        underlyings = tuple(payload.get("underlyings") or ("NIFTY", "BANKNIFTY"))
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

        if not self.regime_classifier.is_trained:
            records = self.feature_store.all_records()
            if records:
                self.regime_classifier.train(records)

        regime = self.regime_classifier.predict(features)
        proba = self.regime_classifier.predict_proba(features)
        return {
            "symbol": symbol,
            "regime": regime,
            "probabilities": proba,
            "classifier_trained": self.regime_classifier.is_trained,
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
        self.live_feed.register_instruments(self.instrument_master.all())
        self.live_feed.subscribe(symbols)
        self.live_feed.start()
        self.monitor.record_event("live_feed_started", f"Live tick feed started for {len(symbols)} symbols")
        return {"started": True, "symbols": symbols}

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
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
