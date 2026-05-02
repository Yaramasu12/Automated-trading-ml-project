from __future__ import annotations

from trading_platform.strategies.base import Strategy
from trading_platform.strategies.derivatives import (
    BearPutSpreadStrategy,
    BreakoutStrategy,
    BullCallSpreadStrategy,
    CalendarSpreadStrategy,
    DefinedRiskOptionSpreadStrategy,
    DeltaNeutralHedgeStrategy,
    ExpiryRolloverStrategy,
    FuturesTrendStrategy,
    FuturesHedgeStrategy,
    IronCondorStrategy,
    LongStraddleStrategy,
    MeanReversionStrategy,
    PairHedgeStrategy,
    ShortStraddleStrategy,
    StrangleStrategy,
    VolatilityBreakoutOptionsStrategy,
)
from trading_platform.strategies.equity import EquityMomentumStrategy, GapStrategy, SwingTrendStrategy


class StrategyFactory:
    def __init__(self):
        self._strategies: dict[str, Strategy] = {
            EquityMomentumStrategy.name: EquityMomentumStrategy(),
            SwingTrendStrategy.name: SwingTrendStrategy(),
            GapStrategy.name: GapStrategy(),
            MeanReversionStrategy.name: MeanReversionStrategy(),
            BreakoutStrategy.name: BreakoutStrategy(),
            FuturesTrendStrategy.name: FuturesTrendStrategy(),
            FuturesHedgeStrategy.name: FuturesHedgeStrategy(),
            PairHedgeStrategy.name: PairHedgeStrategy(),
            ExpiryRolloverStrategy.name: ExpiryRolloverStrategy(),
            DefinedRiskOptionSpreadStrategy.name: DefinedRiskOptionSpreadStrategy(),
            VolatilityBreakoutOptionsStrategy.name: VolatilityBreakoutOptionsStrategy(),
            LongStraddleStrategy.name: LongStraddleStrategy(),
            ShortStraddleStrategy.name: ShortStraddleStrategy(),
            StrangleStrategy.name: StrangleStrategy(),
            IronCondorStrategy.name: IronCondorStrategy(),
            BullCallSpreadStrategy.name: BullCallSpreadStrategy(),
            BearPutSpreadStrategy.name: BearPutSpreadStrategy(),
            CalendarSpreadStrategy.name: CalendarSpreadStrategy(),
            DeltaNeutralHedgeStrategy.name: DeltaNeutralHedgeStrategy(),
        }

    def get(self, name: str) -> Strategy:
        try:
            return self._strategies[name]
        except KeyError as exc:
            raise KeyError(f"Unknown strategy: {name}") from exc

    def all(self) -> list[Strategy]:
        return list(self._strategies.values())

    def names(self) -> list[str]:
        return list(self._strategies.keys())

    def catalog(self) -> list[dict]:
        return [
            {
                "name": strategy.name,
                "family": strategy.family,
                "supports_rollover": strategy.supports_rollover,
                "allows_short_options": strategy.allows_short_options,
                "exit_rules": strategy.exit_rules().__dict__,
                "expiry_rules": strategy.expiry_rules(),
            }
            for strategy in self._strategies.values()
        ]
