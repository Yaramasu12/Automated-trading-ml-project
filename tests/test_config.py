from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trading_platform.config import load_local_env_files, load_settings


class ConfigTests(unittest.TestCase):
    def test_loads_local_env_without_overwriting_existing_environment(self):
        original = os.environ.get("ANGEL_ONE_CLIENT_CODE")
        os.environ["ANGEL_ONE_CLIENT_CODE"] = "existing"
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                Path(".env.local").write_text(
                    "ANGEL_ONE_CLIENT_CODE=from-file\nANGEL_ONE_API_KEY=from-file\n",
                    encoding="utf-8",
                )
                os.environ.pop("ANGEL_ONE_API_KEY", None)

                load_local_env_files()

                self.assertEqual(os.environ["ANGEL_ONE_CLIENT_CODE"], "existing")
                self.assertEqual(os.environ["ANGEL_ONE_API_KEY"], "from-file")
            finally:
                os.chdir(previous_cwd)
                os.environ.pop("ANGEL_ONE_API_KEY", None)
                if original is None:
                    os.environ.pop("ANGEL_ONE_CLIENT_CODE", None)
                else:
                    os.environ["ANGEL_ONE_CLIENT_CODE"] = original


class AnnualTargetPctTests(unittest.TestCase):
    """2026-08-06: replaces the hardcoded 0.40 GoalGovernance used to
    construct with regardless of config (REDESIGN_PROMPT.md §9)."""

    def test_defaults_to_35_percent(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANNUAL_TARGET_PCT", None)
            settings = load_settings()
        self.assertEqual(settings.annual_target_pct, 0.35)

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"ANNUAL_TARGET_PCT": "0.28"}):
            settings = load_settings()
        self.assertEqual(settings.annual_target_pct, 0.28)


if __name__ == "__main__":
    unittest.main()
