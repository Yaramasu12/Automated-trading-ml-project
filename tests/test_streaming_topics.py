"""Tests for trading_platform.streaming.topics."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from trading_platform.streaming.topics import (
    BusTopic,
    TypedMessage,
    TypedTopicBus,
    publish_agent_vote,
    publish_bar,
    publish_features,
    publish_model_prediction,
    publish_order_event,
    publish_quantum_result,
    publish_risk_veto,
    publish_tick,
)


# ── BusTopic enum ─────────────────────────────────────────────────────────────

class TestBusTopic:
    def test_canonical_values(self):
        assert BusTopic.TICK_RAW.value == "tick.raw"
        assert BusTopic.BAR_CLOSED.value == "bar.closed"
        assert BusTopic.OPTION_CHAIN.value == "option.chain"
        assert BusTopic.FEATURES.value == "features"
        assert BusTopic.AGENT_VOTE.value == "agent.vote"
        assert BusTopic.MODEL_PREDICTION.value == "model.prediction"
        assert BusTopic.QUANTUM_RESULT.value == "quantum.result"
        assert BusTopic.RISK_VETO.value == "risk.veto"
        assert BusTopic.ORDER_EVENT.value == "order.event"

    def test_internal_topics(self):
        assert BusTopic.RUNTIME_CONTROL.value == "runtime.control"
        assert BusTopic.LABEL_OUTCOME.value == "label.outcome"

    def test_all_topics_present(self):
        values = {t.value for t in BusTopic}
        required = {
            "tick.raw", "bar.closed", "option.chain", "features",
            "agent.vote", "model.prediction", "quantum.result",
            "risk.veto", "order.event",
        }
        assert required.issubset(values)

    def test_string_comparison(self):
        # BusTopic subclasses str, so it can be used as a string key
        assert BusTopic.TICK_RAW == "tick.raw"


# ── TypedMessage ──────────────────────────────────────────────────────────────

class TestTypedMessage:
    def test_defaults(self):
        msg = TypedMessage(topic=BusTopic.TICK_RAW, payload={"symbol": "NIFTY"})
        assert msg.source == ""
        assert msg.schema_version == "1.0"
        assert isinstance(msg.ts, datetime)

    def test_to_dict_keys(self):
        msg = TypedMessage(
            topic=BusTopic.TICK_RAW,
            payload={"symbol": "NIFTY", "last_price": 22500.0},
            source="live_feed",
        )
        d = msg.to_dict()
        assert d["topic"] == "tick.raw"
        assert d["source"] == "live_feed"
        assert d["schema_version"] == "1.0"
        assert "ts" in d
        assert d["payload"]["symbol"] == "NIFTY"
        assert d["payload"]["last_price"] == 22500.0

    def test_ts_is_utc(self):
        msg = TypedMessage(topic=BusTopic.FEATURES, payload={})
        assert msg.ts.tzinfo is not None


# ── TypedTopicBus ─────────────────────────────────────────────────────────────

def _make_mock_bus():
    """Return a mock InMemoryEventBus with .publish() and .recent()."""
    mock = MagicMock()
    mock.recent.return_value = []
    return mock


class TestTypedTopicBusPublish:
    def test_publish_returns_typed_message(self):
        bus = TypedTopicBus(_make_mock_bus())
        msg = bus.publish(BusTopic.TICK_RAW, {"symbol": "NIFTY", "last_price": 22500.0},
                          source="live_feed")
        assert isinstance(msg, TypedMessage)
        assert msg.topic == BusTopic.TICK_RAW

    def test_publish_forwards_to_underlying_bus(self):
        mock_bus = _make_mock_bus()
        bus = TypedTopicBus(mock_bus)
        bus.publish(BusTopic.FEATURES, {"symbol": "NIFTY", "momentum": 0.5}, source="feature_store")
        mock_bus.publish.assert_called_once()
        call_args = mock_bus.publish.call_args
        assert call_args[0][0] == "features"   # topic value passed as positional arg

    def test_message_count_increments(self):
        bus = TypedTopicBus(_make_mock_bus())
        bus.publish(BusTopic.TICK_RAW, {})
        bus.publish(BusTopic.TICK_RAW, {})
        counts = bus.message_counts()
        assert counts["tick.raw"] == 2

    def test_message_count_separate_per_topic(self):
        bus = TypedTopicBus(_make_mock_bus())
        bus.publish(BusTopic.TICK_RAW, {})
        bus.publish(BusTopic.FEATURES, {})
        bus.publish(BusTopic.FEATURES, {})
        counts = bus.message_counts()
        assert counts["tick.raw"] == 1
        assert counts["features"] == 2

    def test_underlying_bus_error_doesnt_raise(self):
        mock_bus = _make_mock_bus()
        mock_bus.publish.side_effect = RuntimeError("bus error")
        bus = TypedTopicBus(mock_bus)
        # Should not propagate
        msg = bus.publish(BusTopic.TICK_RAW, {"symbol": "NIFTY"})
        assert msg is not None


class TestTypedTopicBusSubscribe:
    def test_subscribe_receives_message(self):
        bus = TypedTopicBus(_make_mock_bus())
        received = []
        bus.subscribe(BusTopic.TICK_RAW, lambda msg: received.append(msg))
        bus.publish(BusTopic.TICK_RAW, {"symbol": "NIFTY", "last_price": 22500.0})
        assert len(received) == 1
        assert received[0].payload["symbol"] == "NIFTY"

    def test_subscribe_only_fires_for_matching_topic(self):
        bus = TypedTopicBus(_make_mock_bus())
        tick_received = []
        feat_received = []
        bus.subscribe(BusTopic.TICK_RAW, lambda msg: tick_received.append(msg))
        bus.subscribe(BusTopic.FEATURES, lambda msg: feat_received.append(msg))
        bus.publish(BusTopic.TICK_RAW, {})
        assert len(tick_received) == 1
        assert len(feat_received) == 0

    def test_multiple_subscribers_all_called(self):
        bus = TypedTopicBus(_make_mock_bus())
        counts = [0, 0]
        bus.subscribe(BusTopic.AGENT_VOTE, lambda _: counts.__setitem__(0, counts[0] + 1))
        bus.subscribe(BusTopic.AGENT_VOTE, lambda _: counts.__setitem__(1, counts[1] + 1))
        bus.publish(BusTopic.AGENT_VOTE, {"action": "PROCEED"})
        assert counts == [1, 1]

    def test_callback_exception_doesnt_propagate(self):
        bus = TypedTopicBus(_make_mock_bus())

        def bad_cb(msg):
            raise RuntimeError("callback error")

        bus.subscribe(BusTopic.RISK_VETO, bad_cb)
        # Should not raise
        bus.publish(BusTopic.RISK_VETO, {"symbol": "NIFTY", "reason": "drawdown"})

    def test_unsubscribe(self):
        bus = TypedTopicBus(_make_mock_bus())
        received = []
        cb = lambda msg: received.append(msg)
        bus.subscribe(BusTopic.ORDER_EVENT, cb)
        bus.publish(BusTopic.ORDER_EVENT, {"event": "filled"})
        bus.unsubscribe(BusTopic.ORDER_EVENT, cb)
        bus.publish(BusTopic.ORDER_EVENT, {"event": "filled"})
        assert len(received) == 1

    def test_unsubscribe_missing_no_error(self):
        bus = TypedTopicBus(_make_mock_bus())
        cb = lambda msg: None
        bus.unsubscribe(BusTopic.TICK_RAW, cb)  # never subscribed


class TestTypedTopicBusInspection:
    def test_message_counts_has_all_topics(self):
        bus = TypedTopicBus(_make_mock_bus())
        counts = bus.message_counts()
        for topic in BusTopic:
            assert topic.value in counts

    def test_topic_status_structure(self):
        bus = TypedTopicBus(_make_mock_bus())
        status = bus.topic_status()
        assert "topics" in status
        assert "message_counts" in status
        assert "total_messages" in status
        assert "backend" in status

    def test_topic_status_total(self):
        bus = TypedTopicBus(_make_mock_bus())
        bus.publish(BusTopic.TICK_RAW, {})
        bus.publish(BusTopic.FEATURES, {})
        bus.publish(BusTopic.FEATURES, {})
        status = bus.topic_status()
        assert status["total_messages"] == 3

    def test_recent_delegates_to_underlying(self):
        mock_bus = _make_mock_bus()
        mock_bus.recent.return_value = [{"payload": "x"}]
        bus = TypedTopicBus(mock_bus)
        result = bus.recent(BusTopic.TICK_RAW, limit=10)
        mock_bus.recent.assert_called_once()
        assert result == [{"payload": "x"}]


class TestTypedTopicBusThreadSafety:
    def test_concurrent_publish_and_subscribe(self):
        bus = TypedTopicBus(_make_mock_bus())
        received = []
        lock = threading.Lock()
        errors = []

        def publisher(i: int) -> None:
            try:
                bus.publish(BusTopic.TICK_RAW, {"i": i})
            except Exception as exc:
                errors.append(exc)

        def subscriber() -> None:
            try:
                bus.subscribe(BusTopic.TICK_RAW, lambda msg: None)
            except Exception as exc:
                errors.append(exc)

        threads = []
        for i in range(20):
            threads.append(threading.Thread(target=publisher, args=(i,)))
            threads.append(threading.Thread(target=subscriber))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors


# ── Convenience publish helpers ───────────────────────────────────────────────

class TestPublishHelpers:
    def _bus(self):
        return TypedTopicBus(_make_mock_bus())

    def test_publish_tick(self):
        bus = self._bus()
        received = []
        bus.subscribe(BusTopic.TICK_RAW, lambda msg: received.append(msg))
        publish_tick(bus, "NIFTY", 22500.0, volume=1000)
        assert len(received) == 1
        assert received[0].payload["symbol"] == "NIFTY"
        assert received[0].payload["last_price"] == 22500.0
        assert received[0].source == "live_feed"

    def test_publish_bar(self):
        bus = self._bus()
        received = []
        bus.subscribe(BusTopic.BAR_CLOSED, lambda msg: received.append(msg))
        publish_bar(bus, "NIFTY", "15min", {"open": 22400, "high": 22600, "low": 22300, "close": 22500})
        assert len(received) == 1
        assert received[0].payload["symbol"] == "NIFTY"
        assert received[0].payload["timeframe"] == "15min"

    def test_publish_features(self):
        bus = self._bus()
        received = []
        bus.subscribe(BusTopic.FEATURES, lambda msg: received.append(msg))
        publish_features(bus, "NIFTY", {"momentum_5": 0.02, "regime": "TRENDING"})
        assert len(received) == 1
        assert received[0].source == "feature_store"

    def test_publish_agent_vote(self):
        bus = self._bus()
        received = []
        bus.subscribe(BusTopic.AGENT_VOTE, lambda msg: received.append(msg))
        publish_agent_vote(bus, {"action": "PROCEED", "confidence": 0.75})
        assert len(received) == 1
        assert received[0].source == "agent_council"

    def test_publish_model_prediction(self):
        bus = self._bus()
        received = []
        bus.subscribe(BusTopic.MODEL_PREDICTION, lambda msg: received.append(msg))
        publish_model_prediction(bus, {"return_q50": 0.02})
        assert len(received) == 1
        assert received[0].source == "neural_service"

    def test_publish_quantum_result(self):
        bus = self._bus()
        received = []
        bus.subscribe(BusTopic.QUANTUM_RESULT, lambda msg: received.append(msg))
        publish_quantum_result(bus, {"optimal_weights": [0.5, 0.3, 0.2]})
        assert len(received) == 1
        assert received[0].source == "quantum_service"

    def test_publish_risk_veto(self):
        bus = self._bus()
        received = []
        bus.subscribe(BusTopic.RISK_VETO, lambda msg: received.append(msg))
        publish_risk_veto(bus, "NIFTY", "drawdown_limit", "risk_engine", intent_id="i1")
        assert len(received) == 1
        msg = received[0]
        assert msg.payload["symbol"] == "NIFTY"
        assert msg.payload["reason"] == "drawdown_limit"
        assert msg.payload["intent_id"] == "i1"

    def test_publish_order_event(self):
        bus = self._bus()
        received = []
        bus.subscribe(BusTopic.ORDER_EVENT, lambda msg: received.append(msg))
        publish_order_event(bus, "filled", "NIFTY", "BUY", 22500.0, 10,
                            strategy="trend_momentum", intent_id="i2")
        assert len(received) == 1
        msg = received[0]
        assert msg.payload["event"] == "filled"
        assert msg.payload["symbol"] == "NIFTY"
        assert msg.payload["price"] == 22500.0
        assert msg.payload["quantity"] == 10
        assert msg.source == "execution_scheduler"
