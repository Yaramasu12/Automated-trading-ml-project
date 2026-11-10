"""Phase-5 OPTIONAL paid intraday-option vendor adapter — STUB, DISABLED by
default.

WHY THIS EXISTS
    The free NSE/BSE bhavcopy the rest of the ingest layer uses gives EOD
    (end-of-day) option quotes only. The one thing it fundamentally cannot
    provide is true INTRADAY traded option prices and full historical option
    chains. A paid market-data vendor can. This module is the pluggable seam
    for that vendor: a clean adapter interface + a strict config gate + tests,
    so a real vendor can be dropped in later WITHOUT touching any caller.

WHAT THIS MODULE DOES NOT DO
    It makes NO network call and ships with NO real vendor wired. It is DISABLED
    unless BOTH an explicit env flag is set AND credentials are present AND a
    concrete adapter has been registered for the named vendor. Until then every
    query returns an EMPTY result with status "not_configured". It NEVER
    fabricates, interpolates or synthesises a quote — an unconfigured adapter
    returns nothing, loudly labelled, exactly like a blocked bhavcopy day is
    recorded absent rather than faked.

HOW A VENDOR IS DROPPED IN LATER
    Implement VendorOptionAdapter (its two _fetch_* methods), then register a
    factory with @register_vendor("yourvendor") / register_vendor(...). Set
    VENDOR_OPTIONS_ENABLED=true, VENDOR_OPTIONS_VENDOR=yourvendor and
    VENDOR_OPTIONS_API_KEY=...; get_vendor_adapter() will then return your
    adapter instead of the disabled default. The base class already enforces the
    gate: _fetch_* is only ever reached when is_configured() is True, so a
    vendor implementation never has to re-check the flag.
"""
from __future__ import annotations

import abc
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Callable

logger = logging.getLogger(__name__)

# Config gate env vars (all must be satisfied for the adapter to be live).
ENV_ENABLED = "VENDOR_OPTIONS_ENABLED"
ENV_VENDOR = "VENDOR_OPTIONS_VENDOR"
ENV_API_KEY = "VENDOR_OPTIONS_API_KEY"

_TRUE = {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class VendorOptionQuote:
    """A single intraday / historical traded option quote from a paid vendor.

    Deliberately the labelled, source-tagged shape the EOD store already uses;
    `source` records which vendor produced it so a vendor price is never
    confused with a bhavcopy EOD price."""
    trade_date: date
    underlying: str
    expiry: date | None
    strike: float | None
    option_type: str | None            # "CE" / "PE"
    price: float | None                # last traded price
    timestamp: str | None = None       # ISO intraday timestamp, if intraday
    bid: float | None = None
    ask: float | None = None
    volume: float | None = None
    oi: float | None = None
    implied_vol: float | None = None
    source: str | None = None


@dataclass
class VendorResult:
    """Result envelope. `configured` and `status` make the "not wired yet" state
    explicit and machine-checkable; `quotes` is ALWAYS empty unless a real,
    configured vendor actually returned real data."""
    configured: bool
    status: str                        # "ok" | "not_configured" | "error"
    detail: str
    vendor: str | None = None
    quotes: list[VendorOptionQuote] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "configured": self.configured,
            "status": self.status,
            "detail": self.detail,
            "vendor": self.vendor,
            "quote_count": len(self.quotes),
            "quotes": [asdict(q) for q in self.quotes],
        }


@dataclass(frozen=True)
class VendorConfig:
    enabled: bool
    vendor: str | None
    api_key: str | None

    @property
    def credentials_present(self) -> bool:
        return bool(self.api_key)


def read_vendor_config(env: dict | None = None) -> VendorConfig:
    env = env if env is not None else os.environ
    raw_enabled = (env.get(ENV_ENABLED) or "").strip().lower()
    vendor = (env.get(ENV_VENDOR) or "").strip() or None
    api_key = (env.get(ENV_API_KEY) or "").strip() or None
    return VendorConfig(enabled=raw_enabled in _TRUE, vendor=vendor, api_key=api_key)


