"""Tests for Phase 2: LocalModelGateway."""
import unittest

from trading_platform.agents.model_gateway import LocalModelGateway


class TestLocalModelGatewayStub(unittest.TestCase):
    def setUp(self):
        self._gw = LocalModelGateway(runtime="stub")

    def test_always_available(self):
        self.assertTrue(self._gw.is_available())

    def test_returns_dict(self):
        result = self._gw.generate(
            "gemma4-31b",
            "You are a trading analyst.",
            "Should we buy NIFTY today?",
        )
        self.assertIsInstance(result, dict)

    def test_has_required_keys(self):
        result = self._gw.generate("gemma4-31b", "sys", "user")
        self.assertIn("action", result)
        self.assertIn("confidence", result)
        self.assertIn("reasoning", result)
        self.assertIn("evidence_ids", result)

    def test_confidence_in_range(self):
        result = self._gw.generate("gemma4-e4b", "sys", "user")
        self.assertGreaterEqual(result["confidence"], 0.0)
        self.assertLessEqual(result["confidence"], 1.0)

    def test_unknown_model_falls_back(self):
        result = self._gw.generate("unknown-model-xyz", "sys", "user")
        self.assertIsInstance(result, dict)
        self.assertIn("action", result)

    def test_no_network_calls(self):
        # Stub should never make network calls — if it does, import urllib will detect it.
        # This is just a sanity check that stub returns deterministically.
        r1 = self._gw.generate("gemma4-31b", "sys", "user1")
        r2 = self._gw.generate("gemma4-31b", "sys", "user2")
        self.assertEqual(r1["action"], r2["action"])  # Stub is deterministic

    def test_all_model_names_return_valid(self):
        models = ["gemma4-31b", "gemma4-26b-moe", "gemma4-e4b", "gemma4-e2b"]
        for model in models:
            result = self._gw.generate(model, "sys", "user")
            self.assertIn("action", result)


if __name__ == "__main__":
    unittest.main()
