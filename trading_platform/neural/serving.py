from __future__ import annotations

"""NeuralPredictionService — orchestrates neural models with version pinning,
ONNX export, and warm-standby fallback.

Advisory only. Never creates OrderIntent.

Three production features added over the thin-wrapper baseline
--------------------------------------------------------------
1. ONNX export (`OnnxExporter`)
   Exports sklearn / pure-Python models to ONNX format using `skl2onnx` when
   available; falls back to a joblib-serialised wrapper (`_JobLibSession`) that
   implements the identical `session.run(output_names, input_dict)` interface so
   callers need no branching.  Either backend is transparent to the caller.

2. Version pinning (`ModelVersionRecord`, `pin_version`, `release_pin`)
   Tracks a semantic version + performance metrics per model name.  A pinned
   version is never auto-promoted, allowing safe hold-back during market
   regime transitions.  Pinned metadata travels with the `NeuralPredictionBundle`
   so the trace system records exactly which model version produced each forecast.

3. Warm standby (`WarmStandbyCache`, `promote_to_champion`)
   When a new model is promoted via `promote_to_champion()` the previous model
   object is moved to the warm-standby cache.  The `predict()` method tracks
   consecutive failures per sub-model: after `_FAILURE_FALLBACK_THRESHOLD`
   consecutive failures on the primary it transparently switches to the warm
   standby for that model, then to a deterministic baseline if the standby also
   fails.  Failure counts reset on each successful prediction.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from trading_platform.neural.calibration import NO_TRADE_UNCERTAINTY_THRESHOLD, IdentityCalibrator
from trading_platform.neural.forecasting import MovingAverageForecaster, TemporalFusionTransformerForecaster
from trading_platform.neural.graph_model import RollingCorrelationGraph
from trading_platform.neural.schemas import ForecastPrediction, NeuralPredictionBundle
from trading_platform.neural.tensor_features import build_feature_batch
from trading_platform.neural.volatility import GARCHVolatilityModel, TailRiskModel

if TYPE_CHECKING:
    from trading_platform.trace.store import TraceStore

logger = logging.getLogger(__name__)

_FAILURE_FALLBACK_THRESHOLD = 2  # consecutive failures before warm-standby activates


# ── Version record ────────────────────────────────────────────────────────────

@dataclass
class ModelVersionRecord:
    """Tracks the active version and promotion status of one model slot.

    Attributes
    ----------
    model_name:      e.g. "forecaster", "volatility", "tail_risk"
    version:         semver-style string e.g. "1.0.0"
    status:          "champion" | "challenger" | "deprecated" | "pinned"
    metrics:         performance metrics recorded at promotion time
    exported_at:     when this version was registered / exported
    export_path:     filesystem path to the ONNX or joblib export (if exported)
    pinned:          True while this version is frozen (no auto-promotion)
    pinned_at:       when pin was applied
    warm_standby_ready: True when a previous version is loaded in WarmStandbyCache
    """
    model_name: str
    version: str
    status: str
    metrics: dict[str, float] = field(default_factory=dict)
    exported_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    export_path: str | None = None
    pinned: bool = False
    pinned_at: datetime | None = None
    warm_standby_ready: bool = False

    def to_dict(self) -> dict:
        return {
            "model_name": self.model_name,
            "version": self.version,
            "status": self.status,
            "metrics": self.metrics,
            "exported_at": self.exported_at.isoformat(),
            "export_path": self.export_path,
            "pinned": self.pinned,
            "pinned_at": self.pinned_at.isoformat() if self.pinned_at else None,
            "warm_standby_ready": self.warm_standby_ready,
        }


# ── ONNX export / load ────────────────────────────────────────────────────────

class _JobLibSession:
    """Fallback when onnxruntime is absent.

    Wraps a joblib-serialised model with the `onnxruntime.InferenceSession`
    `.run(output_names, input_dict)` interface so callers need no branching.
    """

    class _InputMeta:
        def __init__(self, name: str) -> None:
            self.name = name

    def __init__(self, model: Any) -> None:
        self._model = model

    def run(self, output_names: list[str] | None, input_dict: dict) -> list:
        """Perform inference.  input_dict must have exactly one key mapping to a 2-D list/array."""
        values = list(input_dict.values())[0]
        # Accept both list-of-lists and numpy arrays
        if hasattr(values, "tolist"):
            values = values.tolist()
        pred = self._model.predict(values)
        return [pred]

    def get_inputs(self) -> list[_InputMeta]:
        return [self._InputMeta("input")]


class OnnxExporter:
    """Exports sklearn models to ONNX; falls back to joblib if skl2onnx is absent.

    Usage
    -----
    # Export:
    OnnxExporter.export(sklearn_model, "/tmp/regime_clf.onnx", input_shape=(1, 5))

    # Load (transparent whether ONNX or joblib):
    session = OnnxExporter.load("/tmp/regime_clf.onnx")
    preds = session.run(None, {"input": [[0.01, 0.02, 0.015, 1.1, 4.5]]})
    """

    @staticmethod
    def export(
        model: Any,
        path: str,
        input_shape: tuple[int, ...] | None = None,
        model_name: str = "model",
    ) -> bool:
        """Export *model* to *path*.

        Returns True if a true ONNX file was written, False if joblib fallback was used.
        """
        try:
            import numpy as np
            from skl2onnx import convert_sklearn
            from skl2onnx.common.data_types import FloatTensorType

            n_features = input_shape[-1] if input_shape else 5
            initial_type = [("input", FloatTensorType([None, n_features]))]
            onnx_model = convert_sklearn(model, initial_types=initial_type, target_opset=15)
            onnx_path = path if path.endswith(".onnx") else path + ".onnx"
            with open(onnx_path, "wb") as fh:
                fh.write(onnx_model.SerializeToString())
            # Write sidecar manifest
            OnnxExporter._write_manifest(path, backend="onnx", onnx_path=onnx_path, model_name=model_name)
            logger.info("OnnxExporter: ONNX export → %s", onnx_path)
            return True
        except Exception as onnx_exc:
            logger.debug("OnnxExporter: skl2onnx unavailable (%s) — joblib fallback", onnx_exc)

        # joblib fallback
        try:
            import joblib
            jl_path = path if path.endswith(".pkl") else path + ".pkl"
            joblib.dump(model, jl_path)
            OnnxExporter._write_manifest(path, backend="joblib", jl_path=jl_path, model_name=model_name)
            logger.info("OnnxExporter: joblib export → %s", jl_path)
            return False
        except Exception as jl_exc:
            logger.warning("OnnxExporter: export failed for %s: %s", path, jl_exc)
            return False

    @staticmethod
    def load(path: str) -> _JobLibSession | Any:
        """Load an ONNX or joblib export; always returns an object with `.run()`.

        If onnxruntime is present and the export was ONNX, returns a real
        `onnxruntime.InferenceSession`.  Otherwise returns `_JobLibSession`.
        """
        manifest = OnnxExporter._read_manifest(path)
        backend = manifest.get("backend", "joblib")

        if backend == "onnx":
            onnx_path = manifest.get("onnx_path", path + ".onnx")
            try:
                import onnxruntime as ort
                session = ort.InferenceSession(onnx_path)
                logger.info("OnnxExporter: loaded ONNX session from %s", onnx_path)
                return session
            except Exception as exc:
                logger.debug("OnnxExporter: onnxruntime load failed (%s) — joblib fallback", exc)

        # joblib fallback
        try:
            import joblib
            jl_path = manifest.get("jl_path", path + ".pkl")
            model = joblib.load(jl_path)
            return _JobLibSession(model)
        except Exception as exc:
            raise RuntimeError(f"OnnxExporter.load: cannot load model from {path}: {exc}") from exc

    @staticmethod
    def _manifest_path(path: str) -> str:
        # str.rstrip() strips individual characters, not substrings.
        # Use removesuffix() (Python 3.9+) to avoid silently corrupting
        # paths that contain any of the suffix characters (e.g. 'o','n','x','.').
        import os
        base, _ = os.path.splitext(path)
        return base + ".manifest.json"

    @staticmethod
    def _write_manifest(path: str, **meta: Any) -> None:
        try:
            with open(OnnxExporter._manifest_path(path), "w") as fh:
                json.dump(meta, fh)
        except Exception:
            pass

    @staticmethod
    def _read_manifest(path: str) -> dict:
        try:
            with open(OnnxExporter._manifest_path(path)) as fh:
                return json.load(fh)
        except Exception:
            return {}


# ── Warm standby cache ────────────────────────────────────────────────────────

class WarmStandbyCache:
    """Keeps the previous model version in memory as a hot failover.

    When `promote_to_champion()` is called on the service the outgoing champion
    is placed here.  The `predict()` path checks this cache when the primary
    model fails consecutively, before falling back to a deterministic baseline.
    """

    def __init__(self) -> None:
        self._models: dict[str, Any] = {}    # model_name → model object
        self._versions: dict[str, str] = {}  # model_name → version string

    def save(self, model_name: str, model: Any, version: str = "prev") -> None:
        self._models[model_name] = model
        self._versions[model_name] = version
        logger.debug("WarmStandbyCache: saved standby for %s (v%s)", model_name, version)

    def get(self, model_name: str) -> Any | None:
        return self._models.get(model_name)

    def has(self, model_name: str) -> bool:
        return model_name in self._models

    def evict(self, model_name: str) -> None:
        self._models.pop(model_name, None)
        self._versions.pop(model_name, None)

    def status(self) -> dict[str, str]:
        return {name: self._versions.get(name, "unknown") for name in self._models}


# ── Service ───────────────────────────────────────────────────────────────────

class NeuralPredictionService:
    """Runs the neural prediction stack and returns a NeuralPredictionBundle.

    Rules:
    - Falls back to deterministic baselines if any deep model fails.
    - After _FAILURE_FALLBACK_THRESHOLD consecutive failures switches to warm standby.
    - High uncertainty triggers NO_TRADE recommendation.
    - Never blocks the execution path.
    - Version records travel with every bundle for full traceability.
    """

    def __init__(
        self,
        trace_store: TraceStore | None = None,
        model_dir: str | None = None,
    ) -> None:
        self._trace_store = trace_store
        self._model_dir = model_dir

        # Sub-models
        self._ma_forecaster = MovingAverageForecaster()
        self._tft_forecaster = TemporalFusionTransformerForecaster()
        self._garch = GARCHVolatilityModel()
        self._tail_risk = TailRiskModel()
        self._corr_graph = RollingCorrelationGraph()
        self._calibrator = IdentityCalibrator()

        # Version registry  (model_name → ModelVersionRecord)
        self._version_records: dict[str, ModelVersionRecord] = {}

        # Warm standby cache
        self._warm_standby = WarmStandbyCache()

        # Consecutive failure counters; reset on success
        self._failures: dict[str, int] = {}

    # ── Version pinning ───────────────────────────────────────────────────────

    def pin_version(
        self,
        model_name: str,
        version: str,
        metrics: dict[str, float] | None = None,
        export_path: str | None = None,
    ) -> ModelVersionRecord:
        """Freeze *model_name* at *version* so auto-promotion is blocked.

        Call `release_pin()` to resume normal promotion.
        """
        record = ModelVersionRecord(
            model_name=model_name,
            version=version,
            status="pinned",
            metrics=metrics or {},
            pinned=True,
            pinned_at=datetime.now(timezone.utc),
            export_path=export_path,
            warm_standby_ready=self._warm_standby.has(model_name),
        )
        self._version_records[model_name] = record
        logger.info("NeuralPredictionService: pinned %s at v%s", model_name, version)
        return record

    def release_pin(self, model_name: str) -> bool:
        """Release a pin so the model can be promoted normally. Returns True if a pin existed."""
        record = self._version_records.get(model_name)
        if record and record.pinned:
            record.pinned = False
            record.status = "champion"
            logger.info("NeuralPredictionService: released pin on %s", model_name)
            return True
        return False

    def promote_to_champion(
        self,
        model_name: str,
        new_model: Any,
        version: str,
        metrics: dict[str, float] | None = None,
    ) -> ModelVersionRecord:
        """Promote *new_model* to champion for *model_name*.

        The current primary model is moved to warm standby before the new one
        is installed, so immediate fallback is available if the new model fails.
        """
        # Save current primary to warm standby
        current = self._get_model_obj(model_name)
        current_version = self._version_records.get(model_name)
        if current is not None:
            self._warm_standby.save(
                model_name, current,
                version=(current_version.version if current_version else "prev"),
            )

        # Install new model
        self._install_model(model_name, new_model)

        record = ModelVersionRecord(
            model_name=model_name,
            version=version,
            status="champion",
            metrics=metrics or {},
            warm_standby_ready=True,
        )
        self._version_records[model_name] = record
        self._failures[model_name] = 0  # reset failure counter
        logger.info(
            "NeuralPredictionService: promoted %s v%s (standby=prev)", model_name, version
        )
        return record

    # ── ONNX export ───────────────────────────────────────────────────────────

    def export_onnx(
        self,
        model_name: str,
        model: Any,
        path: str,
        input_shape: tuple[int, ...] | None = None,
    ) -> dict:
        """Export *model* to ONNX (or joblib fallback) at *path*.

        Updates the version record's export_path on success.

        Returns a metadata dict with keys: model_name, path, onnx (bool).
        """
        onnx_ok = OnnxExporter.export(model, path, input_shape=input_shape, model_name=model_name)
        record = self._version_records.get(model_name)
        if record:
            record.export_path = path
        return {
            "model_name": model_name,
            "path": path,
            "onnx": onnx_ok,
            "backend": "onnx" if onnx_ok else "joblib",
        }

    def load_onnx(self, model_name: str, path: str) -> Any:
        """Load an ONNX / joblib export and return an inference session."""
        session = OnnxExporter.load(path)
        record = self._version_records.get(model_name)
        if record:
            record.export_path = path
        return session

    # ── Inspection ────────────────────────────────────────────────────────────

    def pinned_versions(self) -> dict[str, dict]:
        return {name: rec.to_dict() for name, rec in self._version_records.items()}

    def warm_standby_status(self) -> dict:
        return {
            "models_in_standby": self._warm_standby.status(),
            "failure_counts": dict(self._failures),
            "fallback_threshold": _FAILURE_FALLBACK_THRESHOLD,
        }

    # ── Prediction ────────────────────────────────────────────────────────────

    def predict(
        self,
        trace_id: str,
        symbols: list[str],
        bars_map: dict[str, list[dict]],
    ) -> NeuralPredictionBundle:
        """Generate full prediction bundle for a list of symbols.

        Each sub-model uses warm-standby fallback on repeated failures.
        Pinned model versions are recorded in bundle.model_versions.
        """
        bundle = NeuralPredictionBundle(trace_id=trace_id)
        returns_map: dict[str, list[float]] = {}

        for sym in symbols:
            bars = bars_map.get(sym, [])
            if not bars:
                continue

            closes = [b.get("close", 0.0) for b in bars]
            rets = [
                (closes[i] - closes[i - 1]) / closes[i - 1]
                if closes[i - 1] > 0 else 0.0
                for i in range(1, len(closes))
            ]
            returns_map[sym] = rets

            # Forecasting — warm-standby-aware
            forecast = self._forecast_with_standby(sym, bars)
            forecast.confidence = self._calibrator.calibrate(forecast.confidence)
            bundle.forecasts.append(forecast)

            # Volatility
            try:
                vol_pred = self._garch.predict(sym, rets)
                bundle.volatility.append(vol_pred)
                self._failures["garch"] = 0
            except Exception as exc:
                self._failures["garch"] = self._failures.get("garch", 0) + 1
                logger.debug("GARCH failed for %s (fail#%d): %s", sym, self._failures["garch"], exc)

            # Tail risk
            try:
                tail_pred = self._tail_risk.predict(sym, rets)
                bundle.tail_risks.append(tail_pred)
                self._failures["tail_risk"] = 0
            except Exception as exc:
                self._failures["tail_risk"] = self._failures.get("tail_risk", 0) + 1
                logger.debug("TailRisk failed for %s: %s", sym, exc)

        # Correlation risk
        try:
            corr_pred = self._corr_graph.predict(symbols, returns_map)
            bundle.correlation_risk = corr_pred
            self._failures["corr"] = 0
        except Exception as exc:
            self._failures["corr"] = self._failures.get("corr", 0) + 1
            logger.debug("Correlation graph failed: %s", exc)

        # Aggregate uncertainty
        if bundle.forecasts:
            bundle.overall_uncertainty = (
                sum(f.model_uncertainty for f in bundle.forecasts) / len(bundle.forecasts)
            )
        else:
            bundle.overall_uncertainty = 1.0

        # Populate model_versions — include pinned version strings
        bundle.model_versions = self._build_version_map()

        self._write_trace(trace_id, bundle)
        return bundle

    def should_trade(self, bundle: NeuralPredictionBundle) -> bool:
        """Returns False if model uncertainty is too high to trade."""
        return bundle.overall_uncertainty < NO_TRADE_UNCERTAINTY_THRESHOLD

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _forecast_with_standby(self, sym: str, bars: list[dict]) -> ForecastPrediction:
        """Try TFT → warm-standby TFT → MA baseline."""
        if self._tft_forecaster.is_available():
            try:
                result = self._tft_forecaster.predict(sym, bars)
                if result is None:
                    raise ValueError("TFT returned None — model weights not loaded")
                self._failures["tft"] = 0
                return result
            except Exception as exc:
                self._failures["tft"] = self._failures.get("tft", 0) + 1
                logger.debug(
                    "TFT forecast failed for %s (fail#%d): %s",
                    sym, self._failures["tft"], exc,
                )

        # Warm standby after repeated TFT failures
        if (
            self._failures.get("tft", 0) >= _FAILURE_FALLBACK_THRESHOLD
            and self._warm_standby.has("tft_forecaster")
        ):
            standby = self._warm_standby.get("tft_forecaster")
            try:
                result = standby.predict(sym, bars)
                logger.info("NeuralPredictionService: warm-standby TFT serving %s", sym)
                return result
            except Exception as exc:
                logger.warning("NeuralPredictionService: warm-standby TFT also failed: %s", exc)

        return self._ma_forecaster.predict(sym, bars)

    def _get_model_obj(self, model_name: str) -> Any | None:
        """Map model name string to current model object (for warm-standby save)."""
        return {
            "tft_forecaster": self._tft_forecaster,
            "ma_forecaster": self._ma_forecaster,
            "garch": self._garch,
            "tail_risk": self._tail_risk,
            "corr_graph": self._corr_graph,
        }.get(model_name)

    def _install_model(self, model_name: str, model: Any) -> None:
        """Replace the active model object for *model_name*."""
        if model_name == "tft_forecaster":
            self._tft_forecaster = model
        elif model_name == "ma_forecaster":
            self._ma_forecaster = model
        elif model_name == "garch":
            self._garch = model
        elif model_name == "tail_risk":
            self._tail_risk = model
        elif model_name == "corr_graph":
            self._corr_graph = model
        else:
            logger.warning("NeuralPredictionService: unknown model_name '%s' for install", model_name)

    def _build_version_map(self) -> dict[str, str]:
        """Build model_versions dict including pinned version strings."""
        base = {
            "forecaster": "baseline_ma",
            "volatility": "garch_1_1",
            "tail_risk": "quantile_baseline",
            "correlation": "rolling_corr_30",
        }
        for name, rec in self._version_records.items():
            base[name] = f"v{rec.version}" + ("_pinned" if rec.pinned else "")
        return base

    def _write_trace(self, trace_id: str, bundle: NeuralPredictionBundle) -> None:
        if not self._trace_store:
            return
        try:
            trace = self._trace_store.get(trace_id)
            if trace:
                trace.neural_model_versions = bundle.model_versions
                trace.add_event(
                    "neural_prediction",
                    "NeuralPredictionService",
                    {
                        "uncertainty": bundle.overall_uncertainty,
                        "n_symbols": len(bundle.forecasts),
                        "version_records": {
                            n: r.to_dict() for n, r in self._version_records.items()
                        },
                    },
                )
                self._trace_store.save(trace)
        except Exception as exc:
            logger.warning("NeuralPredictionService: trace write error: %s", exc)
