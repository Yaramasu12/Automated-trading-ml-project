"""Frontend <-> backend API contract guardrail (Phase 3).

The frontend (hft_frontend/src/api.ts) calls backend routes by hard-coded path
string. Nothing previously stopped those from drifting apart when a route was
renamed/removed — which is exactly how a UI tab silently 404s and "disappears".

This test parses the FastAPI route table from app.py and every API path literal
from api.ts, and asserts every frontend call maps to a real backend route. If it
fails, either the backend route changed (update api.ts) or a genuinely new path
was added (add it to ALLOWLIST below with a reason).
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_APP = _ROOT / "trading_platform" / "api" / "app.py"
_API_TS = _ROOT / "hft_frontend" / "src" / "api.ts"

# Literals in api.ts that start with "/" but are NOT backend routes.
_ALLOWLIST = {
    "/api",   # the VITE_API_URL base-path default, prepended to every real path
}


def _norm(path: str) -> str:
    path = path.split("?", 1)[0]                  # drop query string
    path = re.sub(r"\$\{[^}]+\}", "*", path)       # ${id}  -> *
    path = re.sub(r"\{[^}]+\}", "*", path)         # {id}   -> *
    return path.rstrip("/") or "/"


def _backend_routes() -> set[str]:
    src = _APP.read_text()
    return {
        _norm(m.group(2))
        for m in re.finditer(
            r'@app\.(get|post|put|delete|websocket)\(\s*["\']([^"\']+)["\']', src
        )
    }


def _frontend_paths() -> set[str]:
    src = _API_TS.read_text()
    paths: set[str] = set()
    for m in re.finditer(r"""[`'"](/[A-Za-z0-9_/${}.*?=&:-]*)[`'"]""", src):
        paths.add(_norm(m.group(1)))
    return paths


class FrontendBackendContractTest(unittest.TestCase):
    def test_files_exist(self):
        self.assertTrue(_APP.exists(), f"missing {_APP}")
        self.assertTrue(_API_TS.exists(), f"missing {_API_TS}")

    def test_every_frontend_call_has_a_backend_route(self):
        backend = _backend_routes()
        frontend = _frontend_paths()
        self.assertGreater(len(backend), 50, "route parse looks broken")
        self.assertGreater(len(frontend), 30, "api.ts path parse looks broken")

        drift = sorted(p for p in frontend if p not in backend and p not in _ALLOWLIST)
        self.assertEqual(
            drift, [],
            "Frontend calls a path with no matching backend route (UI tab would "
            f"404 / disappear): {drift}. Fix api.ts or update app.py.",
        )
