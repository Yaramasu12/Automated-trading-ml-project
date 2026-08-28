"""Regression test for LiveFeedService._resolve_feed_symbols.

Bug found 2026-07-28: a bare commodity symbol like "GOLD" can match a
non-tradable reference entry in Angel One's instrument master (an NCDEX
COMDTY entry with a fake, non-numeric token) instead of the real, ticking
MCX futures contract. The old code picked whichever candidate came first
regardless of whether it was actually a live instrument, silently
subscribing a symbol that never receives ticks — permanently stale on the
dashboard — while the correctly-resolved futures contract, added later by
a different code path, ticked fine as a redundant duplicate.
"""
import unittest
from dataclasses import dataclass
from datetime import date

from trading_platform.api.live_feed_service import LiveFeedService
from trading_platform.domain.enums import AssetClass, Exchange, InstrumentType, Segment
from trading_platform.domain.models import Instrument


def _instrument(symbol: str, token: str, exchange=Exchange.MCX, segment=Segment.FUTURES) -> Instrument:
    return Instrument(
        symbol=symbol, name=symbol, exchange=exchange, segment=segment,
        asset_class=AssetClass.COMMODITY, instrument_type=InstrumentType.COMMODITY_FUTURE,
        token=token,
    )


@dataclass
class _FakeInstrumentMaster:
    instruments: dict
    future_by_underlying: dict

    def select_future(self, underlying: str, as_of: date) -> Instrument:
        inst = self.future_by_underlying.get(underlying)
        if inst is None:
            raise ValueError(f"no future for {underlying}")
        return inst


def _service(master: _FakeInstrumentMaster) -> LiveFeedService:
    return LiveFeedService(
        live_feed=None, instrument_master=master, instrument_freshness=None,
        monitor=None, settings=None,
        can_submit_live_orders=lambda: False,
        load_cached_instruments=lambda: False,
        get_execution_mode=lambda: None,
    )


class ResolveFeedSymbolsTests(unittest.TestCase):
    def test_fake_token_bare_match_falls_through_to_futures(self):
        """The exact bug: bare "GOLD" matches a junk reference (non-numeric
        token) — must resolve to the real futures contract instead, not the
        dead reference."""
        gold_future = _instrument("GOLD05AUG26FUT", token="58217")
        master = _FakeInstrumentMaster(
            instruments={
                "GOLD": _instrument("GOLD", token="GOLD", segment=Segment.CASH),  # fake token, like the real NCDEX entry
            },
            future_by_underlying={"GOLD": gold_future},
        )
        resolved = _service(master)._resolve_feed_symbols(["GOLD"])
        self.assertEqual(resolved, ["GOLD05AUG26FUT"])

    def test_real_numeric_token_bare_match_is_used_directly(self):
        """A genuinely tradable bare-symbol match (real numeric token) should
        be used as-is — this must not regress index/equity resolution."""
        master = _FakeInstrumentMaster(
            instruments={"NIFTY": _instrument("NIFTY", token="99926000", exchange=Exchange.NSE)},
            future_by_underlying={},
        )
        resolved = _service(master)._resolve_feed_symbols(["NIFTY"])
        self.assertEqual(resolved, ["NIFTY"])

    def test_cash_equity_suffix_preferred_over_futures_when_valid(self):
        """RELIANCE-EQ (real cash token) must still be preferred over the
        futures contract for equities — only fake-token matches should fall
        through, not every symbol that also has a futures contract."""
        master = _FakeInstrumentMaster(
            instruments={"RELIANCE-EQ": _instrument("RELIANCE-EQ", token="2885", exchange=Exchange.NSE, segment=Segment.CASH)},
            future_by_underlying={"RELIANCE": _instrument("RELIANCE26AUGFUT", token="41234", exchange=Exchange.NFO)},
        )
        resolved = _service(master)._resolve_feed_symbols(["RELIANCE"])
        self.assertEqual(resolved, ["RELIANCE-EQ"])

    def test_no_valid_candidate_at_all_is_dropped(self):
        master = _FakeInstrumentMaster(instruments={}, future_by_underlying={})
        resolved = _service(master)._resolve_feed_symbols(["NOTASYMBOL"])
        self.assertEqual(resolved, [])


class _FakeLiveFeed:
    def snapshot(self) -> dict:
        return {}


class _FakeSettings:
    live_feed_default_symbols = []
    live_feed_max_symbols = 100


class _ModeHolder:
    """Mimics TradingRuntime.execution_mode: an attribute that changes value
    in place (via set_execution_mode/arm_live) without the holding object
    itself being reconstructed."""

    def __init__(self, value):
        self.value = value


class ExecutionModeReadThroughCallableTests(unittest.TestCase):
    """2026-08-22 architecture review: TradingRuntime's decomposition
    invariant is that execution_mode/live_armed change WITHOUT a service
    rebuild, so api/*_service.py must read them through an injected
    CALLABLE, never capture the value at construction time (unlike
    instrument_master, which IS replaced wholesale on refresh, so services
    holding it ARE rebuilt — see LiveFeedService's own module docstring).
    A service that captured the value instead would keep reporting the mode
    the runtime was in when it happened to be constructed, forever.

    LiveFeedService.live_feed_snapshot()'s get_execution_mode callable is the
    representative case: this test flips the underlying mode AFTER
    construction and confirms the service observes the change immediately,
    with no reconstruction — pinning the pattern so a future extraction from
    TradingRuntime can't silently regress it back to a captured value."""

    def test_service_observes_mode_change_without_reconstruction(self):
        from trading_platform.domain.enums import ExecutionMode

        mode_holder = _ModeHolder(ExecutionMode.PAPER)
        service = LiveFeedService(
            live_feed=_FakeLiveFeed(), instrument_master=None, instrument_freshness=None,
            monitor=None, settings=_FakeSettings(),
            can_submit_live_orders=lambda: False,
            load_cached_instruments=lambda: False,
            get_execution_mode=lambda: mode_holder.value,   # callable, not a captured value
        )

        self.assertEqual(service.live_feed_snapshot()["mode"], "paper_market_data")

        # The runtime flips execution_mode in place (TradingRuntime.set_execution_mode
        # does exactly this: self.execution_mode = next_mode) -- no service rebuild.
        mode_holder.value = ExecutionMode.LIVE

        self.assertEqual(
            service.live_feed_snapshot()["mode"], "live_market_data",
            "service must read the CURRENT mode through the callable, not one captured at construction",
        )


if __name__ == "__main__":
    unittest.main()