class VendorOptionAdapter(abc.ABC):
    """Pluggable interface for a paid intraday-option / historical-chain vendor.

    The public methods enforce the config gate centrally: when the adapter is
    not configured they return an empty, clearly-labelled VendorResult and the
    abstract _fetch_* hooks are never called. A concrete vendor implements only
    the two _fetch_* hooks and is_configured(); it never has to re-check the env
    flag or fabricate anything."""

    name: str = "base"

    @abc.abstractmethod
    def is_configured(self) -> bool:
        """True only when the env flag, credentials and this concrete adapter
        are all in place."""

    def _not_configured_detail(self) -> str:
        return (
            f"vendor options adapter '{self.name}' is not configured — set "
            f"{ENV_ENABLED}=true, {ENV_VENDOR}=<vendor>, {ENV_API_KEY}=<key> and "
            "register a concrete adapter. Returning no data (never fabricated)."
        )

    def status(self) -> VendorResult:
        configured = self.is_configured()
        return VendorResult(
            configured=configured,
            status="ok" if configured else "not_configured",
            detail="configured" if configured else self._not_configured_detail(),
            vendor=self.name if configured else None,
        )

    def intraday_quotes(
        self, underlying: str, trade_date: date, *, expiry: date | None = None,
    ) -> VendorResult:
        if not self.is_configured():
            return VendorResult(False, "not_configured", self._not_configured_detail())
        try:
            quotes = list(self._fetch_intraday(underlying, trade_date, expiry))
        except Exception as exc:  # noqa: BLE001 - surface, never fabricate
            logger.warning("vendor %s intraday fetch failed: %s", self.name, exc)
            return VendorResult(True, "error", str(exc)[:200], vendor=self.name)
        return VendorResult(True, "ok", f"{len(quotes)} quote(s)", vendor=self.name, quotes=quotes)

    def historical_chain(
        self, underlying: str, start: date, end: date, *, expiry: date | None = None,
    ) -> VendorResult:
        if not self.is_configured():
            return VendorResult(False, "not_configured", self._not_configured_detail())
        try:
            quotes = list(self._fetch_historical_chain(underlying, start, end, expiry))
        except Exception as exc:  # noqa: BLE001
            logger.warning("vendor %s historical fetch failed: %s", self.name, exc)
            return VendorResult(True, "error", str(exc)[:200], vendor=self.name)
        return VendorResult(True, "ok", f"{len(quotes)} quote(s)", vendor=self.name, quotes=quotes)

    # --- concrete-vendor hooks (only reached when is_configured() is True) ---

    @abc.abstractmethod
    def _fetch_intraday(
        self, underlying: str, trade_date: date, expiry: date | None,
    ) -> list[VendorOptionQuote]:
        ...

    @abc.abstractmethod
    def _fetch_historical_chain(
        self, underlying: str, start: date, end: date, expiry: date | None,
    ) -> list[VendorOptionQuote]:
        ...


class DisabledVendorAdapter(VendorOptionAdapter):
    """The default adapter when no vendor is configured. is_configured() is
    always False, so every query short-circuits to an empty not_configured
    result. The _fetch_* hooks exist only to satisfy the interface and are
    never reached; if somehow called, they refuse rather than invent data."""

    name = "disabled"

    def __init__(self, reason: str = "") -> None:
        self._reason = reason

    def is_configured(self) -> bool:
        return False

    def _not_configured_detail(self) -> str:
        base = super()._not_configured_detail()
        return f"{base} ({self._reason})" if self._reason else base

    def _fetch_intraday(self, underlying, trade_date, expiry):
        raise RuntimeError("disabled vendor adapter must never fetch")

    def _fetch_historical_chain(self, underlying, start, end, expiry):
        raise RuntimeError("disabled vendor adapter must never fetch")


# Registry of concrete vendor factories. Empty by default — no real vendor ships
# wired. A real adapter registers itself here so the config gate can select it.
VendorFactory = Callable[[VendorConfig], VendorOptionAdapter]
_REGISTRY: dict[str, VendorFactory] = {}


def register_vendor(name: str, factory: VendorFactory | None = None):
    """Register a concrete adapter factory under `name`. Usable directly
    (register_vendor("x", factory)) or as a decorator (@register_vendor("x"))."""
    def _register(fac: VendorFactory) -> VendorFactory:
        _REGISTRY[name.strip().lower()] = fac
        return fac
    if factory is not None:
        return _register(factory)
    return _register


def available_vendors() -> list[str]:
    return sorted(_REGISTRY)


def get_vendor_adapter(env: dict | None = None) -> VendorOptionAdapter:
    """Return the configured adapter, or a DisabledVendorAdapter (with the
    reason) when the gate is not fully satisfied. NEVER raises, NEVER returns
    something that fabricates data when unconfigured."""
    cfg = read_vendor_config(env)
    if not cfg.enabled:
        return DisabledVendorAdapter(reason=f"{ENV_ENABLED} is not set/true")
    if not cfg.vendor:
        return DisabledVendorAdapter(reason=f"{ENV_VENDOR} is unset")
    if not cfg.credentials_present:
        return DisabledVendorAdapter(reason=f"{ENV_API_KEY} is unset")
    factory = _REGISTRY.get(cfg.vendor.lower())
    if factory is None:
        return DisabledVendorAdapter(
            reason=f"no adapter registered for vendor '{cfg.vendor}' "
                   f"(registered: {available_vendors() or 'none'})"
        )
    try:
        return factory(cfg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("vendor factory '%s' failed to build: %s", cfg.vendor, exc)
        return DisabledVendorAdapter(reason=f"factory for '{cfg.vendor}' raised: {exc}")
