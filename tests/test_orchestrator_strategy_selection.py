"""Tests for MasterOrchestrator._node_strategy_selection() and
_node_execution_plan()'s consumption of its output.

Found 2026-09-08: the live orchestrator's execution_plan node never used
strategies/factory.py's 19 catalogued strategies at all — confirmed against
real trade history (every one of 172 trades ever recorded was
short_vol_condor/short_vol_exit/exit_manager:expiry, zero from any
catalogued strategy). These tests guard the fix: a strategy may only be
selected if it (a) cleared scripts/validate_dormant_strategies.py's DSR/PBO
gates (strategy_validator.load_accepted_strategies()) AND (b) produces a
real, risk-approved signal for the current cycle — and the change must be
purely additive: when neither holds, execution_plan's original generic
"orchestrator_<regime>" behavior must be exactly what it was before.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from trading_platform.domain.enums import Side
from trading_platform.domain.models import Instrument, MarketBar, Signal
from trading_platform.orchestrator.master_orchestrator import MasterOrchestrator
from trading_platform.orchestrator.state import OrchestratorState
from trading_platform.risk.engine import RiskDecision


def _bar(i: int) -> MarketBar:
    return MarketBar(
        timestamp=datetime(2026, 1, 1 + i, tzinfo=timezone.utc), symbol="RELIANCE",
        open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=1000,
    )


def _instrument() -> Instrument:
    from trading_platform.domain.enums import AssetClass, Exchange, InstrumentType, Segment
    return Instrument(
        symbol="RELIANCE", name="RELIANCE", exchange=Exchange.NSE, segment=Segment.CASH,
        asset_class=AssetClass.EQUITY, instrument_type=InstrumentType.EQUITY, token="X",
    )


def _approved_signal(strategy_name: str = "mean_reversion") -> Signal:
    return Signal(
        strategy_name=strategy_name, symbol="RELIANCE", side=Side.BUY, confidence=0.8,
        price=105.0, reason="test signal", created_at=datetime.now(timezone.utc),
    )


def _fake_decision_pipeline(bars, synthetic, candidate_fn):
    return SimpleNamespace(
        _fetch_bars=lambda underlying, start, days: bars,
        bars_were_synthetic=lambda underlying: synthetic,
        _candidate=candidate_fn,
    )


def _fake_runtime(decision_pipeline) -> SimpleNamespace:
    return SimpleNamespace(
        decision_pipeline=decision_pipeline,
        portfolio=SimpleNamespace(mark_to_market=lambda now, prices: SimpleNamespace()),
        live_armed=False,
        kill_switch_active=False,
    )


def _state() -> OrchestratorState:
    return OrchestratorState(
        trace_id="t1", underlying="RELIANCE", symbol_universe=["RELIANCE"],
        regime="MEAN_REVERTING", execution_mode="PAPER",
    )


def _approving_candidate(strategy_name, underlying, bars, now, snapshot, execution_mode,
                          live_armed, kill_switch_active, features):
    from trading_platform.decision.pipeline import DecisionCandidate
    return DecisionCandidate(
        underlying=underlying, strategy_name=strategy_name, instrument=_instrument(),
        signal=_approved_signal(strategy_name), quantity=10,
        risk_decision=RiskDecision(approved=True, reason="ok", risk_score=0.1), reason="ok",
    )


def _rejecting_candidate(strategy_name, underlying, bars, now, snapshot, execution_mode,
                          live_armed, kill_switch_active, features):
    from trading_platform.decision.pipeline import DecisionCandidate
    return DecisionCandidate(
        underlying=underlying, strategy_name=strategy_name, instrument=_instrument(),
        signal=None, quantity=0, risk_decision=None, reason="no_signal",
    )


class StrategySelectionNodeTests(unittest.TestCase):
    def test_no_accepted_strategies_is_a_pure_noop(self):
        rt = _fake_runtime(_fake_decision_pipeline([_bar(i) for i in range(25)], False, _approving_candidate))
        orch = MasterOrchestrator(rt)

        with mock.patch(
            "trading_platform.research.strategy_validator.load_accepted_strategies", return_value=set()
        ):
            result = orch._node_strategy_selection(_state())

        self.assertEqual(result.updates, {})

    def test_accepted_strategy_not_chosen_for_this_regime_is_a_noop(self):
        rt = _fake_runtime(_fake_decision_pipeline([_bar(i) for i in range(25)], False, _approving_candidate))
        orch = MasterOrchestrator(rt)

        # "breakout" is accepted but StrategySelectionAgent never returns it
        # for MEAN_REVERTING regime — must not be force-selected anyway.
        with mock.patch(
            "trading_platform.research.strategy_validator.load_accepted_strategies",
            return_value={"breakout"},
        ):
            result = orch._node_strategy_selection(_state())

        self.assertEqual(result.updates, {})

    def test_accepted_and_approved_strategy_is_selected(self):
        rt = _fake_runtime(_fake_decision_pipeline([_bar(i) for i in range(25)], False, _approving_candidate))
        orch = MasterOrchestrator(rt)

        with mock.patch(
            "trading_platform.research.strategy_validator.load_accepted_strategies",
            return_value={"mean_reversion"},
        ):
            result = orch._node_strategy_selection(_state())

        self.assertEqual(result.updates.get("selected_strategy"), "mean_reversion")
        order = result.updates.get("selected_strategy_order")
        self.assertIsNotNone(order)
        self.assertEqual(order["strategy_name"], "mean_reversion")
        self.assertEqual(order["symbol"], "RELIANCE")
        self.assertEqual(order["side"], "BUY")
        self.assertEqual(order["quantity"], 10)

    def test_accepted_strategy_with_no_real_signal_is_a_noop(self):
        rt = _fake_runtime(_fake_decision_pipeline([_bar(i) for i in range(25)], False, _rejecting_candidate))
        orch = MasterOrchestrator(rt)

        with mock.patch(
            "trading_platform.research.strategy_validator.load_accepted_strategies",
            return_value={"mean_reversion"},
        ):
            result = orch._node_strategy_selection(_state())

        self.assertEqual(result.updates, {})

    def test_synthetic_bars_block_selection_even_with_accepted_strategy(self):
        rt = _fake_runtime(_fake_decision_pipeline([_bar(i) for i in range(25)], True, _approving_candidate))
        orch = MasterOrchestrator(rt)

        with mock.patch(
            "trading_platform.research.strategy_validator.load_accepted_strategies",
            return_value={"mean_reversion"},
        ):
            result = orch._node_strategy_selection(_state())

        self.assertEqual(result.updates, {})

    def test_no_bars_available_is_a_noop_not_a_crash(self):
        rt = _fake_runtime(_fake_decision_pipeline([], False, _approving_candidate))
        orch = MasterOrchestrator(rt)

        with mock.patch(
            "trading_platform.research.strategy_validator.load_accepted_strategies",
            return_value={"mean_reversion"},
        ):
            result = orch._node_strategy_selection(_state())

        self.assertEqual(result.updates, {})


class ExecutionPlanUsesSelectedStrategyTests(unittest.TestCase):
    def test_selected_strategy_order_produces_a_real_candidate(self):
        rt = _fake_runtime(_fake_decision_pipeline([], False, _approving_candidate))
        orch = MasterOrchestrator(rt)
        state = OrchestratorState(
            trace_id="t1", underlying="RELIANCE", symbol_universe=["RELIANCE"],
            regime="MEAN_REVERTING", fusion_action="HOLD",  # generic path would say HOLD
            selected_strategy="mean_reversion",
            selected_strategy_order={
                "strategy_name": "mean_reversion", "symbol": "RELIANCE", "side": "BUY",
                "confidence": 0.8, "price": 105.0, "reason": "test", "quantity": 10,
            },
        )

        result = orch._node_execution_plan(state)

        candidates = result.updates["order_candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["strategy_name"], "mean_reversion")
        self.assertEqual(candidates[0]["side"], "BUY")
        self.assertEqual(candidates[0]["quantity"], 10)
        self.assertNotIn("orchestrator_", candidates[0]["strategy_name"])

    def test_no_selected_strategy_falls_back_to_generic_candidate_unchanged(self):
        rt = _fake_runtime(_fake_decision_pipeline([], False, _approving_candidate))
        orch = MasterOrchestrator(rt)
        state = OrchestratorState(
            trace_id="t1", underlying="RELIANCE", symbol_universe=["RELIANCE", "RELIANCE-FUT"],
            regime="TRENDING", fusion_action="BUY", fusion_confidence=0.7,
        )

        result = orch._node_execution_plan(state)

        candidates = result.updates["order_candidates"]
        self.assertEqual(len(candidates), 2)  # one per symbol_universe entry, as before
        self.assertEqual(candidates[0]["strategy_name"], "orchestrator_trending")


if __name__ == "__main__":
    unittest.main()
