from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean, stdev, pstdev

from trading_platform.domain.models import MarketBar


@dataclass(frozen=True)
class FeatureSnapshot:
    symbol: str
    close: float
    momentum_5: float
    momentum_20: float
    realized_volatility: float
    volume_ratio: float
    trend_strength: float
    # Extended features for better signal quality
    rsi_14: float = 50.0          # RSI(14): overbought>70, oversold<30
    atr_14: float = 0.0           # Average True Range (14-bar) in price units
    bb_width: float = 0.0         # Bollinger Band width = (upper-lower)/mid, proxy for vol regime
    momentum_alignment: float = 0.0  # sign(mom_5)*sign(mom_20): +1 aligned, -1 conflicting


class FeatureEngine:
    def compute(self, bars: list[MarketBar]) -> FeatureSnapshot:
        if len(bars) < 21:
            raise ValueError("At least 21 bars are required for feature computation")
        closes = [bar.close for bar in bars]
        volumes = [bar.volume for bar in bars]
        returns = [
            (closes[index] - closes[index - 1]) / closes[index - 1]
            for index in range(1, len(closes))
            if closes[index - 1] > 0
        ]
        close = closes[-1]
        momentum_5 = (close - closes[-6]) / closes[-6]
        momentum_20 = (close - closes[-21]) / closes[-21]
        recent_vol = pstdev(returns[-20:]) if len(returns) >= 20 else 0.0
        volume_ratio = volumes[-1] / max(1.0, mean(volumes[-20:]))
        trend_strength = abs(momentum_20) / max(recent_vol, 0.0001)

        rsi_14 = self._rsi(closes, 14)
        atr_14 = self._atr(bars, 14)
        bb_width = self._bollinger_width(closes, 20)
        mom_align = (1.0 if momentum_5 > 0 else -1.0) * (1.0 if momentum_20 > 0 else -1.0)

        return FeatureSnapshot(
            symbol=bars[-1].symbol,
            close=close,
            momentum_5=momentum_5,
            momentum_20=momentum_20,
            realized_volatility=recent_vol,
            volume_ratio=volume_ratio,
            trend_strength=trend_strength,
            rsi_14=rsi_14,
            atr_14=atr_14,
            bb_width=bb_width,
            momentum_alignment=mom_align,
        )

    @staticmethod
    def _rsi(closes: list[float], period: int = 14) -> float:
        """Wilder's RSI using exponential smoothing (industry-standard method).

        Step 1: seed with simple average over first `period` bars.
        Step 2: apply Wilder's smoothing (alpha = 1/period) for all remaining bars.
        This matches TradingView, Bloomberg, and NSE charting RSI exactly.
        """
        if len(closes) < period + 1:
            return 50.0
        diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        # Seed: simple average of first `period` up/down moves
        seed_gains = [max(d, 0.0) for d in diffs[:period]]
        seed_losses = [max(-d, 0.0) for d in diffs[:period]]
        avg_gain = sum(seed_gains) / period
        avg_loss = sum(seed_losses) / period
        # Wilder smoothing over remaining bars
        alpha = 1.0 / period
        for d in diffs[period:]:
            gain = max(d, 0.0)
            loss = max(-d, 0.0)
            avg_gain = avg_gain * (1 - alpha) + gain * alpha
            avg_loss = avg_loss * (1 - alpha) + loss * alpha
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def _atr(bars: list[MarketBar], period: int = 14) -> float:
        if len(bars) < period + 1:
            return 0.0
        trs = []
        for i in range(-period, 0):
            high = bars[i].high
            low = bars[i].low
            prev_close = bars[i - 1].close
            trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        return mean(trs)

    @staticmethod
    def _bollinger_width(closes: list[float], period: int = 20) -> float:
        if len(closes) < period:
            return 0.0
        window = closes[-period:]
        mid = mean(window)
        if mid == 0:
            return 0.0
        # Bollinger Bands use sample std (N-1 denominator), not population std
        s = stdev(window) if len(window) > 1 else 0.0
        return (4.0 * s) / mid  # (upper - lower) / mid = 4σ / mid

