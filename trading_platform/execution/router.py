from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trading_platform.broker.base import BrokerClient
from trading_platform.domain.enums import ExecutionMode, OrderStatus
from trading_platform.domain.models import Order, OrderIntent, Trade
from trading_platform.portfolio.ledger import PortfolioLedger
from trading_platform.risk.engine import RiskDecision, RiskEngine


@dataclass(frozen=True)
class ExecutionReport:
    order: Order
    risk_decision: RiskDecision
    trade: Trade | None


class ExecutionRouter:
    def __init__(
        self,
        broker: BrokerClient,
        risk_engine: RiskEngine,
        portfolio: PortfolioLedger,
        execution_mode: ExecutionMode,
        live_armed: bool = False,
        kill_switch_active: bool = False,
    ):
        self.broker = broker
        self.risk_engine = risk_engine
        self.portfolio = portfolio
        self.execution_mode = execution_mode
        self.live_armed = live_armed
        self.kill_switch_active = kill_switch_active
        self.orders_sent_today = 0
        self.trades_today = 0

    def submit(self, intent: OrderIntent, now: datetime, mark_prices: dict[str, float], charges: float = 0.0) -> ExecutionReport:
        snapshot = self.portfolio.mark_to_market(now, mark_prices)
        risk_decision = self.risk_engine.evaluate(
            intent=intent,
            portfolio=snapshot,
            now=now,
            execution_mode=self.execution_mode,
            live_armed=self.live_armed,
            kill_switch_active=self.kill_switch_active,
            orders_sent_today=self.orders_sent_today,
            trades_today=max(1, self.trades_today),
        )
        order = Order(intent=intent)
        if not risk_decision.approved:
            order.status = OrderStatus.RISK_REJECTED
            order.rejection_reason = risk_decision.reason
            return ExecutionReport(order, risk_decision, None)

        self.orders_sent_today += 1
        result = self.broker.submit_order(intent)
        order.status = result.status
        order.broker_order_id = result.broker_order_id
        order.submitted_at = result.submitted_at
        order.acknowledged_at = result.acknowledged_at
        order.average_price = result.average_price
        order.rejection_reason = None if result.status != OrderStatus.REJECTED else result.message

        trade = None
        if result.status == OrderStatus.FILLED and result.average_price is not None:
            trade = self.portfolio.apply_fill(order, result.average_price, now, charges)
            self.trades_today += 1
        return ExecutionReport(order, risk_decision, trade)

