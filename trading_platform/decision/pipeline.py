from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone

from trading_platform.ai.agents import MarketRegimeAgent, StrategySelectionAgent
from trading_platform.ai.features import FeatureEngine, FeatureSnapshot
from trading_platform.ai.models import VolatilityForecast, VolatilityForecaster
from trading_platform.data.instrument_master import InstrumentMaster
from trading_platform.data.market_data import SyntheticDataProvider
from trading_platform.domain.enums import ExecutionMode, InstrumentType, OptionType, OrderType, ProductType, Segment
from trading_platform.domain.models import Instrument, MarketBar, OrderIntent, Signal
from trading_platform.portfolio.ledger import PortfolioLedger, PortfolioSnapshot
from trading_platform.risk.engine import RiskDecision, RiskEngine
from trading_platform.strategies.factory import StrategyFactory


@dataclass(frozen=True)
class DecisionCandidate:
    underlying: str
    strategy_name: str
    instrument: Instrument
    signal: Signal | None
    quantity: int
    risk_decision: RiskDecision | None
    reason: str

    def to_dict(self) -> dict:
        return {
            "underlying": self.underlying,
            "strategy_name": self.strategy_name,
            "instrument": {
                "symbol": self.instrument.symbol,
                "exchange": self.instrument.exchange.value,
                "segment": self.instrument.segment.value,
                "type": self.instrument.instrument_type.value,
                "underlying": self.instrument.underlying,
                "expiry": self.instrument.expiry.isoformat() if self.instrument.expiry else None,
                "strike": self.instrument.strike,
                "option_type": self.instrument.option_type.value if self.instrument.option_type else None,
                "lot_size": self.instrument.lot_size,
            },
            "signal": self._signal_payload(),
            "quantity": self.quantity,
            "risk_decision": asdict(self.risk_decision) if self.risk_decision else None,
            "reason": self.reason,
        }

    def _signal_payload(self) -> dict | None:
        if self.signal is None:
            return None
        return {
            "strategy_name": self.signal.strategy_name,
            "symbol": self.signal.symbol,
            "side": self.signal.side.value,
            "confidence": self.signal.confidence,
            "price": self.signal.price,
            "reason": self.signal.reason,
            "created_at": self.signal.created_at.isoformat(),
            "metadata": self.signal.metadata,
        }


@dataclass(frozen=True)
class DecisionScanResult:
    as_of: datetime
    execution_mode: ExecutionMode
    underlying: str
    features: FeatureSnapshot
    regime: str
    volatility_forecast: VolatilityForecast
    selected_strategies: list[str]
    candidates: list[DecisionCandidate]

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "execution_mode": self.execution_mode.value,
            "underlying": self.underlying,
            "features": asdict(self.features),
            "regime": self.regime,
            "volatility_forecast": asdict(self.volatility_forecast),
            "selected_strategies": self.selected_strategies,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


