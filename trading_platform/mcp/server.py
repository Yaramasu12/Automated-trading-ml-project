import json
import os
import urllib.request
from mcp.server.mcpserver import MCPServer

BASE_URL = os.environ.get("MCP_TRADING_API_BASE_URL", "http://localhost:8100")

mcp = MCPServer("trading-platform-observability")

def _get(path: str, params: dict | None = None) -> dict:
    url = f"{BASE_URL}{path}"
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        if query:
            url = f"{url}?{query}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        return {"error": str(exc)}

@mcp.tool()
def get_health() -> dict:
    """Retrieve platform health status."""
    return _get("/health")

@mcp.tool()
def get_portfolio_positions() -> dict:
    """Retrieve current portfolio positions."""
    return _get("/portfolio/positions")

@mcp.tool()
def get_db_trades(limit: int = 20) -> dict:
    """Retrieve recent trades from the database."""
    return _get("/db/trades", {"limit": limit})

@mcp.tool()
def get_db_equity_curve(limit: int = 20) -> dict:
    """Retrieve recent equity curve data."""
    return _get("/db/equity-curve", {"limit": limit})

@mcp.tool()
def get_db_risk_events(limit: int = 20) -> dict:
    """Retrieve recent risk events."""
    return _get("/db/risk-events", {"limit": limit})

@mcp.tool()
def get_db_summary() -> dict:
    """Retrieve database summary statistics."""
    return _get("/db/summary")

@mcp.tool()
def get_feed_snapshot() -> dict:
    """Retrieve current market feed snapshot."""
    return _get("/feed/snapshot")

@mcp.tool()
def get_ai_council_status() -> dict:
    """Retrieve AI council operational status."""
    return _get("/ai-council/status")

@mcp.tool()
def get_execution_tca() -> dict:
    """Retrieve transaction cost analysis (TCA) data."""
    return _get("/execution/tca")

@mcp.tool()
def get_strategies_catalog() -> dict:
    """Retrieve available trading strategies catalog."""
    return _get("/strategies/catalog")

@mcp.tool()
def get_governance() -> dict:
    """Retrieve platform governance configuration."""
    return _get("/governance")

@mcp.tool()
def get_live_readiness() -> dict:
    """Retrieve live trading readiness status."""
    return _get("/live/readiness")

@mcp.tool()
def get_monitoring_metrics() -> dict:
    """Retrieve platform monitoring metrics."""
    return _get("/monitoring/metrics")

@mcp.tool()
def get_backtests_gates() -> dict:
    """Retrieve backtesting gates status."""
    return _get("/backtests/gates")

@mcp.tool()
def get_research_hypotheses() -> dict:
    """Retrieve active research hypotheses."""
    return _get("/research/hypotheses")

if __name__ == "__main__":
    mcp.run()