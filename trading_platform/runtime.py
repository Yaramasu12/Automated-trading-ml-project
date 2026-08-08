"""
trading_platform/runtime.py — Tenant-aware TradingRuntime (composition root)

Per §13 Phase 3: Runtime = wiring only (<500 lines).
Per §16: Per-tenant services, not global singletons.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

from trading_platform.config import Settings
from trading_platform.data.market_adapter import MarketDataAdapter, get_market_adapter
from trading_platform.streaming.tick_bus import TickBus
from trading_platform.tenancy.broker_session import BrokerSessionManager
from trading_platform.tenancy.portfolio_ledger import PortfolioLedger, SimulatedBrokerClient
from trading_platform.risk.engine import RiskEngine
from trading_platform.strategies.strategy_engine import StrategyEngine, StrategyRegistry
from trading_platform.strategies.strategy_engine import PortfolioAllocator

logger = logging.getLogger(__name__)


class TenantRuntime:
    """
    Per-tenant runtime: all services scoped to one tenant.
    """

    def __init__(
        self,
        tenant_id: str,
        settings: Settings,
        tick_bus: Optional[TickBus] = None,
        broker_session_manager: Optional[BrokerSessionManager] = None,
        risk_engine: Optional[RiskEngine] = None,
        strategy_engine: Optional[StrategyEngine] = None,
        portfolio_ledger: Optional[PortfolioLedger] = None,
        allocator: Optional[PortfolioAllocator] = None,
    ):
        self.tenant_id = tenant_id
        self.settings = settings

        # Market data
        self.tick_bus = tick_bus
        self.market_adapter: Optional[MarketDataAdapter] = None

        # Broker session
        self.broker_session_manager = broker_session_manager or BrokerSessionManager()
        self.broker_session = None

        # Risk
        self.risk_engine = risk_engine

        # Strategy
        self.strategy_registry = StrategyRegistry()
        self.strategy_engine = strategy_engine

        # Portfolio / broker
        self.portfolio_ledger = portfolio_ledger

        # Allocator
        self.allocator = allocator

        # Internal state
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize tenant runtime: market adapter, broker session, risk, strategies."""
        if self._initialized:
            return

        logger.info(f"[RUNTIME] Initializing tenant={self.tenant_id}")

        # 1. Market data adapter
        if self.tick_bus:
            self.market_adapter = get_market_adapter(
                self.settings,
                tick_bus=self.tick_bus,
                tenant_id=self.tenant_id,
            )
            await self.market_adapter.connect()
            logger.info(f"[RUNTIME] Market adapter connected for tenant={self.tenant_id}")

        # 2. Broker session
        self.broker_session = await self.broker_session_manager.initialize_tenant(
            self.tenant_id,
            self.settings.broker or "ANGEL_ONE",
        )
        if self.settings.paper_mode:
            # Use simulated broker for paper trading
            if not self.portfolio_ledger:
                self.portfolio_ledger = SimulatedBrokerClient(
                    tenant_id=self.tenant_id,
                    initial_capital=self.settings.initial_capital or Decimal("1000000"),
                )
            logger.info(f"[RUNTIME] Paper mode enabled for tenant={self.tenant_id}")
        else:
            await self.broker_session.connect()
            if not self.portfolio_ledger:
                self.portfolio_ledger = PortfolioLedger(
                    tenant_id=self.tenant_id,
                    broker_session=self.broker_session,
                )
            logger.info(f"[RUNTIME] Live broker session connected for tenant={self.tenant_id}")

        # 3. Risk engine (shared across tenants if needed, but tenant-scoped limits)
        if not self.risk_engine:
            self.risk_engine = RiskEngine(
                settings=self.settings,
                tenant_id=self.tenant_id,
                portfolio_ledger=self.portfolio_ledger,
            )

        # 4. Strategy engine
        if not self.strategy_engine:
            self.strategy_engine = StrategyEngine(
                registry=self.strategy_registry,
                alert_callback=self._alert,
            )

        # 5. Allocator
        if not self.allocator:
            self.allocator = PortfolioAllocator(
                initial_capital=self.settings.initial_capital or Decimal("1000000"),
                target_vol=0.15,
                max_drawdown=0.10,
                kelly_fraction=0.25,
                alert_callback=self._alert,
            )

        self._initialized = True
        logger.info(f"[RUNTIME] Tenant={self.tenant_id} initialized")

    async def start(self) -> None:
        """Start all services."""
        await self.strategy_engine.start()
        logger.info(f"[RUNTIME] Tenant={self.tenant_id} started")

    async def stop(self) -> None:
        """Stop all services."""
        await self.strategy_engine.stop()
        if self.broker_session:
            await self.broker_session.disconnect()
        if self.market_adapter:
            await self.market_adapter.disconnect()
        logger.info(f"[RUNTIME] Tenant={self.tenant_id} stopped")

    async def deploy_signals(self, signals: list) -> list:
        """Deploy signals through risk → execution."""
        if not self.risk_engine:
            logger.error(f"[RUNTIME] No risk engine for tenant={self.tenant_id}")
            return []

        results = []
        for signal in signals:
            risk_result = await self.risk_engine.evaluate(signal)
            if risk_result.approved:
                order = await self.portfolio_ledger.submit_order(signal, risk_result)
                results.append(order)
            else:
                logger.warning(f"[RUNTIME] Signal rejected: {risk_result.reason}")
        return results

    async def _alert(self, level: str, title: str, message: str) -> None:
        """Alert callback."""
        logger.warning(f"[ALERT tenant={self.tenant_id}] {level}: {title} - {message}")


class RuntimeFactory:
    """
    Factory for creating tenant runtimes.
    Shared infrastructure (tick bus, broker session manager) is injected.
    """

    def __init__(
        self,
        settings: Settings,
        tick_bus: Optional[TickBus] = None,
        broker_session_manager: Optional[BrokerSessionManager] = None,
    ):
        self.settings = settings
        self.tick_bus = tick_bus or TickBus()
        self.broker_session_manager = broker_session_manager or BrokerSessionManager()
        self._runtimes: Dict[str, TenantRuntime] = {}

    async def create(self, tenant_id: str) -> TenantRuntime:
        """Create a tenant runtime."""
        if tenant_id in self._runtimes:
            return self._runtimes[tenant_id]

        runtime = TenantRuntime(
            tenant_id=tenant_id,
            settings=self.settings,
            tick_bus=self.tick_bus,
            broker_session_manager=self.broker_session_manager,
        )
        self._runtimes[tenant_id] = runtime
        return runtime

    async def initialize_tenant(self, tenant_id: str) -> None:
        """Initialize a tenant runtime."""
        runtime = await self.create(tenant_id)
        await runtime.initialize()

    async def start_tenant(self, tenant_id: str) -> None:
        """Start a tenant runtime."""
        runtime = self._runtimes.get(tenant_id)
        if runtime:
            await runtime.start()

    def get_runtime(self, tenant_id: str) -> Optional[TenantRuntime]:
        """Get a tenant runtime."""
        return self._runtimes.get(tenant_id)

    def get_all_runtimes(self) -> Dict[str, TenantRuntime]:
        """Get all tenant runtimes."""
        return dict(self._runtimes)

    async def shutdown(self) -> None:
        """Shutdown all tenant runtimes."""
        for runtime in self._runtimes.values():
            await runtime.stop()
        await self.tick_bus.disconnect()
        logger.info("[RUNTIME] All runtimes shut down")