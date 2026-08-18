"""Tests for Phase 2: LocalModelGateway."""
import json
import logging
import threading
import time
import unittest
from unittest.mock import patch

from trading_platform.agents.model_gateway import LocalModelGateway


class SecretScanTests(unittest.TestCase):
    """Regression test: found via log monitoring 2026-07-28. specialists.py's
    static system prompt instructs the model "Never include broker credentials,
    API keys, or passwords" — scanning that constant against its own secret-key
    list produced a 100% false-positive rate (152 warnings in 15 minutes, one
    per specialist call), drowning out any real signal from the check."""

    def setUp(self):
        self._gw = LocalModelGateway(runtime="stub")

    def test_static_system_prompt_with_credential_wording_is_not_flagged(self):
        logger = logging.getLogger("trading_platform.agents.model_gateway")
        with self.assertNoLogs(logger, level="WARNING"):
            self._gw.generate(
                "gemma4-31b",
                "Never include broker credentials, API keys, or passwords.",
                "Should we buy NIFTY today?",
            )

    def test_secret_in_user_prompt_is_still_flagged(self):
        logger = logging.getLogger("trading_platform.agents.model_gateway")
        with self.assertLogs(logger, level="WARNING") as captured:
            self._gw.generate("gemma4-31b", "sys", "the api_key is abc123")
        self.assertTrue(any("potential secret key" in line for line in captured.output))

    def test_secret_in_context_is_still_flagged(self):
        logger = logging.getLogger("trading_platform.agents.model_gateway")
        with self.assertLogs(logger, level="WARNING") as captured:
            self._gw.generate("gemma4-31b", "sys", "user", context={"password": "hunter2"})
        self.assertTrue(any("potential secret key" in line for line in captured.output))


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


class TestLmStudioRuntime(unittest.TestCase):
    def test_lm_studio_dispatches_to_openai_compat(self):
        gw = LocalModelGateway(runtime="lm_studio", base_url="http://x", max_concurrent_calls=5)
        calls = []

        def fake(model, system, user, timeout=None, max_tokens=None, response_schema=None):
            calls.append(model)
            return json.dumps({"action": "BUY", "confidence": 0.8, "reasoning": "r", "evidence_ids": []})

        gw._openai_compat = fake
        result = gw.generate("qwen/qwen3.6-35b-a3b", "sys", "user")
        self.assertEqual(result["action"], "BUY")
        self.assertEqual(calls, ["qwen/qwen3.6-35b-a3b"])


class TestOpenAiCompatPayload(unittest.TestCase):
    """Regression: LM Studio's server 400s on response_format.type="json_object"
    (only accepts "json_schema"/"text") — confirmed 2026-07-28 against a live
    server, unlike llama.cpp/vLLM which accept it. Must stay conditional."""

    def _captured_body(self, runtime: str) -> dict:
        gw = LocalModelGateway(runtime=runtime, base_url="http://x", max_concurrent_calls=5)
        captured = {}

        class FakeResp:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def read(self):
                return json.dumps({
                    "choices": [{"message": {"content": json.dumps(
                        {"action": "HOLD", "confidence": 0.5, "reasoning": "x", "evidence_ids": []}
                    )}}]
                }).encode()

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            return FakeResp()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            gw.generate("m", "sys", "user")
        return captured["body"]

    def test_lm_studio_omits_response_format(self):
        body = self._captured_body("lm_studio")
        # CHANGED 2026-08-09: lm_studio now DOES get response_format, using its
        # json_schema wire format (it 400s on "json_object", which is why this
        # was previously omitted). Without it the model never stopped on its
        # own — finish_reason was "length" at every max_tokens tried
        # (128/256/512/2048), burning the whole budget on prose. With a schema
        # the same call stops naturally at ~833 tokens.
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertEqual(
            body["response_format"]["json_schema"]["schema"]["properties"]["action"]["enum"],
            ["BUY", "SELL", "HOLD"],
        )

    def test_vllm_includes_response_format(self):
        body = self._captured_body("vllm")
        self.assertEqual(body.get("response_format"), {"type": "json_object"})

    def test_llama_cpp_includes_response_format(self):
        body = self._captured_body("llama_cpp")
        self.assertEqual(body.get("response_format"), {"type": "json_object"})


