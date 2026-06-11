"""Phase-1 hardening test suite.

Covers:

* OptionLeg / OptionStrategyPlan domain rules + validator
* Multi-leg builders (8 structures) + registry
* FeedStalenessTracker per-symbol staleness + gate
* InstrumentFreshnessTracker + LiveReadinessAggregator
* Runtime arm_live integration — refuses on synthetic boot

Acceptance: ``pytest tests/test_phase1.py -q`` reports ≥ 21 passing.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import pytest

from trading_platform.data.feed_staleness import (
    FeedStalenessTracker,
)
from trading_platform.data.instrument_master import build_default_universe
from trading_platform.derivatives.multi_leg_builder import (
    BuilderContext,
    available_structures,
    build_plan,
    get_builder,
    synthetic_oracle,
)
from trading_platform.derivatives.option_legs import (
    OptionLeg,
    OptionStrategyPlan,
    validate_plan,
)
from trading_platform.domain.enums import OptionType, Side
from trading_platform.governance.live_readiness import (
    InstrumentFreshnessTracker,
    LiveReadinessAggregator,
)


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture()
def master():
    return build_default_universe(date(2026, 5, 4))


@pytest.fixture()
def expiry(master):
    return master.expiries("NIFTY")[0]


@pytest.fixture()
def ctx(master, expiry):
    return BuilderContext(
        master=master,
        underlying="NIFTY",
        spot=23900.0,
        expiry=expiry,
        oracle=synthetic_oracle(annual_vol=0.18),
    )


# ──────────────────────────────────────────────────────────────────────
# OptionLeg / OptionStrategyPlan invariants  (5 tests)
# ──────────────────────────────────────────────────────────────────────


def _make_leg(strike: float, side: Side, opt: OptionType,
              premium: float = 100.0, expiry: date | None = None,
              quantity: int = 1, lot_size: int = 50, hedge: bool = False) -> OptionLeg:
    return OptionLeg(
        underlying="NIFTY",
        expiry=expiry or date(2026, 5, 28),
        strike=strike,
        option_type=opt,
        side=side,
        quantity=quantity,
        lot_size=lot_size,
        reference_premium=premium,
        hedge=hedge,
    )


def test_option_leg_rejects_non_positive_quantity():
    with pytest.raises(ValueError):
        OptionLeg(
            underlying="NIFTY",
            expiry=date(2026, 5, 28),
            strike=22500.0,
            option_type=OptionType.CE,
            side=Side.BUY,
            quantity=0,
        )


def test_option_strategy_plan_rejects_single_leg():
    """The whole point of Phase 1: a "straddle" can't ship as one leg."""
    leg = _make_leg(22500, Side.BUY, OptionType.CE)
    with pytest.raises(ValueError):
        OptionStrategyPlan(
            strategy_name="long_straddle",
            structure="long_straddle",
            underlying="NIFTY",
            legs=(leg,),
            spot_at_creation=22500.0,
        )


def test_option_strategy_plan_rejects_mismatched_underlying():
    leg_a = _make_leg(22500, Side.BUY, OptionType.CE)
    leg_b = OptionLeg(
        underlying="BANKNIFTY",
        expiry=date(2026, 5, 28),
        strike=22500.0,
        option_type=OptionType.PE,
        side=Side.BUY,
        quantity=1,
        lot_size=50,
        reference_premium=100,
    )
    with pytest.raises(ValueError):
        OptionStrategyPlan(
            strategy_name="long_straddle",
            structure="long_straddle",
            underlying="NIFTY",
            legs=(leg_a, leg_b),
            spot_at_creation=22500.0,
        )


def test_iron_condor_max_loss_equals_wing_minus_body_minus_credit():
    """Iron condor: max loss = wing width − body width − net credit, per lot.

    Body 22400/22600, wings 22200/22800. Wing width vs body = 200 each
    side. Worst case loses 200 × lot − net credit.
    """
    legs = (
        _make_leg(22800, Side.BUY, OptionType.CE, premium=20, hedge=True),
        _make_leg(22200, Side.BUY, OptionType.PE, premium=20, hedge=True),
        _make_leg(22600, Side.SELL, OptionType.CE, premium=80),
        _make_leg(22400, Side.SELL, OptionType.PE, premium=80),
    )
    plan = OptionStrategyPlan(
        strategy_name="iron_condor",
        structure="iron_condor",
        underlying="NIFTY",
        legs=legs,
        spot_at_creation=22500.0,
    )
    assert not plan.has_naked_short
    loss = plan.compute_max_loss()
    # Wing width 200 × lot 50 = 10000; net credit (80+80-20-20)*50 = 6000
    # Max loss = 10000 − 6000 = 4000 per side. Per side dominates → 4000.
    assert math.isfinite(loss)
    assert 3000 <= loss <= 5000, f"expected ~4000, got {loss}"


