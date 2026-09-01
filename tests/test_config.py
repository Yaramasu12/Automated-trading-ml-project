from __future__ import annotations

import logging
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


class LocalLLMSecondaryModelTests(unittest.TestCase):
    """2026-09-01: added as a 4th model-pool slot when gemma-4-e4b (the
    small/fast model the original fast/primary/coordinator split was built
    around) became unavailable in LM Studio -- see specialists.py's
    BATCHABLE_AGENT_CLASSES round-robin."""

    def test_falls_back_to_fast_model_when_unset(self):
        # Patch load_local_env_files() to a no-op: this repo's own .env sets
        # LOCAL_LLM_SECONDARY_MODEL for real deployment, which would otherwise
        # leak into this test's "unset" scenario since load_local_env_files()
        # deliberately does not overwrite an env var that's merely absent from
        # os.environ at call time -- it fills it right back in from the file.
        with mock.patch("trading_platform.config.load_local_env_files"):
            with mock.patch.dict(os.environ, {"LOCAL_LLM_FAST_MODEL": "some-fast-model"}):
                os.environ.pop("LOCAL_LLM_SECONDARY_MODEL", None)
                settings = load_settings()
        self.assertEqual(settings.local_llm_secondary_model, "some-fast-model")

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"LOCAL_LLM_SECONDARY_MODEL": "some-other-model"}):
            settings = load_settings()
        self.assertEqual(settings.local_llm_secondary_model, "some-other-model")


class LoadSettingsConfiguresLoggingTests(unittest.TestCase):
    """2026-08-31: nothing in the app's startup path called logging.basicConfig(),
    so the root logger had no handler and every note_swallowed()/logger.warning()
    call was invisible in `docker logs` regardless of volume — confirmed live:
    1197 swallowed exceptions counted in /health, 0 corresponding WARNING lines
    in 34h of container logs. load_settings() must attach a handler so WARNING+
    records actually reach a stream."""

    def test_root_logger_gets_a_handler_capable_of_warning(self):
        root = logging.getLogger()
        original_handlers = list(root.handlers)
        original_level = root.level
        root.handlers = []
        try:
            load_settings()
            self.assertTrue(root.handlers, "load_settings() must attach a handler to the root logger")
            self.assertTrue(root.isEnabledFor(logging.WARNING))
        finally:
            root.handlers = original_handlers
            root.setLevel(original_level)

    def test_is_idempotent_across_repeated_calls(self):
        root = logging.getLogger()
        original_handlers = list(root.handlers)
        original_level = root.level
        root.handlers = []
        try:
            load_settings()
            first_count = len(root.handlers)
            load_settings()
            self.assertEqual(len(root.handlers), first_count)
        finally:
            root.handlers = original_handlers
            root.setLevel(original_level)


if __name__ == "__main__":
    unittest.main()
