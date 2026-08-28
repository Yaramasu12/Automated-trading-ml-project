"""Delegate a well-scoped implementation task to the local LLM, acting as a
professional developer implementing a real feature for this codebase: an MCP
(Model Context Protocol) server exposing this trading platform's read-only
observability data.

Uses the same reasoning_content-fallback LocalLLMClient already fixed in
llm_researcher.py this session (all locally-loaded models are "thinking"
hybrids that need it). The model's output is written to a file, then this
script's caller (not the model) is responsible for reviewing, testing, and
fixing it before it's considered done — same propose-then-verify discipline
as research/llm_researcher.py, applied to code instead of trading hypotheses.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_platform.research.llm_researcher import LocalLLMClient

SYSTEM_PROMPT = """You are a professional Python backend developer with deep experience
in the Model Context Protocol (MCP) and FastAPI-adjacent codebases. You write clean,
correct, minimal code with no speculative features. You NEVER invent an API endpoint
path that wasn't given to you — if you're not sure a route exists, you don't use it.
You always output a complete, single, runnable Python file inside one ```python code
fence and nothing else outside that fence (no chatter before or after)."""

TASK_PROMPT = """Implement `trading_platform/mcp/server.py`: an MCP server for this
automated trading platform (Automated-trading-ml-project), using the official `mcp`
Python SDK (already installed, package `mcp`, version 2.1.1 — use
`from mcp.server.fastmcp import FastMCP`).

## What this server is for

Expose this platform's REST API (already running at http://localhost:8100) as MCP
tools, so any MCP-compatible client (Claude Desktop, Claude Code, etc.) can inspect
live trading-platform state in a standardized way — positions, recent trades, risk
events, feed health, AI council status, and so on.

## Hard constraints — read these twice

1. READ-ONLY ONLY. Every tool must be a GET request to the existing API. Do NOT
   implement any tool that places an order, changes execution mode, clears the kill
   switch, arms live trading, or POSTs/PUTs/DELETEs anything. If you're tempted to add
   a "helpful" write capability, don't — this is a hard rule, not a suggestion.
2. Use ONLY these exact, already-existing, verified endpoint paths — do not invent or
   guess any others:
   - GET /health
   - GET /portfolio/positions
   - GET /db/trades?limit={n}
   - GET /db/equity-curve?limit={n}
   - GET /db/risk-events?limit={n}
   - GET /db/summary
   - GET /feed/snapshot
   - GET /ai-council/status
   - GET /execution/tca
   - GET /strategies/catalog
   - GET /governance
   - GET /live/readiness
   - GET /monitoring/metrics
   - GET /backtests/gates
   - GET /research/hypotheses
3. All requests go to base URL `http://localhost:8100` (configurable via an
   `MCP_TRADING_API_BASE_URL` environment variable, default that value).
4. Use the standard library `urllib.request` for HTTP calls (no `requests` dependency
   needed) — mirror this exact pattern for a GET-JSON helper:

```python
import json
import urllib.request

def _get(path: str, params: dict | None = None) -> dict:
    url = f"{BASE_URL}{path}"
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        if query:
            url = f"{url}?{query}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode())
```

5. One `@mcp.tool()` function per endpoint above, each with a clear one-line
   docstring, type-hinted parameters (e.g. `limit: int = 20` where the endpoint takes
   a limit), and returning the parsed JSON dict (or a clear error dict
   `{"error": str(exc)}` if the HTTP call fails — never let a tool raise and crash the
   server; the trading-api container may legitimately be down).
6. Name the FastMCP server instance `"trading-platform-observability"`.
7. Use stdio transport: the file's `if __name__ == "__main__":` block should call
   `mcp.run()` (FastMCP defaults to stdio transport, which is correct here — no need
   to specify transport explicitly).
8. Keep the whole file self-contained, well under 300 lines. No unnecessary
   abstraction, no config framework, no logging framework — this is a thin,
   honest passthrough layer, nothing more.

Output ONLY the complete file content in a single ```python fence."""


def extract_code(reply: str) -> str | None:
    m = re.search(r"```python\s*(.*?)```", reply, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Some thinking models omit the closing fence when they run out of budget.
    m = re.search(r"```python\s*(.*)", reply, re.DOTALL)
    return m.group(1).strip() if m else None


def main() -> int:
    client = LocalLLMClient(
        model="qwen/qwen3.6-35b-a3b", base_url="http://localhost:1234/v1",
        max_tokens=8000, timeout=900,
    )
    print("Calling local model to implement trading_platform/mcp/server.py ...")
    reply = client.complete(SYSTEM_PROMPT, TASK_PROMPT, temperature=0.2)
    print(f"Got reply, {len(reply)} chars")

    code = extract_code(reply)
    if code is None:
        print("=== COULD NOT EXTRACT CODE — raw reply below ===")
        print(reply)
        return 1

    out_dir = Path(__file__).resolve().parents[1] / "trading_platform" / "mcp"
    out_dir.mkdir(exist_ok=True)
    init_file = out_dir / "__init__.py"
    if not init_file.exists():
        init_file.write_text("")
    out_path = out_dir / "server.py"
    out_path.write_text(code, encoding="utf-8")
    print(f"Wrote {out_path} ({len(code)} chars) — NOT reviewed or tested yet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
