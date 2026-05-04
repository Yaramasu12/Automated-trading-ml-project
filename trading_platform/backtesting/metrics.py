from __future__ import annotations

from collections import deque
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


def _round_trip_pnls(trades: list[Trade]) -> list[float]:
    """Compute realised per-round-trip PnL with FIFO matching, per symbol.

    Supports both long (BUY then SELL) and short (SELL then BUY) round trips,
    and partial fills. A "round trip" is the realised PnL on a single closing
    leg matched to one or more opens of the opposite side. Charges on both the
    opening and the closing trade are netted into the realised PnL.
    """
    open_legs: dict[str, deque] = {}
    realised: list[float] = []
    for trade in trades:
        symbol = trade.symbol
        legs = open_legs.setdefault(symbol, deque())
        sign = trade.side.sign  # BUY=+1, SELL=-1
        # Per-unit charge is allocated proportionally over quantity*lot.
        per_unit_charge = trade.charges / max(1, trade.quantity)
        remaining_qty = trade.quantity
        while remaining_qty > 0 and legs and legs[0]["sign"] != sign:
            head = legs[0]
            matched = min(head["quantity"], remaining_qty)
            if head["sign"] > 0:
                # Long open, current close is SELL
                pnl_per_unit = trade.price - head["price"]
            else:
                # Short open, current close is BUY
                pnl_per_unit = head["price"] - trade.price
            pnl = pnl_per_unit * matched - matched * head["per_unit_charge"] - matched * per_unit_charge
            realised.append(pnl)
            head["quantity"] -= matched
            remaining_qty -= matched
            if head["quantity"] == 0:
                legs.popleft()
        if remaining_qty > 0:
            legs.append(
                {
                    "sign": sign,
                    "quantity": remaining_qty,
                    "price": trade.price,
                    "per_unit_charge": per_unit_charge,
                }
            )
    return realised


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

    round_trips = _round_trip_pnls(trades)
    wins = [pnl for pnl in round_trips if pnl > 0]
    losses = [abs(pnl) for pnl in round_trips if pnl < 0]
    if losses:
        profit_factor = sum(wins) / sum(losses)
    elif wins:
        # Convention: when there are wins and no losses, surface a large finite
        # profit factor rather than an honest +inf so downstream JSON encoders
        # do not break. Documented here so callers can interpret the cap.
        profit_factor = float(len(wins))
    else:
        profit_factor = 0.0
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
