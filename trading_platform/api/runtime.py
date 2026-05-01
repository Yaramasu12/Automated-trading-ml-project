from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone

from trading_platform.ai.agents import ModelPerformance, RetrainingAgent
from trading_platform.backtesting.engine import BacktestConfig, BacktestEngine
from trading_platform.broker.angel_one import AngelOneBrokerClient
from trading_platform.broker.simulated import SimulatedBrokerClient
from trading_platform.config import Settings, load_settings
from trading_platform.data.instrument_master import build_default_universe
from trading_platform.domain.enums import ExecutionMode
from trading_platform.portfolio.ledger import PortfolioLedger
from trading_platform.risk.engine import RiskEngine, RiskLimits


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
        self.instrument_master = build_default_universe()
        self.backtest_engine = BacktestEngine(self.instrument_master)
        self.portfolio = PortfolioLedger(self.settings.initial_capital)
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

    def state(self) -> RuntimeState:
        return RuntimeState(
            execution_mode=self.settings.execution_mode,
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
        }

    def broker_client(self):
        if self.settings.execution_mode == ExecutionMode.LIVE:
            return AngelOneBrokerClient(self.settings)
        return SimulatedBrokerClient()

    def arm_live(self, armed: bool) -> dict:
        if armed and not self.settings.can_submit_live_orders:
            raise ValueError("Live trading requires EXECUTION_MODE=LIVE, LIVE_TRADING_ENABLED=true, and Angel One credentials")
        self.live_armed = armed
        return self.state_payload()

    def set_kill_switch(self, active: bool) -> dict:
        self.kill_switch_active = active
        if active:
            self.live_armed = False
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
        )
        result = self.backtest_engine.run(config)
        return result.to_dict()

    def universe(self) -> list[dict]:
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
            for instrument in self.instrument_master.all()
        ]

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

    def health(self) -> dict:
        return {
            "status": "healthy",
            "state": self.state_payload(),
            "risk_limits": asdict(self.risk_engine.limits),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