def test_short_straddle_unbounded_unless_naked_opt_in():
    legs = (
        _make_leg(22500, Side.SELL, OptionType.CE, premium=120),
        _make_leg(22500, Side.SELL, OptionType.PE, premium=120),
    )
    plan = OptionStrategyPlan(
        strategy_name="short_straddle",
        structure="short_straddle",
        underlying="NIFTY",
        legs=legs,
        spot_at_creation=22500.0,
        allow_naked_short=False,
    )
    assert plan.has_naked_short
    assert math.isinf(plan.compute_max_loss())
    result = validate_plan(plan)
    assert not result.ok
    assert "naked_short_blocked" in result.reasons
    assert "unbounded_loss" in result.reasons


# ──────────────────────────────────────────────────────────────────────
# Validator extras (2 tests)
# ──────────────────────────────────────────────────────────────────────


def test_validate_plan_collects_all_failures_not_just_first():
    """Validator must collect every reason — operators want the punch list."""
    legs = (
        _make_leg(22500, Side.SELL, OptionType.CE, premium=120),
        _make_leg(22500, Side.SELL, OptionType.PE, premium=120),
    )
    plan = OptionStrategyPlan(
        strategy_name="short_straddle",
        structure="short_straddle",
        underlying="NIFTY",
        legs=legs,
        spot_at_creation=22500.0,
        allow_naked_short=False,
    )
    result = validate_plan(plan, max_allowed_loss=5000)
    # Both naked_short_blocked AND unbounded_loss should be present.
    assert {"naked_short_blocked", "unbounded_loss"}.issubset(set(result.reasons))


def test_validate_plan_max_loss_cap_blocks_oversized_position():
    legs = (
        _make_leg(22500, Side.BUY, OptionType.CE, premium=200, quantity=10),
        _make_leg(22500, Side.BUY, OptionType.PE, premium=200, quantity=10),
    )
    plan = OptionStrategyPlan(
        strategy_name="long_straddle",
        structure="long_straddle",
        underlying="NIFTY",
        legs=legs,
        spot_at_creation=22500.0,
    )
    # 10 lots × 50 lot size × 400 net debit = 200_000. Cap at 50_000.
    result = validate_plan(plan, max_allowed_loss=50_000)
    assert not result.ok
    assert any(r.startswith("max_loss_exceeds_cap") for r in result.reasons)


# ──────────────────────────────────────────────────────────────────────
# Multi-leg builders  (9 tests — 8 structures + registry)
# ──────────────────────────────────────────────────────────────────────


def test_long_straddle_builder_emits_two_legs(ctx):
    plan = build_plan("long_straddle", ctx)
    assert len(plan.legs) == 2
    assert plan.legs[0].option_type != plan.legs[1].option_type
    assert all(leg.is_long for leg in plan.legs)
    assert not plan.has_naked_short
    assert math.isfinite(plan.compute_max_loss())


def test_long_strangle_uses_otm_strikes(ctx):
    plan = build_plan("long_strangle", ctx)
    assert len(plan.legs) == 2
    ce = next(leg for leg in plan.legs if leg.option_type == OptionType.CE)
    pe = next(leg for leg in plan.legs if leg.option_type == OptionType.PE)
    # Strangle: CE strike > spot, PE strike < spot.
    assert ce.strike > ctx.spot
    assert pe.strike < ctx.spot


def test_short_straddle_builder_marks_naked(ctx):
    """Short straddle without explicit opt-in must validate as
    naked_short_blocked + unbounded_loss.
    """
    plan = build_plan("short_straddle", ctx)
    assert len(plan.legs) == 2
    assert all(leg.is_short for leg in plan.legs)
    assert plan.has_naked_short
    result = validate_plan(plan)
    assert "naked_short_blocked" in result.reasons


def test_iron_condor_has_four_legs_hedges_first(ctx):
    plan = build_plan("iron_condor", ctx)
    assert len(plan.legs) == 4
    # Builder ordering: hedges first so wings fill before body.
    assert plan.legs[0].hedge is True
    assert plan.legs[1].hedge is True
    assert plan.legs[2].hedge is False
    assert plan.legs[3].hedge is False
    assert not plan.has_naked_short
    assert math.isfinite(plan.compute_max_loss())


def test_bull_call_spread_capped_loss(ctx):
    plan = build_plan("bull_call_spread", ctx)
    assert len(plan.legs) == 2
    # Long ATM at lower strike, short above.
    long_leg = next(leg for leg in plan.legs if leg.is_long)
    short_leg = next(leg for leg in plan.legs if leg.is_short)
    assert long_leg.strike < short_leg.strike
    assert math.isfinite(plan.compute_max_loss())


