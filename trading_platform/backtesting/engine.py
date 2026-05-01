from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta

from trading_platform.ai.agents import MarketRegimeAgent, StrategySelectionAgent
from trading_platform.ai.features import FeatureEngine
from trading_platform.backtesting.charges import ChargesModel
from trading_platform.backtesting.metrics import PerformanceMetrics, calculate_metrics
from trading_platform.broker.simulated import SimulatedBrokerClient
from trading_platform.data.instrument_master import InstrumentMaster, build_default_universe
from trading_platform.data.market_data import SyntheticDataProvider
from trading_platform.domain.enums import ExecutionMode, InstrumentType, OptionType, OrderType, ProductType, Segment, Side
from trading_platform.domain.models import MarketBar, OrderIntent, Signal
from trading_platform.execution.router import ExecutionReport, ExecutionRouter
from trading_platform.portfolio.ledger import PortfolioLedger
from trading_platform.risk.engine import RiskEngine, RiskLimits
from trading_platform.strategies.factory import StrategyFactory


@dataclass(frozen=True)
class BacktestConfig:
    starting_capital: float = 1_000_000
    start: date = date(2026, 1, 1)
    days: int = 30
    underlyings: tuple[str, ...] = ("NIFTY", "BANKNIFTY", "MIDCPNIFTY", "RELIANCE", "TCS")
    max_drawdown: float = 0.10


@dataclass(frozen=True)
class BacktestResult:
    config: BacktestConfig
    metrics: PerformanceMetrics
    reports: list[ExecutionReport]
    selected_strategies: dict[str, list[str]]

    def to_dict(self) -> dict:
        return {
            "config": {
                **asdict(self.config),
                "start": self.config.start.isoformat(),
                "underlyings": list(self.config.underlyings),
            },
            "metrics": asdict(self.metrics),
            "orders": len(self.reports),
            "selected_strategies": self.selected_strategies,
        }


