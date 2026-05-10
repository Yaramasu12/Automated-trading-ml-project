"""Tests for enhanced NeuralPredictionService: version pinning, ONNX export, warm standby."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from trading_platform.neural.serving import (
    _FAILURE_FALLBACK_THRESHOLD,
    _JobLibSession,
    ModelVersionRecord,
    NeuralPredictionService,
    OnnxExporter,
    WarmStandbyCache,
)


# ── ModelVersionRecord ────────────────────────────────────────────────────────

class TestModelVersionRecord:
    def test_to_dict_keys(self):
        rec = ModelVersionRecord(
            model_name="forecaster",
            version="1.0.0",
            status="champion",
            metrics={"sharpe": 1.5},
        )
        d = rec.to_dict()
        assert d["model_name"] == "forecaster"
        assert d["version"] == "1.0.0"
        assert d["status"] == "champion"
        assert d["metrics"] == {"sharpe": 1.5}
        assert d["pinned"] is False
        assert d["pinned_at"] is None
        assert "exported_at" in d

    def test_pinned_record_to_dict(self):
        now = datetime.now(timezone.utc)
        rec = ModelVersionRecord(
            model_name="forecaster",
            version="2.0.0",
            status="pinned",
            pinned=True,
            pinned_at=now,
        )
        d = rec.to_dict()
        assert d["pinned"] is True
        assert d["pinned_at"] is not None

    def test_warm_standby_ready_flag(self):
        rec = ModelVersionRecord(
            model_name="forecaster", version="1.0.0", status="champion", warm_standby_ready=True
        )
        assert rec.to_dict()["warm_standby_ready"] is True


# ── _JobLibSession ─────────────────────────────────────────────────────────────

class TestJobLibSession:
    def _mock_model(self, return_val):
        model = MagicMock()
        model.predict.return_value = return_val
        return model

    def test_run_returns_list(self):
        model = self._mock_model([1, 2, 3])
        session = _JobLibSession(model)
        result = session.run(None, {"input": [[0.1, 0.2]]})
        assert isinstance(result, list)
        assert len(result) == 1

    def test_run_passes_input_to_predict(self):
        model = self._mock_model([42])
        session = _JobLibSession(model)
        session.run(None, {"input": [[1, 2, 3]]})
        model.predict.assert_called_once_with([[1, 2, 3]])

    def test_run_accepts_numpy_array(self):
        try:
            import numpy as np
        except ImportError:
            pytest.skip("numpy not available")
        model = self._mock_model(np.array([1.0]))
        session = _JobLibSession(model)
        arr = np.array([[0.1, 0.2]])
        result = session.run(None, {"input": arr})
        assert result is not None

    def test_get_inputs_returns_list_with_input(self):
        session = _JobLibSession(MagicMock())
        inputs = session.get_inputs()
        assert len(inputs) == 1
        assert inputs[0].name == "input"


# ── WarmStandbyCache ──────────────────────────────────────────────────────────

class TestWarmStandbyCache:
    def test_save_and_get(self):
        cache = WarmStandbyCache()
        model = object()
        cache.save("tft", model, "1.0.0")
        assert cache.get("tft") is model

    def test_has_returns_false_initially(self):
        cache = WarmStandbyCache()
        assert cache.has("tft") is False

    def test_has_returns_true_after_save(self):
        cache = WarmStandbyCache()
        cache.save("tft", object())
        assert cache.has("tft") is True

    def test_evict_removes_model(self):
        cache = WarmStandbyCache()
        cache.save("tft", object())
        cache.evict("tft")
        assert cache.has("tft") is False
        assert cache.get("tft") is None

    def test_evict_missing_no_error(self):
        cache = WarmStandbyCache()
        cache.evict("nonexistent")  # should not raise

    def test_status_returns_version_map(self):
        cache = WarmStandbyCache()
        cache.save("tft", object(), "1.2.3")
        status = cache.status()
        assert "tft" in status
        assert status["tft"] == "1.2.3"

    def test_get_missing_returns_none(self):
        cache = WarmStandbyCache()
        assert cache.get("unknown") is None


# ── OnnxExporter ─────────────────────────────────────────────────────────────

class TestOnnxExporter:
    def test_export_joblib_fallback(self, tmp_path):
        """When skl2onnx is absent, falls back to joblib using a real picklable model."""
        try:
            import joblib
            from sklearn.dummy import DummyClassifier
        except ImportError:
            pytest.skip("sklearn/joblib not available")

        import numpy as np
        model = DummyClassifier(strategy="most_frequent")
        model.fit([[0], [1]], [0, 1])
        path = str(tmp_path / "test_model")
        with patch.dict("sys.modules", {"skl2onnx": None, "skl2onnx.common.data_types": None}):
            result = OnnxExporter.export(model, path)
        # Returns False for joblib fallback
        assert isinstance(result, bool)
        # Manifest should exist
        manifest_path = OnnxExporter._manifest_path(path)
        assert os.path.exists(manifest_path)
        with open(manifest_path) as fh:
            manifest = json.load(fh)
        assert "backend" in manifest

    def test_load_missing_raises(self, tmp_path):
        path = str(tmp_path / "missing_model")
        with pytest.raises(RuntimeError):
            OnnxExporter.load(path)

    def test_manifest_path_computation(self):
        path = "/tmp/model.onnx"
        manifest = OnnxExporter._manifest_path(path)
        assert manifest.endswith(".manifest.json")

    def test_write_and_read_manifest(self, tmp_path):
        path = str(tmp_path / "model")
        OnnxExporter._write_manifest(path, backend="joblib", jl_path=path + ".pkl")
        data = OnnxExporter._read_manifest(path)
        assert data["backend"] == "joblib"
        assert "jl_path" in data

    def test_read_manifest_missing_returns_empty(self, tmp_path):
        path = str(tmp_path / "no_manifest")
        data = OnnxExporter._read_manifest(path)
        assert data == {}

    def test_export_and_load_joblib_roundtrip(self, tmp_path):
        """Export a real sklearn model via joblib and reload it."""
        try:
            import joblib
            from sklearn.dummy import DummyClassifier
        except ImportError:
            pytest.skip("sklearn/joblib not available")

        model = DummyClassifier(strategy="most_frequent")
        # Fit with minimal data so predict works
        import numpy as np
        model.fit([[0], [1]], [0, 1])
        path = str(tmp_path / "dummy")
        with patch.dict("sys.modules", {"skl2onnx": None}):
            OnnxExporter.export(model, path)
        session = OnnxExporter.load(path)
        assert session is not None
        result = session.run(None, {"input": [[0]]})
        assert result is not None


# ── NeuralPredictionService: version pinning ──────────────────────────────────

class TestVersionPinning:
    def test_pin_version_creates_record(self):
        svc = NeuralPredictionService()
        rec = svc.pin_version("forecaster", "1.5.0", metrics={"sharpe": 1.2})
        assert rec.model_name == "forecaster"
        assert rec.version == "1.5.0"
        assert rec.pinned is True
        assert rec.status == "pinned"
        assert rec.metrics["sharpe"] == 1.2

    def test_pin_version_appears_in_pinned_versions(self):
        svc = NeuralPredictionService()
        svc.pin_version("forecaster", "2.0.0")
        pinned = svc.pinned_versions()
        assert "forecaster" in pinned
        assert pinned["forecaster"]["pinned"] is True

    def test_release_pin_returns_true_when_pinned(self):
        svc = NeuralPredictionService()
        svc.pin_version("forecaster", "1.0.0")
        result = svc.release_pin("forecaster")
        assert result is True
        rec = svc._version_records["forecaster"]
        assert rec.pinned is False
        assert rec.status == "champion"

    def test_release_pin_returns_false_when_not_pinned(self):
        svc = NeuralPredictionService()
        result = svc.release_pin("forecaster")
        assert result is False

    def test_pinned_version_appears_in_bundle_model_versions(self):
        svc = NeuralPredictionService()
        svc.pin_version("forecaster", "3.1.0")
        bundle = svc.predict("trace_pinned", ["NIFTY"], {})
        # model_versions dict should annotate pinned models
        assert "forecaster" in bundle.model_versions
        v = bundle.model_versions["forecaster"]
        assert "pinned" in v or "3.1.0" in v


# ── NeuralPredictionService: promote_to_champion ──────────────────────────────

class TestPromoteToChampion:
    def test_promote_creates_champion_record(self):
        svc = NeuralPredictionService()
        new_model = MagicMock()
        rec = svc.promote_to_champion("ma_forecaster", new_model, "2.0.0", metrics={"sharpe": 2.0})
        assert rec.model_name == "ma_forecaster"
        assert rec.version == "2.0.0"
        assert rec.status == "champion"
        assert rec.warm_standby_ready is True

    def test_promote_saves_old_model_to_standby(self):
        svc = NeuralPredictionService()
        old_model = svc._get_model_obj("ma_forecaster")
        new_model = MagicMock()
        svc.promote_to_champion("ma_forecaster", new_model, "2.0.0")
        assert svc._warm_standby.has("ma_forecaster")

    def test_promote_resets_failure_counter(self):
        svc = NeuralPredictionService()
        svc._failures["ma_forecaster"] = 5
        svc.promote_to_champion("ma_forecaster", MagicMock(), "2.0.0")
        assert svc._failures.get("ma_forecaster", 0) == 0

    def test_promote_installs_new_model(self):
        svc = NeuralPredictionService()
        new_model = MagicMock()
        svc.promote_to_champion("ma_forecaster", new_model, "2.0.0")
        assert svc._ma_forecaster is new_model


# ── NeuralPredictionService: warm standby fallback ────────────────────────────

class TestWarmStandbyFallback:
    def test_tft_failure_uses_ma_fallback(self):
        svc = NeuralPredictionService()
        # Make TFT claim to be available but always fail
        svc._tft_forecaster = MagicMock()
        svc._tft_forecaster.is_available.return_value = True
        svc._tft_forecaster.predict.side_effect = RuntimeError("TFT broken")

        # MA forecaster works fine
        result = svc._forecast_with_standby("NIFTY", [{"close": 100}, {"close": 101}])
        assert result is not None
        assert result.symbol == "NIFTY"

    def test_tft_threshold_not_reached_no_standby(self):
        """With only 1 failure, standby shouldn't kick in (threshold=2)."""
        svc = NeuralPredictionService()
        svc._tft_forecaster = MagicMock()
        svc._tft_forecaster.is_available.return_value = True
        svc._tft_forecaster.predict.side_effect = RuntimeError("TFT broken")
        # failures["tft"] = 0 before call; after one fail = 1 < threshold 2
        svc._failures["tft"] = 0

        standby_model = MagicMock()
        svc._warm_standby.save("tft_forecaster", standby_model)
        # First call: fail count goes to 1, but threshold is 2 → standby not reached yet
        # But then MA fallback should be used
        result = svc._forecast_with_standby("NIFTY", [{"close": 100}, {"close": 101}])
        assert result is not None

    def test_predict_with_no_bars_skips_symbol(self):
        svc = NeuralPredictionService()
        bundle = svc.predict("trace_x", ["NIFTY"], {})
        # No bars → no forecasts, but no crash
        assert bundle.trace_id == "trace_x"
        assert bundle.forecasts == []

    def test_predict_with_bars_returns_forecasts(self):
        svc = NeuralPredictionService()
        bars = [{"close": 100 + i, "high": 101 + i, "low": 99 + i, "volume": 1000, "open": 100 + i}
                for i in range(30)]
        bundle = svc.predict("trace_y", ["NIFTY"], {"NIFTY": bars})
        assert bundle.trace_id == "trace_y"
        assert len(bundle.forecasts) >= 1
        forecast = bundle.forecasts[0]
        assert 0 <= forecast.direction_probability <= 1