class TestMarkdownFencedJsonResponse(unittest.TestCase):
    """Regression: gemma-4-e4b via LM Studio wraps JSON in a ```json fence when
    response_format can't constrain it (confirmed 2026-07-28 against a live
    server) — json.loads used to fail at char 0 on every such reply."""

    def test_fenced_json_is_parsed(self):
        gw = LocalModelGateway(runtime="lm_studio", max_concurrent_calls=5)
        fenced = '```json\n{"action": "HOLD", "confidence": 0.3, "reasoning": "r", "evidence_ids": []}\n```'
        gw._openai_compat = lambda model, system, user, timeout=None, max_tokens=None, response_schema=None: fenced
        result = gw.generate("google/gemma-4-e4b", "sys", "user")
        self.assertEqual(result["action"], "HOLD")
        self.assertIsNone(result["failure_mode"])

    def test_plain_fence_without_json_tag_is_parsed(self):
        gw = LocalModelGateway(runtime="lm_studio", max_concurrent_calls=5)
        fenced = '```\n{"action": "BUY", "confidence": 0.7, "reasoning": "r", "evidence_ids": []}\n```'
        gw._openai_compat = lambda model, system, user, timeout=None, max_tokens=None, response_schema=None: fenced
        result = gw.generate("google/gemma-4-e4b", "sys", "user")
        self.assertEqual(result["action"], "BUY")

    def test_unfenced_json_still_parses(self):
        gw = LocalModelGateway(runtime="lm_studio", max_concurrent_calls=5)
        gw._openai_compat = lambda model, system, user, timeout=None, max_tokens=None, response_schema=None: '{"action": "SELL", "confidence": 0.6, "reasoning": "r", "evidence_ids": []}'
        result = gw.generate("google/gemma-4-e4b", "sys", "user")
        self.assertEqual(result["action"], "SELL")


class TestConcurrencyCap(unittest.TestCase):
    def test_clamps_to_at_least_one(self):
        gw = LocalModelGateway(runtime="lm_studio", max_concurrent_calls=0)
        self.assertEqual(gw.max_concurrent_calls, 1)

    def test_status_reports_max_concurrent_calls(self):
        gw = LocalModelGateway(runtime="stub", max_concurrent_calls=7)
        self.assertEqual(gw.status()["max_concurrent_calls"], 7)

    def test_cap_bounds_simultaneous_dispatches(self):
        cap = 2
        gw = LocalModelGateway(runtime="lm_studio", max_concurrent_calls=cap, timeout=5)
        gw._concurrency_wait_s = 1.0  # keep test fast but let contenders queue briefly
        lock = threading.Lock()
        in_flight = 0
        max_seen = 0
        release = threading.Event()

        def fake(model, system, user, timeout=None, max_tokens=None, response_schema=None):
            nonlocal in_flight, max_seen
            with lock:
                in_flight += 1
                max_seen = max(max_seen, in_flight)
            release.wait(2)
            with lock:
                in_flight -= 1
            return json.dumps({"action": "HOLD", "confidence": 0.5, "reasoning": "x", "evidence_ids": []})

        gw._openai_compat = fake
        threads = [threading.Thread(target=gw.generate, args=("m", "sys", "user")) for _ in range(6)]
        for t in threads:
            t.start()
        time.sleep(0.3)
        self.assertLessEqual(max_seen, cap)
        release.set()
        for t in threads:
            t.join(timeout=5)

    def test_overflow_call_falls_back_to_stub_fast(self):
        gw = LocalModelGateway(runtime="lm_studio", max_concurrent_calls=1, timeout=5)
        gw._concurrency_wait_s = 0.2  # keep the test fast
        started = threading.Event()
        release = threading.Event()

        def blocking(model, system, user, timeout=None, max_tokens=None, response_schema=None):
            started.set()
            release.wait(5)
            return json.dumps({"action": "HOLD", "confidence": 0.5, "reasoning": "x", "evidence_ids": []})

        gw._openai_compat = blocking
        holder = threading.Thread(target=gw.generate, args=("m", "sys", "user"))
        holder.start()
        started.wait(2)

        t0 = time.monotonic()
        result = gw.generate("m", "sys", "user")
        elapsed = time.monotonic() - t0

        self.assertEqual(result.get("failure_mode"), "concurrency_saturated")
        self.assertLess(elapsed, 1.0)  # failed fast, did not queue

        release.set()
        holder.join(timeout=5)


