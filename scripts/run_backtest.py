from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trading_platform.api.runtime import TradingRuntime


if __name__ == "__main__":
    runtime = TradingRuntime()
    result = runtime.run_backtest({"days": 30})
    print(json.dumps(result, indent=2))