# ── NeuralPredictionService: export_onnx / load_onnx ─────────────────────────

class TestExportOnnx:
    def test_export_onnx_returns_dict(self, tmp_path):
        svc = NeuralPredictionService()
        model = MagicMock()
        path = str(tmp_path / "model")
        with patch("trading_platform.neural.serving.OnnxExporter.export", return_value=False) as mock_exp:
            result = svc.export_onnx("forecaster", model, path)
        assert "model_name" in result
        assert "path" in result
        assert "onnx" in result
        assert "backend" in result

    def test_export_updates_version_record_path(self, tmp_path):
        svc = NeuralPredictionService()
        svc.pin_version("forecaster", "1.0.0")
        path = str(tmp_path / "model")
        with patch("trading_platform.neural.serving.OnnxExporter.export", return_value=False):
            svc.export_onnx("forecaster", MagicMock(), path)
        rec = svc._version_records.get("forecaster")
        assert rec.export_path == path

    def test_load_onnx_returns_session(self, tmp_path):
        svc = NeuralPredictionService()
        mock_session = MagicMock()
        path = str(tmp_path / "model")
        with patch("trading_platform.neural.serving.OnnxExporter.load", return_value=mock_session):
            session = svc.load_onnx("forecaster", path)
        assert session is mock_session


# ── warm_standby_status ────────────────────────────────────────────────────────

class TestWarmStandbyStatus:
    def test_status_structure(self):
        svc = NeuralPredictionService()
        status = svc.warm_standby_status()
        assert "models_in_standby" in status
        assert "failure_counts" in status
        assert "fallback_threshold" in status
        assert status["fallback_threshold"] == _FAILURE_FALLBACK_THRESHOLD

    def test_status_shows_standby_after_promote(self):
        svc = NeuralPredictionService()
        svc.promote_to_champion("ma_forecaster", MagicMock(), "2.0.0")
        status = svc.warm_standby_status()
        assert "ma_forecaster" in status["models_in_standby"]

    def test_should_trade_with_low_uncertainty(self):
        svc = NeuralPredictionService()
        bundle = MagicMock()
        bundle.overall_uncertainty = 0.1
        assert svc.should_trade(bundle) is True

    def test_should_not_trade_with_high_uncertainty(self):
        svc = NeuralPredictionService()
        bundle = MagicMock()
        bundle.overall_uncertainty = 0.99
        assert svc.should_trade(bundle) is False
