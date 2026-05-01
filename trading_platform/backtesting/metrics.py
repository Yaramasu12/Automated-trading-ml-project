from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, pstdev

from trading_platform.domain.enums import Side
from trading_platform.domain.models import Trade


@dataclass(frozen=True)
class PerformanceMetrics:
    starting_capital: float
    ending_equity: float
    total_pnl: float
    return_pct: float
    max_drawdown: float
    trade_count: int
    win_rate: float
    profit_factor: float
    sharpe_like: float


def calculate_metrics(starting_capital: float, equity_values: list[float], trades: list[Trade]) -> PerformanceMetrics:
    ending = equity_values[-1] if equity_values else starting_capital
    returns = [
        (equity_values[index] - equity_values[index - 1]) / equity_values[index - 1]
        for index in range(1, len(equity_values))
        if equity_values[index - 1] > 0
    ]
    peak = starting_capital
    max_drawdown = 0.0
    for value in equity_values:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - value) / peak)

    round_trips = []
    open_buy_price = None
    for trade in trades:
        if trade.side == Side.BUY:
            open_buy_price = trade.price
        elif open_buy_price is not None:
            round_trips.append(trade.price - open_buy_price - trade.charges)
            open_buy_price = None

    wins = [pnl for pnl in round_trips if pnl > 0]
    losses = [abs(pnl) for pnl in round_trips if pnl < 0]
    profit_factor = sum(wins) / sum(losses) if losses else (sum(wins) if wins else 0.0)
    sharpe_like = 0.0
    if len(returns) > 1 and pstdev(returns) > 0:
        sharpe_like = mean(returns) / pstdev(returns) * (252 ** 0.5)
    return PerformanceMetrics(
        starting_capital=starting_capital,
        ending_equity=ending,
        total_pnl=ending - starting_capital,
        return_pct=(ending - starting_capital) / starting_capital if starting_capital else 0.0,
        max_drawdown=max_drawdown,
        trade_count=len(trades),
        win_rate=len(wins) / len(round_trips) if round_trips else 0.0,
        profit_factor=profit_factor,
        sharpe_like=sharpe_like,
    )