class DecisionPipeline:
    def __init__(
        self,
        instrument_master: InstrumentMaster,
        strategy_factory: StrategyFactory,
        risk_engine: RiskEngine,
        portfolio: PortfolioLedger,
        data_provider: SyntheticDataProvider | None = None,
    ):
        self.instrument_master = instrument_master
        self.strategy_factory = strategy_factory
        self.risk_engine = risk_engine
        self.portfolio = portfolio
        self.data_provider = data_provider or SyntheticDataProvider()
        self.feature_engine = FeatureEngine()
        self.regime_agent = MarketRegimeAgent()
        self.strategy_agent = StrategySelectionAgent()
        self.volatility_forecaster = VolatilityForecaster()

    def scan(
        self,
        underlying: str,
        start: date,
        days: int,
        execution_mode: ExecutionMode,
        live_armed: bool,
        kill_switch_active: bool,
        strategy_names: list[str] | None = None,
    ) -> DecisionScanResult:
        bars = self.data_provider.generate_daily_bars(underlying, start, max(days, 22), self._base_price(underlying))
        features = self.feature_engine.compute(bars)
        regime = self.regime_agent.classify(features)
        selected = strategy_names or self.strategy_agent.choose(regime, underlying)
        closes = [bar.close for bar in bars]
        forecast = self.volatility_forecaster.forecast(closes, "garch_baseline")
        now = self._market_time(bars[-1])
        snapshot = self.portfolio.mark_to_market(now, {underlying: bars[-1].close})
        candidates = [
            self._candidate(
                underlying=underlying,
                strategy_name=strategy_name,
                bars=bars,
                now=now,
                snapshot=snapshot,
                execution_mode=execution_mode,
                live_armed=live_armed,
                kill_switch_active=kill_switch_active,
            )
            for strategy_name in selected
        ]
        return DecisionScanResult(
            as_of=now,
            execution_mode=execution_mode,
            underlying=underlying,
            features=features,
            regime=regime,
            volatility_forecast=forecast,
            selected_strategies=selected,
            candidates=candidates,
        )

    def _candidate(
        self,
        underlying: str,
        strategy_name: str,
        bars: list[MarketBar],
        now: datetime,
        snapshot: PortfolioSnapshot,
        execution_mode: ExecutionMode,
        live_armed: bool,
        kill_switch_active: bool,
    ) -> DecisionCandidate:
        strategy = self.strategy_factory.get(strategy_name)
        instrument = self._select_instrument(strategy_name, underlying, bars[-1], now.date())
        signal = strategy.generate_signal(instrument, bars, now)
        if signal is None:
            return DecisionCandidate(underlying, strategy_name, instrument, None, 0, None, "no_signal")
        quantity = self._position_quantity(snapshot.equity, instrument, signal.price)
        if quantity <= 0:
            return DecisionCandidate(underlying, strategy_name, instrument, signal, 0, None, "quantity_zero")
        intent = OrderIntent(signal, instrument, quantity, OrderType.MARKET, ProductType.INTRADAY)
        risk_decision = self.risk_engine.evaluate(
            intent=intent,
            portfolio=snapshot,
            now=now,
            execution_mode=execution_mode,
            live_armed=live_armed,
            kill_switch_active=kill_switch_active,
        )
        return DecisionCandidate(
            underlying=underlying,
            strategy_name=strategy_name,
            instrument=instrument,
            signal=signal,
            quantity=quantity,
            risk_decision=risk_decision,
            reason=risk_decision.reason,
        )

    def _select_instrument(self, strategy_name: str, underlying: str, bar: MarketBar, as_of: date) -> Instrument:
        strategy = self.strategy_factory.get(strategy_name)
        if strategy.family == "futures":
            return self.instrument_master.select_future(underlying, as_of)
        if strategy.family == "options":
            option_type = OptionType.CE if bar.close >= bar.open else OptionType.PE
            return self.instrument_master.select_option(underlying, as_of, bar.close, option_type)
        instrument = self.instrument_master.get(underlying)
        if instrument.instrument_type == InstrumentType.INDEX or instrument.segment != Segment.CASH:
            return self.instrument_master.select_future(underlying, as_of)
        return instrument

    def _position_quantity(self, equity: float, instrument: Instrument, price: float) -> int:
        budget = equity * 0.02
        lot_notional = max(price * instrument.lot_size, 1)
        return max(1, int(budget / lot_notional))

    def _base_price(self, underlying: str) -> float:
        bases = {
            "NIFTY": 22500,
            "BANKNIFTY": 48500,
            "FINNIFTY": 21500,
            "MIDCPNIFTY": 11800,
            "RELIANCE": 2800,
            "TCS": 3500,
            "INFY": 1500,
            "HDFCBANK": 1600,
            "ICICIBANK": 1100,
            "SBIN": 750,
        }
        return bases.get(underlying, 1000.0)

    def _market_time(self, bar: MarketBar) -> datetime:
        if bar.timestamp.tzinfo is None:
            return bar.timestamp.replace(tzinfo=timezone.utc)
        return bar.timestamp.astimezone(timezone.utc)
