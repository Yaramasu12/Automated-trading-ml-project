from __future__ import annotations

import time
import uuid


def new_trace_id(prefix: str = "scan") -> str:
    """Generate a unique, time-ordered trace ID.

    Format: {prefix}-{unix_ms_hex}-{random_hex8}
    Monotone within a process; unique globally with high probability.
    """
    ts_hex = format(int(time.time() * 1000), "x")
    rand_hex = uuid.uuid4().hex[:8]
    return f"{prefix}-{ts_hex}-{rand_hex}"
