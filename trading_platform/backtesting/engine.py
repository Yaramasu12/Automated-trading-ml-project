from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta

logger = logging.getLogger(__name__)

from trading_platform.ai.agents import MarketRegimeAgent, StrategySelectionAgent
from trading_platform.ai.features import FeatureEngine
from trading_platform.backtesting.charges import ChargesModel
from trading_platform.backtesting.metrics import PerformanceMetrics, calculate_metrics
from trading_platform.broker.simulated import SimulatedBrokerClient
from trading_platform.data.instrument_master import INDEX_UNDERLYINGS, InstrumentMaster, build_default_universe
from trading_platform.data.market_data import SyntheticDataProvider
from trading_platform.derivatives.engine import ImpliedVolatilityCalculator
from trading_platform.domain.enums import ExecutionMode, InstrumentType, OptionType, OrderType, ProductType, Segment, Side
from trading_platform.domain.models import MarketBar, OrderIntent, Signal
from trading_platform.execution.router import ExecutionReport, ExecutionRouter
from trading_platform.exit.exit_plan import ExitPlan
from trading_platform.portfolio.ledger import PortfolioLedger
from trading_platform.risk.engine import RiskEngine, RiskLimits
from trading_platform.strategies.factory import StrategyFactory
from trading_platform.agent.market_hours import now_ist


@dataclass(frozen=True)
class BacktestConfig:
    starting_capital: float = 1_000_000
    start: date = date(2026, 1, 1)
    # H1: was 30 — only 9 effective trading bars after 21-bar warmup. 90 gives ~60 trading bars.
    days: int = 90
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


