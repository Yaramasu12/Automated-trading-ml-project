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


if __name__ == "__main__":
    unittest.main()