def test_bear_put_spread_capped_loss(ctx):
    plan = build_plan("bear_put_spread", ctx)
    long_leg = next(leg for leg in plan.legs if leg.is_long)
    short_leg = next(leg for leg in plan.legs if leg.is_short)
    assert long_leg.strike > short_leg.strike
    assert math.isfinite(plan.compute_max_loss())


def test_calendar_spread_uses_distinct_expiries(master, ctx):
    expiries = master.expiries("NIFTY")
    assert len(expiries) >= 2, "fixture needs ≥ 2 expiries"
    plan = build_plan("calendar_spread", ctx)
    assert plan.is_calendar
    # Validator should accept multi-expiry on a calendar.
    result = validate_plan(plan)
    assert "multi_expiry_in_non_calendar_structure" not in result.reasons


def test_delta_neutral_hedge_is_long_straddle_with_metadata(ctx):
    plan = build_plan("delta_neutral_hedge", ctx)
    assert len(plan.legs) == 2
    assert all(leg.is_long for leg in plan.legs)
    assert plan.metadata.get("requires_futures_hedge") is True


def test_registry_lists_all_eight_structures():
    structures = available_structures()
    expected = {
        "long_straddle", "long_strangle", "short_straddle", "iron_condor",
        "bull_call_spread", "bear_put_spread", "calendar_spread",
        "delta_neutral_hedge",
    }
    assert expected.issubset(set(structures))
    for s in expected:
        assert get_builder(s) is not None


# ──────────────────────────────────────────────────────────────────────
# FeedStalenessTracker  (4 tests)
# ──────────────────────────────────────────────────────────────────────


def _frozen_clock(start: datetime):
    state = {"now": start}

    def clock():
        return state["now"]

    def advance(seconds: float):
        state["now"] = state["now"] + timedelta(seconds=seconds)

    return clock, advance


def test_feed_staleness_records_per_symbol():
    clock, advance = _frozen_clock(datetime(2026, 5, 4, 9, 30, tzinfo=timezone.utc))
    tracker = FeedStalenessTracker(hard_seconds=15, warn_seconds=60, clock=clock)
    tracker.record("NIFTY")
    advance(5)
    status = tracker.status("NIFTY")
    assert status.is_stale is False
    assert status.is_warn is False
    assert status.age_seconds is not None and 4.5 < status.age_seconds < 5.5


def test_feed_staleness_flags_after_hard_threshold():
    clock, advance = _frozen_clock(datetime(2026, 5, 4, 9, 30, tzinfo=timezone.utc))
    tracker = FeedStalenessTracker(hard_seconds=15, warn_seconds=60, clock=clock)
    tracker.record("NIFTY")
    advance(20)  # past hard threshold
    assert tracker.is_stale("NIFTY")
    status = tracker.status("NIFTY")
    assert status.is_stale and not status.is_warn


def test_feed_staleness_gate_fails_when_feed_not_running():
    tracker = FeedStalenessTracker()
    gate = tracker.gate(["NIFTY"], feed_running=False)
    assert not gate.passed
    assert gate.reason == "feed_not_running"


def test_feed_staleness_gate_fails_with_no_subscribed_symbols():
    tracker = FeedStalenessTracker()
    gate = tracker.gate([], feed_running=True)
    assert not gate.passed
    assert gate.reason == "no_symbols_subscribed"


def test_feed_staleness_gate_passes_when_all_symbols_recent():
    clock, advance = _frozen_clock(datetime(2026, 5, 4, 9, 30, tzinfo=timezone.utc))
    tracker = FeedStalenessTracker(hard_seconds=15, warn_seconds=60, clock=clock)
    tracker.record("NIFTY")
    tracker.record("BANKNIFTY")
    advance(2)
    gate = tracker.gate(["NIFTY", "BANKNIFTY"], feed_running=True)
    assert gate.passed
    assert gate.reason == "ok"


# ──────────────────────────────────────────────────────────────────────
# InstrumentFreshnessTracker + LiveReadinessAggregator  (5 tests)
# ──────────────────────────────────────────────────────────────────────


def test_instrument_freshness_blocks_when_synthetic():
    clock, _ = _frozen_clock(datetime(2026, 5, 4, 9, 0, tzinfo=timezone.utc))
    tracker = InstrumentFreshnessTracker(max_age_seconds=43_200, clock=clock)
    tracker.mark_synthetic(parsed_count=12)
    gate = tracker.gate()
    assert not gate.passed
    assert gate.reason == "instrument_master_is_synthetic"


def test_instrument_freshness_passes_after_real_refresh():
    clock, advance = _frozen_clock(datetime(2026, 5, 4, 9, 0, tzinfo=timezone.utc))
    tracker = InstrumentFreshnessTracker(max_age_seconds=43_200, clock=clock)
    tracker.record_refresh(source="https://api.angelone.in/...", parsed_count=120_000)
    advance(60)
    gate = tracker.gate()
    assert gate.passed