class BacktestEngine:
    def __init__(
        self,
        instrument_master: InstrumentMaster | None = None,
        strategy_factory: StrategyFactory | None = None,
        data_provider: SyntheticDataProvider | None = None,
    ):
        self._uses_default_master = instrument_master is None
        self.instrument_master = instrument_master or build_default_universe()
        self.strategy_factory = strategy_factory or StrategyFactory()
        self.data_provider = data_provider or SyntheticDataProvider()
        self.feature_engine = FeatureEngine()
        self.regime_agent = MarketRegimeAgent()
        self.strategy_agent = StrategySelectionAgent()
        self.charges_model = ChargesModel()

    def run(self, config: BacktestConfig) -> BacktestResult:
        if self._uses_default_master:
            self.instrument_master = build_default_universe(config.start)
        bars_by_underlying = self.data_provider.generate_many(config.underlyings, config.start, config.days)
        portfolio = PortfolioLedger(config.starting_capital)
        risk = RiskEngine(RiskLimits(max_drawdown=config.max_drawdown))
        router = ExecutionRouter(
            broker=SimulatedBrokerClient(),
            risk_engine=risk,
            portfolio=portfolio,
            execution_mode=ExecutionMode.BACKTEST,
            live_armed=False,
        )
        reports: list[ExecutionReport] = []
        selected: dict[str, list[str]] = {}

        for bar_index in range(21, config.days):
            marks = {
                symbol: bars[min(bar_index, len(bars) - 1)].close
                for symbol, bars in bars_by_underlying.items()
                if bars
            }
            now = datetime.combine(config.start + timedelta(days=bar_index), time(10, 0))
            for underlying in config.underlyings:
                bars = bars_by_underlying[underlying][: bar_index + 1]
                if len(bars) < 21:
                    continue
                features = self.feature_engine.compute(bars)
                regime = self.regime_agent.classify(features)
                strategy_names = self.strategy_agent.choose(regime, underlying)
                selected[underlying] = strategy_names
                for strategy_name in strategy_names[:2]:
                    instrument = self._select_instrument(strategy_name, underlying, bars[-1], now.date())
                    strategy = self.strategy_factory.get(strategy_name)
                    signal = strategy.generate_signal(instrument, bars, now)
                    if signal is None or signal.confidence < 0.55:
                        continue
                    quantity = self._position_quantity(config.starting_capital, instrument, signal.price)
                    if quantity <= 0:
                        continue
                    intent = OrderIntent(
                        signal=signal,
                        instrument=instrument,
                        quantity=quantity,
                        order_type=OrderType.MARKET,
                        product_type=ProductType.INTRADAY,
                    )
                    charges = self.charges_model.estimate(intent, signal.price)
                    report = router.submit(intent, now, {**marks, instrument.symbol: signal.price}, charges)
                    reports.append(report)
            portfolio.mark_to_market(now, self._with_position_marks(portfolio, marks))

        final_underlying_marks = {
            symbol: bars[-1].close
            for symbol, bars in bars_by_underlying.items()
            if bars
        }
        final_time = datetime.combine(config.start + timedelta(days=config.days + 1), time(15, 20))
        for symbol, position in list(portfolio.positions.items()):
            if position.quantity == 0:
                continue
            mark_price = self._mark_price_for_instrument(position.instrument, final_underlying_marks, position.average_price)
            exit_side = Side.SELL if position.quantity > 0 else Side.BUY
            exit_signal = Signal(
                strategy_name="forced_backtest_exit",
                symbol=symbol,
                side=exit_side,
                confidence=1.0,
                price=mark_price,
                reason="forced end-of-backtest liquidation",
                created_at=final_time,
                metadata={"opens_position": False, "hedged": True},
            )
            intent = OrderIntent(
                signal=exit_signal,
                instrument=position.instrument,
                quantity=abs(position.quantity),
                order_type=OrderType.MARKET,
                product_type=ProductType.INTRADAY,
            )
            charges = self.charges_model.estimate(intent, mark_price)
            reports.append(router.submit(intent, final_time, {**final_underlying_marks, symbol: mark_price}, charges))
        portfolio.mark_to_market(final_time, self._with_position_marks(portfolio, final_underlying_marks))

        equity_values = [value for _, value in portfolio.equity_curve]
        metrics = calculate_metrics(config.starting_capital, equity_values, portfolio.trades)
        return BacktestResult(config=config, metrics=metrics, reports=reports, selected_strategies=selected)

    def _select_instrument(self, strategy_name: str, underlying: str, bar: MarketBar, as_of: date):
        if strategy_name == "futures_trend":
            return self.instrument_master.select_future(underlying, as_of)
        if "option" in strategy_name:
            option_type = OptionType.CE if bar.close >= bar.open else OptionType.PE
            return self.instrument_master.select_option(underlying, as_of, bar.close, option_type)
        instrument = self.instrument_master.get(underlying)
        if instrument.instrument_type == InstrumentType.INDEX or instrument.segment != Segment.CASH:
            return self.instrument_master.select_future(underlying, as_of)
        return instrument

    def _position_quantity(self, capital: float, instrument, price: float) -> int:
        budget = capital * 0.02
        lot_notional = max(price * instrument.lot_size, 1)
        return max(1, int(budget / lot_notional))

    def _mark_price_for_instrument(self, instrument, underlying_marks: dict[str, float], fallback: float) -> float:
        underlying = instrument.underlying or instrument.symbol
        underlying_price = underlying_marks.get(underlying, fallback)
        if instrument.instrument_type == InstrumentType.OPTION:
            return max(1.0, underlying_price * 0.015)
        return underlying_price

    def _with_position_marks(self, portfolio: PortfolioLedger, underlying_marks: dict[str, float]) -> dict[str, float]:
        marks = dict(underlying_marks)
        for symbol, position in portfolio.positions.items():
            if position.quantity != 0:
                marks[symbol] = self._mark_price_for_instrument(
                    position.instrument,
                    underlying_marks,
                    position.average_price,
                )
        return marks
