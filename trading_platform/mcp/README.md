# Trading platform MCP server

Exposes this platform's REST API (`trading_platform/api/app.py`, running at
`http://localhost:8100` by default) as [MCP](https://modelcontextprotocol.io)
tools, so any MCP-compatible client (Claude Desktop, Claude Code, etc.) can
inspect live trading-platform state — positions, recent trades, risk events,
feed health, AI council status, governance/readiness gates — without a
separate integration for each client.

**Read-only by construction.** Every tool is a `urllib.request.urlopen(url)`
call with no request body — there is no code path in this file that can issue
a POST/PUT/DELETE. It cannot place an order, arm live trading, clear the kill
switch, or change any configuration. `tests/test_mcp_server.py` asserts this
structurally (greps the source for mutating HTTP verbs) so a future edit
can't quietly turn a tool into a write without a test failing.

Not on the money path: nothing in `api/app.py` or `api/runtime.py` imports
this package. It's a standalone, opt-in process.

## Run it

```bash
python -m trading_platform.mcp.server
```

Runs over stdio (the standard local-MCP-server transport). Point an MCP
client's config at this command, e.g. for Claude Desktop
(`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "trading-platform": {
      "command": "python",
      "args": ["-m", "trading_platform.mcp.server"],
      "cwd": "/path/to/Automated-trading-ml-project"
    }
  }
}
```

Set `MCP_TRADING_API_BASE_URL` if the API isn't at the default
`http://localhost:8100` (e.g. `http://localhost:8100` when run outside
Docker, or a container-internal address when run alongside it).

## Tools

15 tools, one per reviewed endpoint: `get_health`, `get_portfolio_positions`,
`get_db_trades`, `get_db_equity_curve`, `get_db_risk_events`, `get_db_summary`,
`get_feed_snapshot`, `get_ai_council_status`, `get_execution_tca`,
`get_strategies_catalog`, `get_governance`, `get_live_readiness`,
`get_monitoring_metrics`, `get_backtests_gates`, `get_research_hypotheses`.

Adding a new tool: add it to the exact list above AND to `EXPECTED_TOOLS` in
`tests/test_mcp_server.py` — the test suite fails deliberately on a mismatch
so the reviewed tool surface can't silently drift.