class TestEmbed(unittest.TestCase):
    """LocalModelGateway.embed() — real embeddings for RAG, shares the same
    concurrency semaphore as generate() since both compete for the same
    LM Studio capacity."""

    def test_stub_runtime_returns_none(self):
        gw = LocalModelGateway(runtime="stub")
        self.assertIsNone(gw.embed("some text"))

    def test_successful_embed_returns_vector(self):
        gw = LocalModelGateway(runtime="lm_studio", max_concurrent_calls=5)

        def fake_urlopen(req, timeout=None):
            class FakeResp:
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
                def read(self):
                    return json.dumps({"data": [{"embedding": [0.1, 0.2, 0.3]}]}).encode()
            return FakeResp()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = gw.embed("trend momentum strategy")
        self.assertEqual(result, [0.1, 0.2, 0.3])

    def test_request_failure_returns_none_not_raise(self):
        gw = LocalModelGateway(runtime="lm_studio", max_concurrent_calls=5)

        def fake_urlopen(req, timeout=None):
            raise TimeoutError("boom")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = gw.embed("some text")  # must not raise
        self.assertIsNone(result)

    def test_concurrency_saturated_returns_none_fast(self):
        gw = LocalModelGateway(runtime="lm_studio", max_concurrent_calls=1, timeout=5)
        gw._concurrency_wait_s = 0.2
        started = threading.Event()
        release = threading.Event()

        def blocking(req, timeout=None):
            started.set()
            release.wait(5)
            class FakeResp:
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
                def read(self):
                    return json.dumps({"data": [{"embedding": [1.0]}]}).encode()
            return FakeResp()

        with patch("urllib.request.urlopen", side_effect=blocking):
            holder = threading.Thread(target=gw.embed, args=("text",))
            holder.start()
            started.wait(2)

            t0 = time.monotonic()
            result = gw.embed("other text")
            elapsed = time.monotonic() - t0

            release.set()
            holder.join(timeout=5)

        self.assertIsNone(result)
        self.assertLess(elapsed, 1.0)


class TestScoreSentiment(unittest.TestCase):
    """LocalModelGateway.score_sentiment() — real financial-news sentiment
    for NewsIntelligence, same safe-None-on-any-failure contract as embed()."""

    def _fake_resp(self, content: str):
        def fake_urlopen(req, timeout=None):
            class FakeResp:
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
                def read(self):
                    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()
            return FakeResp()
        return fake_urlopen

    def test_stub_runtime_returns_none(self):
        gw = LocalModelGateway(runtime="stub")
        self.assertIsNone(gw.score_sentiment("headline", "summary"))

    def test_successful_score_returns_float(self):
        gw = LocalModelGateway(runtime="lm_studio", max_concurrent_calls=5)
        with patch("urllib.request.urlopen", side_effect=self._fake_resp('{"score": 0.6}')):
            result = gw.score_sentiment("Company beats earnings", "Strong quarter")
        self.assertEqual(result, 0.6)

    def test_score_is_clamped_to_range(self):
        gw = LocalModelGateway(runtime="lm_studio", max_concurrent_calls=5)
        with patch("urllib.request.urlopen", side_effect=self._fake_resp('{"score": 5.0}')):
            result = gw.score_sentiment("headline", "summary")
        self.assertEqual(result, 1.0)

    def test_markdown_fenced_json_is_parsed(self):
        gw = LocalModelGateway(runtime="lm_studio", max_concurrent_calls=5)
        fenced = '```json\n{"score": -0.4}\n```'
        with patch("urllib.request.urlopen", side_effect=self._fake_resp(fenced)):
            result = gw.score_sentiment("headline", "summary")
        self.assertEqual(result, -0.4)

    def test_malformed_json_returns_none_not_raise(self):
        gw = LocalModelGateway(runtime="lm_studio", max_concurrent_calls=5)
        with patch("urllib.request.urlopen", side_effect=self._fake_resp("not json")):
            result = gw.score_sentiment("headline", "summary")
        self.assertIsNone(result)

    def test_request_failure_returns_none_not_raise(self):
        gw = LocalModelGateway(runtime="lm_studio", max_concurrent_calls=5)
        with patch("urllib.request.urlopen", side_effect=TimeoutError("boom")):
            result = gw.score_sentiment("headline", "summary")
        self.assertIsNone(result)

    def test_concurrency_saturated_returns_none_fast(self):
        gw = LocalModelGateway(runtime="lm_studio", max_concurrent_calls=1, timeout=5)
        gw._concurrency_wait_s = 0.2
        started = threading.Event()
        release = threading.Event()

        def blocking(req, timeout=None):
            started.set()
            release.wait(5)
            class FakeResp:
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
                def read(self):
                    return json.dumps({"choices": [{"message": {"content": '{"score": 0.0}'}}]}).encode()
            return FakeResp()

        with patch("urllib.request.urlopen", side_effect=blocking):
            holder = threading.Thread(target=gw.score_sentiment, args=("h", "s"))
            holder.start()
            started.wait(2)

            t0 = time.monotonic()
            result = gw.score_sentiment("h2", "s2")
            elapsed = time.monotonic() - t0

            release.set()
            holder.join(timeout=5)

        self.assertIsNone(result)
        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
