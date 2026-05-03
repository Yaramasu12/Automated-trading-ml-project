from __future__ import annotations

from dataclasses import asdict, dataclass, replace
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
    strategy_names: tuple[str, ...] | None = None


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

    def run(
        self,
        config: BacktestConfig,
        signal_filter=None,
    ) -> BacktestResult:
        """Run a backtest.

        `signal_filter` is an optional callable `(signal) -> bool` that the
        WalkForwardEvaluator passes in after fitting on the train window.
        Signals are dropped before order construction when the filter rejects
        them. This is what makes train→test "predict" step real instead of
        just running two disjoint backtests.

        Lookahead fix: signals are computed on bars `0 .. t-1` (i.e. NOT
        peeking at bar `t`'s close), and the resulting order is filled at the
        bar `t` open. Previously the engine computed features through bar `t`
        and filled at that same close, which is a peek-at-close lookahead.
        """
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

        # NOTE: signals at decision-time `t` look at bars[: t] (exclusive of
        # the current bar), and orders are filled at bars[t].open. This avoids
        # the close-of-bar peek bias that would let a signal trade at the same
        # close it was generated from.
        for bar_index in range(21, config.days):
            marks = {
                symbol: bars[min(bar_index, len(bars) - 1)].close
                for symbol, bars in bars_by_underlying.items()
                if bars
            }
            now = datetime.combine(config.start + timedelta(days=bar_index), time(10, 0))
            for underlying in config.underlyings:
                full_bars = bars_by_underlying[underlying]
                if bar_index >= len(full_bars):
                    continue
                # Signal-generation bars are bar_index-exclusive (no peek).
                history_bars = full_bars[:bar_index]
                if len(history_bars) < 21:
                    continue
                features = self.feature_engine.compute(history_bars)
                regime = self.regime_agent.classify(features)
                strategy_names = list(config.strategy_names or self.strategy_agent.choose(regime, underlying))
                selected[underlying] = strategy_names
                # The bar we will fill against (today's open).
                execution_bar = full_bars[bar_index]
                for strategy_name in strategy_names[:2]:
                    instrument = self._select_instrument(strategy_name, underlying, history_bars[-1], now.date())
                    strategy = self.strategy_factory.get(strategy_name)
                    signal = strategy.generate_signal(instrument, history_bars, now)
                    if signal is None or signal.confidence < 0.55:
                        continue
                    if signal_filter is not None and not signal_filter(signal):
                        continue
                    # Force execution at next bar's open price (no close lookahead),
                    # regardless of the strategy's referenced "signal price".
                    fill_reference_price = execution_bar.open
                    signal = replace(signal, price=fill_reference_price)
                    quantity = self._position_quantity(config.starting_capital, instrument, fill_reference_price)
                    if quantity <= 0:
                        continue
                    intent = OrderIntent(
                        signal=signal,
                        instrument=instrument,
                        quantity=quantity,
                        order_type=OrderType.MARKET,
                        product_type=ProductType.INTRADAY,
                    )
                    charges = self.charges_model.estimate(intent, fill_reference_price)
                    report = router.submit(intent, now, {**marks, instrument.symbol: fill_reference_price}, charges)
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
