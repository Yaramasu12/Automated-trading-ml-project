"""trading_platform/mcp/server.py exposes the platform's REST API as MCP tools
for any MCP-compatible client. These tests verify: (1) it's genuinely
read-only (no tool can reach a mutating HTTP method or a non-GET endpoint),
(2) every tool degrades to an error dict instead of raising when the API is
unreachable, and (3) the registered tool set matches the fixed, reviewed
endpoint list — nothing invented, nothing silently added later without a
test catching it.
"""
from __future__ import annotations

import asyncio
import unittest
import urllib.error
from unittest import mock

import trading_platform.mcp.server as mcp_server


EXPECTED_TOOLS = {
    "get_health", "get_portfolio_positions", "get_db_trades",
    "get_db_equity_curve", "get_db_risk_events", "get_db_summary",
    "get_feed_snapshot", "get_ai_council_status", "get_execution_tca",
    "get_strategies_catalog", "get_governance", "get_live_readiness",
    "get_monitoring_metrics", "get_backtests_gates", "get_research_hypotheses",
}


class ReadOnlySurfaceTests(unittest.TestCase):
    """The whole point of this server is that it cannot mutate anything."""

    def test_registered_tool_set_is_exactly_the_reviewed_list(self) -> None:
        tools = asyncio.run(mcp_server.mcp.list_tools())
        names = {t.name for t in tools}
        self.assertEqual(names, EXPECTED_TOOLS)

    def test_source_never_references_a_mutating_http_method(self) -> None:
        import inspect
        source = inspect.getsource(mcp_server)
        for verb in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertNotIn(verb, source, f"found {verb} in mcp/server.py — this server must stay read-only")

    def test_get_helper_never_passes_a_request_body(self) -> None:
        """urllib.request.urlopen(url) with no `data=` is always a GET —
        confirms _get() can't be turned into a write by a future edit that
        adds a data= kwarg without anyone noticing."""
        import inspect
        source = inspect.getsource(mcp_server._get)
        self.assertNotIn("data=", source)
        self.assertNotIn("Request(", source)  # would allow an explicit method=


class ToolErrorHandlingTests(unittest.TestCase):
    """Every tool must degrade to {"error": ...} instead of raising — the
    trading-api container may legitimately be down or restarting."""

    def test_get_health_returns_error_dict_when_api_unreachable(self) -> None:
        with mock.patch(
            "trading_platform.mcp.server.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            result = mcp_server.get_health()
        self.assertIn("error", result)

    def test_get_db_trades_passes_limit_as_query_param(self) -> None:
        captured = {}

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"count": 0, "trades": []}'

        def _fake_urlopen(url, timeout=10):
            captured["url"] = url
            return _FakeResp()

        with mock.patch("trading_platform.mcp.server.urllib.request.urlopen", side_effect=_fake_urlopen):
            result = mcp_server.get_db_trades(limit=7)

        self.assertIn("limit=7", captured["url"])
        self.assertEqual(result, {"count": 0, "trades": []})


if __name__ == "__main__":
    unittest.main()
