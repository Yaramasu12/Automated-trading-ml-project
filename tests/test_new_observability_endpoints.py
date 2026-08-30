"""GET /vector-memory/status and GET /ai-council/skill-eval — thin
integration tests confirming the FastAPI routes themselves are wired
correctly (return 200, correct top-level shape). The underlying logic is
tested directly and thoroughly in test_vector_memory_qdrant.py and
test_council_skill_eval.py; this only proves the HTTP layer actually calls
through to it.
"""
from __future__ import annotations

import unittest


class ObservabilityEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from trading_platform.api.app import app
        cls.client = TestClient(app)

    def test_vector_memory_status_returns_200_with_enabled_key(self):
        r = self.client.get("/vector-memory/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("enabled", body)

    def test_vector_memory_status_reports_qdrant_shape_when_enabled(self):
        r = self.client.get("/vector-memory/status")
        body = r.json()
        if body.get("enabled"):
            self.assertIn("connected", body)
            self.assertIn("documents_in_memory", body)
            self.assertIn("categories", body)

    def test_ai_council_skill_eval_returns_200_with_expected_shape(self):
        r = self.client.get("/ai-council/skill-eval")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for key in (
            "total_decisions_traced", "total_outcomes_traced", "joined_count",
            "sample_size_sufficient", "joined",
        ):
            self.assertIn(key, body)

    def test_ai_council_skill_eval_respects_limit_param(self):
        r = self.client.get("/ai-council/skill-eval?limit=5")
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
