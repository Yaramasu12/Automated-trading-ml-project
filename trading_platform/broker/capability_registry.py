from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BrokerCapabilities:
    broker_name: str
    supports_gtt: bool = False
    supports_oco: bool = False
    supports_trailing_stop: bool = False
    supports_bracket_orders: bool = False
    supports_cover_orders: bool = False
    max_orders_per_second: float = 3.0
    max_orders_per_day: int = 200
    supports_options: bool = True
    supports_futures: bool = True
    supports_multi_leg: bool = False


_REGISTRY: dict[str, BrokerCapabilities] = {
    "ANGEL_ONE": BrokerCapabilities(
        broker_name="ANGEL_ONE",
        supports_gtt=True,
        supports_oco=False,
        supports_trailing_stop=False,
        supports_bracket_orders=True,
        supports_cover_orders=True,
        max_orders_per_second=3.0,
        max_orders_per_day=200,
        supports_options=True,
        supports_futures=True,
        supports_multi_leg=False,
    ),
    "SIMULATED": BrokerCapabilities(
        broker_name="SIMULATED",
        supports_gtt=True,
        supports_oco=True,
        supports_trailing_stop=True,
        supports_bracket_orders=True,
        supports_cover_orders=True,
        max_orders_per_second=100.0,
        max_orders_per_day=10000,
        supports_options=True,
        supports_futures=True,
        supports_multi_leg=True,
    ),
}


class BrokerCapabilityRegistry:
    """Knows which broker supports which order types.

    Checked before constructing multi-leg, GTT, or OCO orders to ensure
    the broker can actually handle them. Falls back gracefully to manual
    exit management when native features are unsupported.
    """

    def get(self, broker_name: str) -> BrokerCapabilities:
        return _REGISTRY.get(broker_name.upper(), BrokerCapabilities(broker_name=broker_name))

    def supports_gtt(self, broker_name: str) -> bool:
        return self.get(broker_name).supports_gtt

    def supports_oco(self, broker_name: str) -> bool:
        return self.get(broker_name).supports_oco

    def supports_multi_leg(self, broker_name: str) -> bool:
        return self.get(broker_name).supports_multi_leg

    def rate_limit(self, broker_name: str) -> float:
        return self.get(broker_name).max_orders_per_second

    def register(self, capabilities: BrokerCapabilities) -> None:
        _REGISTRY[capabilities.broker_name.upper()] = capabilities

    def all_brokers(self) -> list[dict]:
        return [
            {
                "broker_name": cap.broker_name,
                "supports_gtt": cap.supports_gtt,
                "supports_oco": cap.supports_oco,
                "supports_trailing_stop": cap.supports_trailing_stop,
                "supports_multi_leg": cap.supports_multi_leg,
                "max_orders_per_second": cap.max_orders_per_second,
                "max_orders_per_day": cap.max_orders_per_day,
            }
            for cap in _REGISTRY.values()
        ]
