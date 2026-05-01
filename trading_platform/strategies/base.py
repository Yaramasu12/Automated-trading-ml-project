from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from trading_platform.ai.features import FeatureEngine
from trading_platform.domain.models import Instrument, MarketBar, Signal


class Strategy(ABC):
    name: str

    def __init__(self):
        self.feature_engine = FeatureEngine()

    @abstractmethod
    def generate_signal(
        self,
        instrument: Instrument,
        bars: list[MarketBar],
        now: datetime,
    ) -> Signal | None:
        raise NotImplementedError

