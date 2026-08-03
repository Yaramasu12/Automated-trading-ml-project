"""IST-market-hours logic under trading_platform/agent/ must read the clock
through now_ist() (agent/market_hours.py), never a naive datetime.now() —
a naive call silently reads the host's local time instead of IST, which is
wrong whenever this runs on a non-IST host/container (see CLAUDE.md: "always
use this, never naive datetime.now()").

now_ist() itself legitimately calls datetime.now(IST) — that's timezone-aware,
not naive, so it's exempted by file rather than by call shape."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent / "trading_platform" / "agent"

# now_ist()'s own implementation calls datetime.now(IST) — timezone-aware, but
# structurally indistinguishable from a naive call by argument count alone
# (IST is a module-level constant, not detectable via simple AST shape
# matching without a full symbol table). Exempt this one file by design.
EXEMPT_FILES = {"market_hours.py"}


def _naive_now_calls(source: str, filename: str) -> list[int]:
    """Line numbers of any `datetime.now()` call with zero arguments."""
    tree = ast.parse(source, filename=filename)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "now"
            and isinstance(func.value, ast.Name)
            and func.value.id == "datetime"
            and not node.args
            and not node.keywords
        ):
            hits.append(node.lineno)
    return hits


class NoNaiveDatetimeNowInAgentTests(unittest.TestCase):
    def test_no_module_under_agent_calls_naive_datetime_now(self):
        violations: dict[str, list[int]] = {}
        for path in sorted(AGENT_DIR.glob("*.py")):
            if path.name in EXEMPT_FILES:
                continue
            hits = _naive_now_calls(path.read_text(encoding="utf-8"), path.name)
            if hits:
                violations[path.name] = hits
        self.assertEqual(
            violations, {},
            f"naive datetime.now() found under trading_platform/agent/ (use now_ist() instead): {violations}",
        )

    def test_detector_actually_catches_a_naive_call(self):
        """Guard the guard: confirm the AST check flags a real violation,
        so this test can't pass vacuously if the detector itself is broken."""
        # datetime.now() (bare), the actual pattern this codebase forbids:
        sample2 = "from datetime import datetime\nx = datetime.now()\n"
        self.assertEqual(_naive_now_calls(sample2, "sample.py"), [2])
        # A timezone-aware call must NOT be flagged.
        aware = "from datetime import datetime, timezone\nx = datetime.now(timezone.utc)\n"
        self.assertEqual(_naive_now_calls(aware, "sample.py"), [])


if __name__ == "__main__":
    unittest.main()
