from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

from trading_platform.agent.market_hours import is_entry_allowed
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

if TYPE_CHECKING:
    from trading_platform.ai.feature_store import FeatureStore
    from trading_platform.ai.models import MetaModel, RegimeClassifier
    from trading_platform.data.angel_one_history import AngelOneHistoricalDataProvider

logger = logging.getLogger(__name__)


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
        live_feed=None,
        history_provider: "AngelOneHistoricalDataProvider | None" = None,
        feature_store: "FeatureStore | None" = None,
        regime_classifier: "RegimeClassifier | None" = None,
        meta_model: "MetaModel | None" = None,
    ):
        self.instrument_master = instrument_master
        self.strategy_factory = strategy_factory
        self.risk_engine = risk_engine
        self.portfolio = portfolio
        self.data_provider = data_provider or SyntheticDataProvider()
        self.live_feed = live_feed
        self.history_provider = history_provider
        self.feature_store = feature_store
        self.regime_classifier = regime_classifier
        self.meta_model = meta_model
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
        # Enforce market hours only for armed live entry generation. Paper and
        # unarmed live-readiness scans must still produce candidates so the
        # risk layer can prove it will reject or approve them correctly.
        if execution_mode.value.startswith("LIVE") and live_armed and not is_entry_allowed():
            now = datetime.now(timezone.utc)
            return DecisionScanResult(
                as_of=now,
                execution_mode=execution_mode,
                underlying=underlying,
                features=self.feature_engine.compute(
                    self.data_provider.generate_daily_bars(underlying, start, max(days, 22), self._base_price(underlying))
                ),
                regime="MARKET_CLOSED",
                volatility_forecast=self.volatility_forecaster.forecast(
                    [self._base_price(underlying)] * 30, "garch_baseline"
                ),
                selected_strategies=[],
                candidates=[],
            )
        bars = self._fetch_bars(underlying, start, days)
        # Override last bar's price with real live tick (keep bar timestamp unchanged)
        if self.live_feed is not None:
            tick = self.live_feed.latest_tick(underlying)
            if tick is not None and tick.last_price > 0:
                last = bars[-1]
                bars[-1] = MarketBar(
                    timestamp=last.timestamp,
                    symbol=last.symbol,
                    open=tick.open if tick.open > 0 else last.open,
                    high=max(last.high, tick.high if tick.high > 0 else tick.last_price),
                    low=min(last.low, tick.low if tick.low > 0 else tick.last_price),
                    close=tick.last_price,
                    volume=tick.volume if tick.volume > 0 else last.volume,
                )
        features = self.feature_engine.compute(bars)

        # Use ML regime classifier when it has been trained and validated on
        # external labels; otherwise fall back to the deterministic rule agent.
        if self.regime_classifier is not None and self.regime_classifier.is_trained:
            regime = self.regime_classifier.predict(features)
        else:
            regime = self.regime_agent.classify(features)

        # Use MetaModel-ranked strategies when enough feedback has accumulated;
        # otherwise fall back to the fixed strategy-selection mapping.
        if strategy_names:
            selected = strategy_names
        elif self.meta_model is not None:
            base = self.strategy_agent.choose(regime, underlying)
            # Rank the rule-selected candidates by observed P&L scores per regime.
            ranked = self.meta_model.rank(regime, base)
            selected = [item.strategy_name for item in ranked]
        else:
            selected = self.strategy_agent.choose(regime, underlying)

        closes = [bar.close for bar in bars]
        forecast = self.volatility_forecaster.forecast(closes, "garch_baseline")

        # FIX: use actual current time for risk checks (expiry cutoff, etc.),
        # NOT the synthetic bar timestamp which may be days/weeks in the future.
        now = datetime.now(timezone.utc)

        snapshot = self.portfolio.mark_to_market(now, {underlying: bars[-1].close})
        # Persist features for ML training / drift detection
        if self.feature_store is not None:
            try:
                self.feature_store.append(underlying, now.date(), features, regime)
            except Exception as exc:
                logger.debug("feature_store.append failed for %s: %s", underlying, exc)
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

    def _fetch_bars(self, underlying: str, start: date, days: int) -> list[MarketBar]:
        """Fetch historical bars from Angel One; fall back to synthetic if unavailable."""
        min_bars = max(days, 22)
        if self.history_provider is not None:
            try:
                instrument = self.instrument_master.get(underlying)
                from datetime import datetime as _dt
                from_dt = _dt.combine(start, _dt.min.time())
                to_dt = _dt.combine(date.today(), _dt.min.time())
                bars = self.history_provider.get_candles(instrument, from_dt, to_dt, interval="ONE_DAY")
                if len(bars) >= min_bars:
                    return bars[-min_bars:]
                logger.debug("Angel One returned only %d bars for %s, using synthetic", len(bars), underlying)
            except Exception as exc:
                logger.debug("Angel One historical fetch failed for %s: %s — using synthetic", underlying, exc)
        return self.data_provider.generate_daily_bars(underlying, start, min_bars, self._base_price(underlying))

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
            try:
                return self.instrument_master.select_future(underlying, as_of)
            except ValueError:
                return self.instrument_master.get(underlying)
        if strategy.family == "options":
            try:
                option_type = OptionType.CE if bar.close >= bar.open else OptionType.PE
                return self.instrument_master.select_option(underlying, as_of, bar.close, option_type)
            except ValueError:
                return self.instrument_master.get(underlying)
        instrument = self.instrument_master.get(underlying)
        if instrument.instrument_type == InstrumentType.INDEX or instrument.segment != Segment.CASH:
            try:
                return self.instrument_master.select_future(underlying, as_of)
            except ValueError:
                pass
        return instrument

    # SPAN margin rates by instrument type (approximate NSE requirements)
    _FUTURES_MARGIN_PCT = 0.12   # ~12% of notional for index/equity futures
    _OPTIONS_MARGIN_PCT = 1.00   # options buyers pay full premium

    def _position_quantity(self, equity: float, instrument: Instrument, price: float) -> int:
        budget = equity * 0.02
        if instrument.instrument_type == InstrumentType.FUTURE:
            # For futures, lot_notional is full contract value but only ~12% margin is required.
            # Size so that the margin deployed ≤ budget, not the full notional.
            margin_per_lot = max(price * instrument.lot_size * self._FUTURES_MARGIN_PCT, 1)
            return max(1, int(budget / margin_per_lot))
        lot_notional = max(price * instrument.lot_size, 1)
        return max(1, int(budget / lot_notional))

    def _base_price(self, underlying: str) -> float:
        bases = {
            "NIFTY": 22500,
            "BANKNIFTY": 48500,
            "FINNIFTY": 23000,
            "MIDCPNIFTY": 12500,
            "SENSEX": 80000,
            "BANKEX": 58000,
            "RELIANCE": 2900,
            "TCS": 3800,
            "INFY": 1600,
            "HDFCBANK": 1700,
            "ICICIBANK": 1200,
            "SBIN": 800,
            "WIPRO": 480,
            "KOTAKBANK": 1900,
            "AXISBANK": 1100,
            "MARUTI": 12000,
            "SUNPHARMA": 1700,
            "TATAMOTORS": 750,
            "BAJFINANCE": 7000,
            "HINDUNILVR": 2400,
            "BHARTIARTL": 1600,
            "NTPC": 350,
            "POWERGRID": 330,
            "TITAN": 3300,
            "ASIANPAINT": 2400,
            "LTIM": 5500,
            "ONGC": 280,
        }
        return bases.get(underlying, 1000.0)

    def _market_time(self, bar: MarketBar) -> datetime:
        if bar.timestamp.tzinfo is None:
            return bar.timestamp.replace(tzinfo=timezone.utc)
        return bar.timestamp.astimezone(timezone.utc)
