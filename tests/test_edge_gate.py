"""EdgeGate: the disciplined entry policy (approved trade list + VRP regime).

Locks in the behaviour that stops short-vol from trading blind: entries are
refused unless the (family, underlying) pair is promoted AND the underlying's
trailing-60d premium clears theta. Exits are never blocked, and missing regime
data fails CLOSED. See trading_platform/risk/edge_gate.py and
audit_results/2026-09-19/premium_regime_filter.md.
"""
from __future__ import annotations

from datetime import date

from trading_platform.risk.edge_gate import (
    EdgeGate,
    EdgePolicy,
    PremiumSnapshot,
    family_of,
    underlying_of,
)

# The study's own verdicts as of 2026-09-18 (premium_regime_filter.md):
#   FINNIFTY trailing 6.02 >= theta 4 -> TRADE;  NIFTY 0.85 < 2, BANKNIFTY -0.14 < 1 -> STAND ASIDE.
SPEC = "short_vol:FINNIFTY:4.0, short_vol:NIFTY:2.0, short_vol:BANKNIFTY:1.0"


def _gate(**kw) -> EdgeGate:
    return EdgeGate.from_spec(SPEC, **kw)


def _snap(underlying: str, trailing: float | None, stale: int = 0) -> PremiumSnapshot:
    return PremiumSnapshot(
        underlying=underlying,
        trade_date=date(2026, 9, 18),
        premium=trailing,
        trailing_median_60d=trailing,
        stale_days=stale,
    )


# ---- helpers -----------------------------------------------------------------

def test_family_of_collapses_short_vol_variants():
    assert family_of("short_vol_condor") == "short_vol"
    assert family_of("short_vol_exit") == "short_vol"
    assert family_of("short_vol") == "short_vol"
    assert family_of("SHORT_VOL_CONDOR".lower()) == "short_vol"
    assert family_of("some_unknown_strategy") == "some_unknown_strategy"


def test_underlying_of_prefers_explicit_then_prefix():
    assert underlying_of("FINNIFTY13OCT2625500CE") == "FINNIFTY"
    assert underlying_of("MIDCPNIFTY30SEP26..CE") == "MIDCPNIFTY"  # not shadowed by NIFTY
    assert underlying_of("NIFTY13OCT2623500CE") == "NIFTY"
    assert underlying_of("ignored", explicit="banknifty") == "BANKNIFTY"
    assert underlying_of("RELIANCE") == "RELIANCE"  # equity falls back to ticker


# ---- the core rule -----------------------------------------------------------

def test_exit_is_never_blocked_even_with_no_data():
    r = _gate().evaluate(strategy_name="short_vol_exit", underlying="NIFTY",
                         opens_position=False, premium=None)
    assert r.allowed and r.code == "exit_or_reduction"


def test_finnifty_entry_allowed_when_premium_rich():
    r = _gate().evaluate(strategy_name="short_vol_condor", underlying="FINNIFTY",
                         opens_position=True, premium=_snap("FINNIFTY", 6.02))
    assert r.allowed and r.code == "edge_validated"


def test_finnifty_entry_refused_when_premium_below_theta():
    r = _gate().evaluate(strategy_name="short_vol_condor", underlying="FINNIFTY",
                         opens_position=True, premium=_snap("FINNIFTY", 3.5))
    assert not r.allowed and r.code == "regime_stand_aside"


def test_nifty_stand_aside_matches_study_verdict():
    # NIFTY trailing 0.85 < theta 2 -> STAND ASIDE
    r = _gate().evaluate(strategy_name="short_vol_condor", underlying="NIFTY",
                         opens_position=True, premium=_snap("NIFTY", 0.85))
    assert not r.allowed and r.code == "regime_stand_aside"


def test_unpromoted_pair_refused():
    # MIDCPNIFTY is not on the approved list at all (negative Sharpe in the study).
    r = _gate().evaluate(strategy_name="short_vol_condor", underlying="MIDCPNIFTY",
                         opens_position=True, premium=_snap("MIDCPNIFTY", 10.0))
    assert not r.allowed and r.code == "not_on_approved_trade_list"


def test_ungoverned_family_passes_through():
    # A family with no approved-edge policy is governed by the other gates,
    # not this one — it is not blocked here (it also cannot be "promoted" here).
    r = _gate().evaluate(strategy_name="mystery_alpha", underlying="FINNIFTY",
                         opens_position=True, premium=_snap("FINNIFTY", 6.02))
    assert r.allowed and r.code == "family_not_governed"


def test_governed_short_vol_on_unlisted_underlying_is_refused():
    # short_vol IS governed (it has a policy), so short_vol on an underlying
    # with no policy line (BANKNIFTY here only if absent) is refused. Use a
    # spec that governs short_vol but lists only FINNIFTY.
    g = EdgeGate.from_spec("short_vol:FINNIFTY:4.0")
    r = g.evaluate(strategy_name="short_vol_condor", underlying="NIFTY",
                   opens_position=True, premium=_snap("NIFTY", 10.0))
    assert not r.allowed and r.code == "not_on_approved_trade_list"


