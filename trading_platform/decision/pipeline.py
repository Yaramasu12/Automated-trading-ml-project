from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

from trading_platform.agent.market_hours import is_entry_allowed, now_ist
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
from trading_platform.logging_safety import note_swallowed

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
            meta_scores: dict[str, float] = {}
        elif self.meta_model is not None:
            base = self.strategy_agent.choose(regime, underlying)
            ranked = self.meta_model.rank(regime, base)
            # Skip strategies the MetaModel has learned are consistently losing
            # in this regime (score < 0.35 with enough observations = 5+ trades)
            selected = []
            meta_scores = {}
            for item in ranked:
                obs = self.meta_model.observation_count(regime, item.strategy_name)
                if obs >= 5 and item.score < 0.35:
                    logger.debug(
                        "MetaModel pruned %s in %s regime (score=%.2f obs=%d)",
                        item.strategy_name, regime, item.score, obs,
                    )
                    continue
                selected.append(item.strategy_name)
                meta_scores[item.strategy_name] = item.score
            if not selected:  # all pruned — fall back to rule selection
                selected = base
                meta_scores = {}
        else:
            selected = self.strategy_agent.choose(regime, underlying)
            meta_scores = {}

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
                features=features,
                meta_score=meta_scores.get(strategy_name),
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

    def get_regime(self, underlying: str) -> dict:
        """Return the current market regime for an underlying as a dict.

        Uses ML classifier if trained, else falls back to rule-based agent.
        Returns {'regime': str, 'confidence': float}.
        """
        try:
            bars = self._fetch_bars(underlying, now_ist().date(), 30)
            features = self.feature_engine.compute(bars)
            if self.regime_classifier is not None and self.regime_classifier.is_trained:
                regime = self.regime_classifier.predict(features)
                confidence = 0.75
            else:
                regime = self.regime_agent.classify(features)
                confidence = 0.60
            return {"regime": regime, "confidence": confidence}
        except Exception as exc:
            logger.debug("get_regime failed for %s: %s", underlying, exc)
            return {"regime": "unknown", "confidence": 0.5}

    def _fetch_bars(self, underlying: str, start: date, days: int) -> list[MarketBar]:
        """Fetch historical bars from Angel One; fall back to synthetic if unavailable."""
        min_bars = max(days, 22)
        if self.history_provider is not None:
            try:
                instrument = self.instrument_master.get(underlying)
                from datetime import datetime as _dt
                from_dt = _dt.combine(start, _dt.min.time())
                to_dt = _dt.combine(now_ist().date(), _dt.min.time())
                bars = self.history_provider.get_candles(instrument, from_dt, to_dt, interval="ONE_DAY")
                if len(bars) >= min_bars:
                    return bars[-min_bars:]
                logger.debug("Angel One returned only %d bars for %s, using synthetic", len(bars), underlying)
            except Exception as exc:
                logger.debug("Angel One historical fetch failed for %s: %s — using synthetic", underlying, exc)
        return self.data_provider.generate_daily_bars(underlying, start, min_bars, self._base_price(underlying))

    # Minimum signal confidence to proceed to risk evaluation
    _MIN_CONFIDENCE = 0.65
    # Annualised vol cap: skip entries when market is in extreme volatility
    _MAX_ENTRY_ANNUALISED_VOL = 0.60   # 60% annualised (~3.8% daily)

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
        features: "FeatureSnapshot | None" = None,
        meta_score: float | None = None,
    ) -> DecisionCandidate:
        strategy = self.strategy_factory.get(strategy_name)
        instrument = self._select_instrument(strategy_name, underlying, bars[-1], now.date())
        signal = strategy.generate_signal(instrument, bars, now)
        if signal is None:
            return DecisionCandidate(underlying, strategy_name, instrument, None, 0, None, "no_signal")

        # Apply MetaModel score as confidence adjustment:
        # - score > 0.6 → slight boost (+0.03) for high-performing strategies
        # - score < 0.45 → slight penalty (−0.03) for underperformers
        # - score not available → neutral (new strategies not penalized)
        if meta_score is not None and meta_score > 0:
            from dataclasses import replace as _replace
            if meta_score > 0.60:
                adj = min(0.04, (meta_score - 0.60) * 0.2)
                signal = _replace(signal, confidence=min(0.97, signal.confidence + adj))
            elif meta_score < 0.45:
                adj = min(0.04, (0.45 - meta_score) * 0.2)
                signal = _replace(signal, confidence=max(0.0, signal.confidence - adj))

        # Gate 1: confidence filter — drop weak signals before any further processing
        if signal.confidence < self._MIN_CONFIDENCE:
            return DecisionCandidate(
                underlying, strategy_name, instrument, signal, 0, None,
                f"low_confidence:{signal.confidence:.2f}<{self._MIN_CONFIDENCE}",
            )

        # Gate 2: PRICE SANITY — reject if signal price is stale/synthetic vs live tick.
        # Applies to EQUITY, INDEX, and FUTURES (futures signal price = underlying close).
        # OPTIONS are excluded because option premium ≠ underlying spot price.
        # For futures the underlying tick is the correct reference (signal.price tracks
        # the underlying, not the derivative quote).
        if self.live_feed is not None and instrument.instrument_type != InstrumentType.OPTION:
            try:
                tick = self.live_feed.latest_tick(underlying)
                if tick is not None:
                    lp = tick.last_price if hasattr(tick, "last_price") else tick.get("last_price", 0)
                    if lp and lp > 0 and signal.price > 0:
                        price_error = abs(signal.price - lp) / lp
                        if price_error > 0.08:   # >8% away from live tick = stale/synthetic price
                            return DecisionCandidate(
                                underlying, strategy_name, instrument, signal, 0, None,
                                f"price_sanity_fail:signal={signal.price:.0f}_tick={lp:.0f}_err={price_error:.1%}",
                            )
                elif instrument.instrument_type != InstrumentType.OPTION and (
                    execution_mode.value.startswith("LIVE") or bool(getattr(self.live_feed, "is_running", False))
                ):
                    # No live tick available — block futures entries entirely (they carry the
                    # highest notional risk and are most sensitive to price errors).
                    if instrument.instrument_type == InstrumentType.FUTURE:
                        return DecisionCandidate(
                            underlying, strategy_name, instrument, signal, 0, None,
                            "no_live_tick:futures_entry_blocked_without_price_confirmation",
                        )
            except Exception as exc:
                note_swallowed("pipeline.futures_tick_check", exc)

        # Gate 3: volatility blowout filter — annualised vol > 60% = avoid new entries
        if features is not None:
            ann_vol = features.realized_volatility * (252 ** 0.5)
            if ann_vol > self._MAX_ENTRY_ANNUALISED_VOL:
                return DecisionCandidate(
                    underlying, strategy_name, instrument, signal, 0, None,
                    f"volatility_blowout:{ann_vol:.1%}>{self._MAX_ENTRY_ANNUALISED_VOL:.0%}",
                )

        quantity = self._position_quantity(snapshot.equity, instrument, signal.price, features)
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

    def _position_quantity(
        self,
        equity: float,
        instrument: Instrument,
        price: float,
        features: "FeatureSnapshot | None" = None,
    ) -> int:
        base_pct = 0.02
        # Volatility-adjusted sizing: scale down in high-vol, scale up in calm markets
        # Target daily risk = 0.5% of equity; position = target_risk / realized_vol
        if features is not None and features.realized_volatility > 0:
            target_daily_risk = equity * 0.005
            vol_sized_budget = target_daily_risk / features.realized_volatility
            # Clamp: never more than 3% or less than 0.5% of equity regardless
            budget = max(equity * 0.005, min(equity * 0.03, vol_sized_budget))
        else:
            budget = equity * base_pct
        if instrument.instrument_type == InstrumentType.FUTURE:
            margin_per_lot = max(price * instrument.lot_size * self._FUTURES_MARGIN_PCT, 1)
            return max(1, int(budget / margin_per_lot))
        lot_notional = max(price * instrument.lot_size, 1)
        return max(1, int(budget / lot_notional))

    def _base_price(self, underlying: str) -> float:
        # Single source of truth: SyntheticDataProvider._BASE_PRICES in data/market_data.py.
        # Prefer live tick price when available — it is always more accurate than any hardcoded value.
        if self.live_feed is not None:
            try:
                tick = self.live_feed.latest_tick(underlying)
                if tick is not None:
                    lp = tick.last_price if hasattr(tick, "last_price") else tick.get("last_price", 0)
                    if lp and lp > 0:
                        return float(lp)
            except Exception as exc:
                note_swallowed("pipeline.latest_tick_price", exc)
        return self.data_provider._BASE_PRICES.get(underlying, 1000.0)

    def _market_time(self, bar: MarketBar) -> datetime:
        if bar.timestamp.tzinfo is None:
            return bar.timestamp.replace(tzinfo=timezone.utc)
        return bar.timestamp.astimezone(timezone.utc)
