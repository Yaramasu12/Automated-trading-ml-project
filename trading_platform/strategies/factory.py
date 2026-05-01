from __future__ import annotations

from trading_platform.strategies.base import Strategy
from trading_platform.strategies.derivatives import (
    DefinedRiskOptionSpreadStrategy,
    FuturesTrendStrategy,
    VolatilityBreakoutOptionsStrategy,
)
from trading_platform.strategies.equity import EquityMomentumStrategy


class StrategyFactory:
    def __init__(self):
        self._strategies: dict[str, Strategy] = {
            EquityMomentumStrategy.name: EquityMomentumStrategy(),
            FuturesTrendStrategy.name: FuturesTrendStrategy(),
            DefinedRiskOptionSpreadStrategy.name: DefinedRiskOptionSpreadStrategy(),
            VolatilityBreakoutOptionsStrategy.name: VolatilityBreakoutOptionsStrategy(),
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