# ---- fail-closed on data gaps ------------------------------------------------

def test_entry_fails_closed_when_regime_data_missing():
    r = _gate().evaluate(strategy_name="short_vol_condor", underlying="FINNIFTY",
                         opens_position=True, premium=None)
    assert not r.allowed and r.code == "regime_data_unavailable"


def test_entry_fails_closed_when_trailing_median_is_none():
    snap = PremiumSnapshot("FINNIFTY", date(2026, 9, 18), premium=5.0, trailing_median_60d=None)
    r = _gate().evaluate(strategy_name="short_vol_condor", underlying="FINNIFTY",
                         opens_position=True, premium=snap)
    assert not r.allowed and r.code == "regime_data_unavailable"


def test_stale_regime_data_refused():
    r = _gate().evaluate(strategy_name="short_vol_condor", underlying="FINNIFTY",
                         opens_position=True, premium=_snap("FINNIFTY", 6.02, stale=10))
    assert not r.allowed and r.code == "regime_data_stale"


def test_require_regime_data_false_allows_when_missing():
    r = _gate(require_regime_data=False).evaluate(
        strategy_name="short_vol_condor", underlying="FINNIFTY",
        opens_position=True, premium=None)
    assert r.allowed and r.code == "regime_data_optional"


# ---- config / disable --------------------------------------------------------

def test_disabled_gate_allows_everything():
    r = _gate(enabled=False).evaluate(
        strategy_name="anything", underlying="MIDCPNIFTY",
        opens_position=True, premium=None)
    assert r.allowed and r.code == "gate_disabled"


def test_from_spec_skips_malformed_entries():
    g = EdgeGate.from_spec("short_vol:FINNIFTY:4.0, garbage, short_vol:NIFTY")
    assert g.approved_pairs == [("short_vol", "FINNIFTY")]


def test_from_spec_optional_staleness_field():
    g = EdgeGate.from_spec("short_vol:FINNIFTY:4.0:7")
    policy = g._by_key[("short_vol", "FINNIFTY")]
    assert isinstance(policy, EdgePolicy) and policy.max_staleness_days == 7


# ---- integration: the gate is actually wired into the runtime final gate ----

import unittest
from datetime import date, datetime, timezone

from trading_platform.api.runtime import TradingRuntime
from trading_platform.domain.enums import Segment


class EdgeGateRuntimeWiringTests(unittest.TestCase):
    """Proves _evaluate_final_execution_gate consults the edge gate on entries.
    Default settings approve short_vol:FINNIFTY:4.0 only."""

    def _finnifty_option(self, runtime):
        opts = runtime.instrument_master.by_underlying("FINNIFTY", segment=Segment.OPTIONS)
        return opts[0] if opts else None

    def _nifty_option(self, runtime):
        return runtime.instrument_master.by_underlying("NIFTY", segment=Segment.OPTIONS)[0]

    def test_short_vol_nifty_entry_blocked_not_on_list(self):
        runtime = TradingRuntime()
        leg = self._nifty_option(runtime)
        intent = runtime._intent_from_payload({
            "symbol": leg.symbol, "side": "BUY", "quantity": 1, "price": 100.0,
            "strategy_name": "short_vol_condor", "metadata": {"hedged": True},
        })
        decision = runtime._evaluate_final_execution_gate(intent)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "edge_gate:not_on_approved_trade_list")

    def test_short_vol_finnifty_entry_fails_closed_without_premium_data(self):
        runtime = TradingRuntime()
        leg = self._finnifty_option(runtime)
        if leg is None:
            self.skipTest("no FINNIFTY options in instrument master")
        intent = runtime._intent_from_payload({
            "symbol": leg.symbol, "side": "BUY", "quantity": 1, "price": 100.0,
            "strategy_name": "short_vol_condor", "metadata": {"hedged": True},
        })
        decision = runtime._evaluate_final_execution_gate(intent)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "edge_gate:regime_data_unavailable")

    def test_short_vol_finnifty_entry_passes_edge_gate_when_premium_rich(self):
        runtime = TradingRuntime()
        leg = self._finnifty_option(runtime)
        if leg is None:
            self.skipTest("no FINNIFTY options in instrument master")
        today = datetime.now(timezone.utc).date()
        # Seed a rich, fresh trailing-60d premium (6.0 >= theta 4.0).
        runtime.db.save_index_premium(today, "FINNIFTY", 25000.0, 19.0, 13.0, 6.0, 6.0)
        intent = runtime._intent_from_payload({
            "symbol": leg.symbol, "side": "BUY", "quantity": 1, "price": 100.0,
            "strategy_name": "short_vol_condor", "metadata": {"hedged": True},
        })
        decision = runtime._evaluate_final_execution_gate(intent)
        # The edge gate must not be the thing blocking it (a later gate may).
        self.assertFalse(decision.reason.startswith("edge_gate:"),
                         f"edge gate should pass when premium is rich, got {decision.reason}")