def test_instrument_freshness_blocks_when_stale():
    clock, advance = _frozen_clock(datetime(2026, 5, 4, 9, 0, tzinfo=timezone.utc))
    tracker = InstrumentFreshnessTracker(max_age_seconds=3600, clock=clock)
    tracker.record_refresh(source="https://api.angelone.in/", parsed_count=120_000)
    advance(7200)  # 2h > 1h max-age
    gate = tracker.gate()
    assert not gate.passed
    assert gate.reason.startswith("instrument_master_stale")


def test_live_readiness_blocks_on_synthetic_boot():
    """Fresh-boot scenario: synthetic universe + no ticks → arm_live blocked."""
    clock, _ = _frozen_clock(datetime(2026, 5, 4, 9, 0, tzinfo=timezone.utc))
    freshness = InstrumentFreshnessTracker(clock=clock)
    freshness.mark_synthetic(parsed_count=12)
    staleness = FeedStalenessTracker(clock=clock)
    agg = LiveReadinessAggregator(
        instrument_freshness=freshness, feed_staleness=staleness, clock=clock,
    )
    readiness = agg.evaluate(
        execution_mode="LIVE",
        live_trading_enabled=True,
        real_money_confirmation="I_ACCEPT_REAL_MONEY_LIVE_ORDERS",
        broker_configured=True,
        broker_name="angel_one",
        kill_switch_active=False,
        feed_running=False,
        subscribed_symbols=[],
    )
    assert not readiness.armed_eligible
    reasons = set(readiness.blocking_reasons)
    assert "instrument_master_is_synthetic" in reasons
    assert "feed_not_running" in reasons


def test_live_readiness_passes_when_everything_green():
    clock, advance = _frozen_clock(datetime(2026, 5, 4, 9, 0, tzinfo=timezone.utc))
    freshness = InstrumentFreshnessTracker(clock=clock)
    freshness.record_refresh(source="https://api.angelone.in/", parsed_count=120_000)
    staleness = FeedStalenessTracker(clock=clock)
    staleness.record("NIFTY")
    advance(2)
    agg = LiveReadinessAggregator(
        instrument_freshness=freshness, feed_staleness=staleness, clock=clock,
    )
    readiness = agg.evaluate(
        execution_mode="LIVE",
        live_trading_enabled=True,
        real_money_confirmation="I_ACCEPT_REAL_MONEY_LIVE_ORDERS",
        broker_configured=True,
        broker_name="angel_one",
        kill_switch_active=False,
        feed_running=True,
        subscribed_symbols=["NIFTY"],
    )
    assert readiness.armed_eligible
    assert readiness.blocking_reasons == ()
    assert all(g.passed for g in readiness.gates)


def test_live_readiness_evaluates_all_seven_gates_even_when_one_fails():
    """Design rule: never short-circuit. The full gate list is the
    operator's punch list."""
    clock, _ = _frozen_clock(datetime(2026, 5, 4, 9, 0, tzinfo=timezone.utc))
    freshness = InstrumentFreshnessTracker(clock=clock)
    freshness.mark_synthetic(parsed_count=0)
    staleness = FeedStalenessTracker(clock=clock)
    agg = LiveReadinessAggregator(
        instrument_freshness=freshness, feed_staleness=staleness, clock=clock,
    )
    readiness = agg.evaluate(
        execution_mode="BACKTEST",  # also fails
        live_trading_enabled=False,
        real_money_confirmation="",
        broker_configured=False,
        broker_name="simulated",
        kill_switch_active=True,
        feed_running=False,
        subscribed_symbols=[],
    )
    assert len(readiness.gates) == 7
    # Every gate evaluated — none was skipped because of an earlier failure.
    failed = [g for g in readiness.gates if not g.passed]
    assert len(failed) == 7


# ──────────────────────────────────────────────────────────────────────
# Runtime integration  (1 test)
# ──────────────────────────────────────────────────────────────────────


def test_runtime_arm_live_refuses_on_synthetic_boot():
    """End-to-end: a fresh runtime with the boot-loaded synthetic
    universe MUST refuse arm_live(True). The synthetic-universe gate
    fires regardless of how credentials/mode are set.
    """
    from trading_platform.api.runtime import TradingRuntime

    rt = TradingRuntime()
    payload = rt.live_readiness_payload()
    assert not payload["armed_eligible"]
    blocking = set(payload["blocking_reasons"])
    assert "instrument_master_is_synthetic" in blocking
    with pytest.raises(ValueError, match="live_readiness_blocked"):
        rt.arm_live(True)