@dataclass
class _PositionExitState:
    """Exit-plan tracking for one open position during the backtest loop."""
    plan: ExitPlan
    bars_held: int = 0


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
        self._default_master_cache: dict[date, InstrumentMaster] = {}
        if self._uses_default_master:
            self._default_master_cache[now_ist().date()] = self.instrument_master
        self._iv_calculator = ImpliedVolatilityCalculator()

    def run(
        self,
        config: BacktestConfig,
        signal_filter=None,
    ) -> BacktestResult:
        """Run a backtest.

        Fixes applied vs prior version:
          C1  Per-bar exit simulation: stop-loss, target, and max_holding_days
              are checked against each bar's high/low before new entries.
          C2  Daily loss circuit breaker: session_start_equity is set at bar
              open so the risk engine correctly measures intrabar P&L.
          C3  Futures position sizing: margin-based budget; no forced 1-lot
              minimum; cumulative SPAN margin cap prevents over-leverage.
          C4  Instrument dedup: two strategies routing to the same contract
              on the same bar only place one order.
          C5  Existing-position guard: no re-entry while a position is open.
          H1  Default days raised to 90 for ~60 effective trading bars.
          H4  Marks dict enriched with derivative prices before every
              router.submit call so the risk engine sees correct MTM.
          M2  Daily order/trade counters reset at bar open.
          M3  Trade timestamps use actual generated trading-day dates, not
              calendar-day offsets that skip weekends incorrectly.

        Signal lookahead fix (unchanged): signals computed on bars[0..t-1],
        orders filled at bar[t].open.
        """
        if self._uses_default_master:
            cached = self._default_master_cache.get(config.start)
            if cached is None:
                cached = build_default_universe(config.start)
                self._default_master_cache[config.start] = cached
            self.instrument_master = cached

        bars_by_underlying = self.data_provider.generate_many(
            config.underlyings, config.start, config.days
        )

        portfolio = PortfolioLedger(config.starting_capital)
        risk = RiskEngine(
            RiskLimits(
                max_drawdown=config.max_drawdown,
                # C3: 20% per-order SPAN cap (NIFTY=~14%, BANKNIFTY=~10%).
                # Total cumulative exposure is capped in the loop below.
                max_futures_margin_pct=0.20,
                max_position_pct=0.05,
            )
        )
        router = ExecutionRouter(
            broker=SimulatedBrokerClient(),
            risk_engine=risk,
            portfolio=portfolio,
            execution_mode=ExecutionMode.BACKTEST,
            live_armed=False,
        )
        reports: list[ExecutionReport] = []
        selected: dict[str, list[str]] = {}
        entry_context: dict[str, tuple[float, float, date]] = {}

        # C1: active exit plans — symbol → _PositionExitState
        exit_states: dict[str, _PositionExitState] = {}

        # M3: use the first underlying's generated bars as trading calendar
        # so timestamps come from actual trading days, not calendar offsets.
        calendar_bars = bars_by_underlying[config.underlyings[0]]

        for bar_index in range(21, len(calendar_bars)):
            # M3: real trading-day date from the generated bar timestamp
            bar_date = calendar_bars[bar_index].timestamp.date()
            now = datetime.combine(bar_date, time(10, 0))

            # Build underlying close prices for this bar
            underlying_marks: dict[str, float] = {
                sym: bars[bar_index].close
                for sym, bars in bars_by_underlying.items()
                if bar_index < len(bars)
            }
            # H4: enrich with derivative marks so the risk engine sees true MTM
            full_marks = self._with_position_marks(
                portfolio, underlying_marks, entry_context, as_of=bar_date
            )

            # C2: set session-start equity so per-bar daily-loss gate fires
            bar_open_snap = portfolio.mark_to_market(now, full_marks)
            router.set_session_start_equity(bar_open_snap.equity)

            # M2: reset daily counters at bar open
            router.orders_sent_today = 0
            router.trades_today = 0

            # ── C1: Simulate exits for open positions ────────────────────────
            for symbol, es in list(exit_states.items()):
                pos = portfolio.positions.get(symbol)
                if pos is None or pos.quantity == 0:
                    exit_states.pop(symbol, None)
                    continue

                es.bars_held += 1

                # Use the underlying's bar to detect intrabar SL/target hits
                underlying_sym = es.plan.instrument.underlying or symbol
                u_bars = bars_by_underlying.get(underlying_sym, [])
                if bar_index < len(u_bars):
                    bar_low = u_bars[bar_index].low
                    bar_high = u_bars[bar_index].high
                else:
                    bar_low = bar_high = full_marks.get(symbol, es.plan.entry_price)

                plan = es.plan
                # Determine max_holding_days from strategy exit rules
                try:
                    exit_rules = self.strategy_factory.get(plan.strategy_name).exit_rules()
                    max_holding = exit_rules.max_holding_days
                except (KeyError, AttributeError):
                    max_holding = 5

                triggered = False
                exit_price = full_marks.get(symbol, es.plan.entry_price)

                if plan.side == "BUY":
                    if plan.stop_loss_price is not None and bar_low <= plan.stop_loss_price:
                        exit_price = plan.stop_loss_price
                        triggered = True
                    elif plan.target_price is not None and bar_high >= plan.target_price:
                        exit_price = plan.target_price
                        triggered = True
                else:  # SHORT
                    if plan.stop_loss_price is not None and bar_high >= plan.stop_loss_price:
                        exit_price = plan.stop_loss_price
                        triggered = True
                    elif plan.target_price is not None and bar_low <= plan.target_price:
                        exit_price = plan.target_price
                        triggered = True

                if not triggered and es.bars_held >= max_holding:
                    triggered = True  # max holding days reached

                if triggered:
                    exit_side = Side.SELL if plan.side == "BUY" else Side.BUY
                    exit_signal = Signal(
                        strategy_name=f"{plan.strategy_name}:exit",
                        symbol=symbol,
                        side=exit_side,
                        confidence=1.0,
                        price=exit_price,
                        reason="backtest exit simulation",
                        created_at=now,
                        metadata={"opens_position": False, "hedged": True},
                    )
                    exit_intent = OrderIntent(
                        signal=exit_signal,
                        instrument=pos.instrument,
                        quantity=abs(pos.quantity),
                        order_type=OrderType.MARKET,
                        product_type=ProductType.INTRADAY,
                    )
                    exit_charges = self.charges_model.estimate(exit_intent, exit_price)
                    exit_marks = self._with_position_marks(
                        portfolio, underlying_marks, entry_context, as_of=bar_date
                    )
                    exit_marks[symbol] = exit_price
                    exit_report = router.submit(exit_intent, now, exit_marks, exit_charges)
                    reports.append(exit_report)
                    exit_states.pop(symbol, None)

            # ── Process new entries ──────────────────────────────────────────
            # C4: instruments already entered this bar (dedup same contract)
            instruments_entered_this_bar: set[str] = set()

            # C3: cumulative SPAN margin already consumed by open futures
            total_futures_margin_used = sum(
                abs(pos.quantity) * pos.average_price * pos.instrument.lot_size * 0.12
                for pos in portfolio.positions.values()
                if pos.quantity != 0
                and pos.instrument.instrument_type == InstrumentType.FUTURE
            )
            # Cap: 30% of starting capital in total futures SPAN margin
            futures_margin_cap = config.starting_capital * 0.30

            for underlying in config.underlyings:
                full_bars_list = bars_by_underlying[underlying]
                if bar_index >= len(full_bars_list):
                    continue
                history_bars = full_bars_list[:bar_index]
                if len(history_bars) < 21:
                    continue
                features = self.feature_engine.compute(history_bars)
                regime = self.regime_agent.classify(features)
                strategy_names = list(
                    config.strategy_names or self.strategy_agent.choose(regime, underlying)
                )
                selected[underlying] = strategy_names
                execution_bar = full_bars_list[bar_index]

                for strategy_name in strategy_names[:2]:
                    strategy = self.strategy_factory.get(strategy_name)

                    # Futures strategies are designed for index contracts (NIFTY, BANKNIFTY).
                    # Individual equity underlyings have lot sizes of 175–750 shares, producing
                    # notional exposure of 300K–800K per lot — inappropriate for a 1M account.
                    # Route equity underlyings through equity strategies only.
                    if strategy.family == "futures" and underlying not in INDEX_UNDERLYINGS:
                        continue

                    instrument = self._select_instrument(
                        strategy_name, underlying, history_bars[-1], bar_date
                    )
                    sym = instrument.symbol

                    # C4: skip if this exact instrument already entered this bar
                    if sym in instruments_entered_this_bar:
                        continue

                    # C5: skip if position already open
                    existing = portfolio.positions.get(sym)
                    if existing is not None and existing.quantity != 0:
                        continue

                    signal = strategy.generate_signal(instrument, history_bars, now)
                    if signal is None or signal.confidence < 0.65:
                        continue
                    if signal_filter is not None and not signal_filter(signal):
                        continue

                    fill_reference_price = execution_bar.open
                    signal = replace(signal, price=fill_reference_price)

                    quantity = self._position_quantity(
                        config.starting_capital, instrument, fill_reference_price
                    )
                    if quantity <= 0:
                        continue

                    # C3: block futures entry when cumulative SPAN margin cap hit.
                    # Check full position margin (quantity × per-lot) not just 1 lot.
                    if instrument.instrument_type == InstrumentType.FUTURE:
                        this_position_margin = fill_reference_price * instrument.lot_size * 0.12 * quantity
                        if total_futures_margin_used + this_position_margin > futures_margin_cap:
                            logger.debug(
                                "Backtest: skipping %s — cumulative margin cap hit "
                                "(used=%.0f + this=%.0f > cap=%.0f)",
                                sym, total_futures_margin_used, this_position_margin, futures_margin_cap,
                            )
                            continue

                    intent = OrderIntent(
                        signal=signal,
                        instrument=instrument,
                        quantity=quantity,
                        order_type=OrderType.MARKET,
                        product_type=ProductType.INTRADAY,
                    )
                    # H4: use derivative-enriched marks for every submit call
                    entry_marks = self._with_position_marks(
                        portfolio, underlying_marks, entry_context, as_of=bar_date
                    )
                    entry_marks[sym] = fill_reference_price
                    charges = self.charges_model.estimate(intent, fill_reference_price)
                    report = router.submit(intent, now, entry_marks, charges)
                    reports.append(report)

                    if report.trade is not None:
                        instruments_entered_this_bar.add(sym)
                        # C3: update running margin total
                        if instrument.instrument_type == InstrumentType.FUTURE:
                            total_futures_margin_used += (
                                fill_reference_price * instrument.lot_size * 0.12 * quantity
                            )
                        # Record options context for IV fitting on exit
                        if (
                            instrument.instrument_type == InstrumentType.OPTION
                            and sym not in entry_context
                        ):
                            underlying_at_entry = underlying_marks.get(
                                instrument.underlying or sym
                            )
                            if underlying_at_entry and underlying_at_entry > 0:
                                entry_context[sym] = (
                                    underlying_at_entry,
                                    report.trade.price,
                                    bar_date,
                                )
                        # C1: create exit plan for each filled entry
                        exit_rules = strategy.exit_rules()
                        atr = getattr(features, "atr_14", None)
                        exit_plan = ExitPlan.from_trade(
                            trade=report.trade,
                            instrument=instrument,
                            stop_loss_pct=exit_rules.stop_loss_pct,
                            target_pct=exit_rules.target_pct,
                            atr=atr if atr and atr > 0 else None,
                        )
                        exit_states[sym] = _PositionExitState(plan=exit_plan, bars_held=0)

            # End-of-bar portfolio mark
            end_marks = self._with_position_marks(
                portfolio, underlying_marks, entry_context, as_of=bar_date
            )
            portfolio.mark_to_market(now, end_marks)

        # ── Forced EOD liquidation of remaining positions ────────────────────
        final_underlying_marks = {
            sym: bars[-1].close
            for sym, bars in bars_by_underlying.items()
            if bars
        }
        final_bar_date = calendar_bars[-1].timestamp.date()
        final_time = datetime.combine(final_bar_date, time(15, 20))

        for symbol, position in list(portfolio.positions.items()):
            if position.quantity == 0:
                continue
            mark_price = self._mark_price_for_instrument(
                position.instrument,
                final_underlying_marks,
                position.average_price,
                entry_context=entry_context,
                as_of=final_time.date(),
            )
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
            eod_marks = self._with_position_marks(
                portfolio, final_underlying_marks, entry_context, as_of=final_time.date()
            )
            eod_marks[symbol] = mark_price
            reports.append(router.submit(intent, final_time, eod_marks, charges))

        portfolio.mark_to_market(
            final_time,
            self._with_position_marks(
                portfolio, final_underlying_marks, entry_context, as_of=final_time.date()
            ),
        )

        equity_values = [value for _, value in portfolio.equity_curve]
        metrics = calculate_metrics(
            config.starting_capital, equity_values, list(portfolio.trades)
        )
        return BacktestResult(
            config=config, metrics=metrics, reports=reports, selected_strategies=selected
        )

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
        # C3: futures sized by SPAN margin budget; no forced minimum so
        # undercapitalised lot sizes are cleanly skipped (quantity=0 → caller skips).
        if instrument.instrument_type == InstrumentType.FUTURE:
            margin_budget = capital * 0.10   # 10% of capital per futures position
            margin_per_lot = max(price * instrument.lot_size * 0.12, 1)
            return int(margin_budget / margin_per_lot)
        budget = capital * 0.02
        lot_notional = max(price * instrument.lot_size, 1)
        return max(1, int(budget / lot_notional))

    def _mark_price_for_instrument(
        self,
        instrument,
        underlying_marks: dict[str, float],
        fallback: float,
        entry_context: dict[str, tuple[float, float, date]] | None = None,
        as_of: date | None = None,
    ) -> float:
        underlying = instrument.underlying or instrument.symbol
        underlying_price = underlying_marks.get(underlying, fallback)
        if instrument.instrument_type != InstrumentType.OPTION:
            return underlying_price

        strike = instrument.strike
        option_type = instrument.option_type
        expiry = instrument.expiry
        if strike is None or option_type is None:
            return max(1.0, underlying_price * 0.015)

        as_of = as_of or now_ist().date()
        if expiry is not None and expiry <= as_of:
            if option_type == OptionType.CE:
                intrinsic = max(0.0, underlying_price - strike)
            else:
                intrinsic = max(0.0, strike - underlying_price)
            return max(intrinsic, instrument.tick_size or 0.05)

        days_to_expiry = max(1, (expiry - as_of).days) if expiry is not None else 30
        sigma = 0.25
        ctx = (entry_context or {}).get(instrument.symbol)
        if ctx is not None:
            entry_spot, entry_option_price, entry_date = ctx
            entry_dte = max(1, (expiry - entry_date).days) if expiry is not None else 30
            try:
                sigma = self._iv_calculator.calculate(
                    market_price=max(0.05, entry_option_price),
                    spot=max(0.01, entry_spot),
                    strike=strike,
                    days_to_expiry=entry_dte,
                    option_type=option_type,
                )
            except Exception:
                logger.debug("IV calc failed for strike=%s; σ=0.25", strike, exc_info=True)
                sigma = 0.25
        try:
            t = days_to_expiry / 365.0
            price = self._iv_calculator._bs_price(
                spot=max(0.01, underlying_price),
                strike=strike,
                t=t,
                sigma=max(0.005, sigma),
                option_type=option_type,
                r=0.06,
            )
        except Exception:
            logger.debug("BS pricing failed for strike=%s; 1.5%% fallback", strike, exc_info=True)
            price = underlying_price * 0.015
        return max(price, instrument.tick_size or 0.05)

    def _with_position_marks(
        self,
        portfolio: PortfolioLedger,
        underlying_marks: dict[str, float],
        entry_context: dict[str, tuple[float, float, date]] | None = None,
        as_of: date | None = None,
    ) -> dict[str, float]:
        marks = dict(underlying_marks)
        for symbol, position in portfolio.positions.items():
            if position.quantity != 0:
                marks[symbol] = self._mark_price_for_instrument(
                    position.instrument,
                    underlying_marks,
                    position.average_price,
                    entry_context=entry_context,
                    as_of=as_of,
                )
        return marks
