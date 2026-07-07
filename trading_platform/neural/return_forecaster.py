"""GradientBoostedReturnForecaster — a *real*, validated direction model.

This replaces the placeholder TFT (which returned None) with an actual trained
model, following the same honesty discipline as `ai.models.RegimeClassifier`:

  * It predicts P(up) over a fixed forward horizon from engineered price/volume
    features using sklearn's HistGradientBoostingClassifier.
  * `train()` runs **walk-forward** (expanding-window, chronological) validation
    and only ACCEPTS the model when its out-of-sample ROC-AUC and accuracy beat
    the naive baselines by a margin.  On a driftless random walk this correctly
    fails — so the live system never gets a fake "neural" edge.
  * `predict()` returns a `ForecastPrediction` only when a validated model is
    loaded; otherwise it returns None and `NeuralPredictionService` falls back to
    the moving-average baseline.

Feature engineering and metrics are pure numpy + sklearn (both already deps).
"""
from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np

from trading_platform.neural.schemas import ForecastPrediction

logger = logging.getLogger(__name__)

# Bars of history needed before the first feature row can be formed.
LOOKBACK = 20
# Forward horizon (bars) the direction label is computed over. 1 bar, because
# the engineered features are bar-level momentum/mean-reversion signals: their
# information dilutes roughly as sqrt(h) against the h-bar return variance, and
# empirically the walk-forward gate cannot reliably certify even strong genuine
# AR(1) edge at horizon 5. Multi-bar horizons remain available via the
# constructor / training-script sweep, which picks whatever horizon validates.
DEFAULT_HORIZON = 1
# Acceptance margins over the naive baselines (out-of-sample).
# MIN_AUC_GAIN is a floor: the effective AUC threshold is
# 0.5 + max(MIN_AUC_GAIN, 2 * SE_null), where SE_null is the Hanley-McNeil
# standard error of AUC under the no-skill null. On small OOS samples
# (single symbol, ~650 rows) pure noise clears a fixed 0.52 bar about 20% of
# the time; scaling with sample size keeps the gate honest there while leaving
# the large pooled real-data pipeline (threshold ≈ 0.523) essentially unchanged.
MIN_AUC_GAIN = 0.02      # floor on how much OOS AUC must exceed 0.50
MIN_ACC_GAIN = 0.01      # OOS accuracy must exceed majority-class rate by this much
MIN_TRAIN_ROWS = 120     # need a reasonable sample before trusting validation
MODEL_ID = "gbm_direction_v1"

FEATURE_NAMES = [
    "ret_1", "ret_2", "ret_3", "ret_5", "ret_10",
    "sma5_sma20", "close_sma20", "vol_10", "vol_20",
    "rsi_14", "mom_5", "mom_10", "hl_range", "vol_ratio",
]


def _rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes[-(period + 1):])
    gains = np.clip(deltas, 0, None)
    losses = -np.clip(deltas, None, 0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _feature_row(closes: np.ndarray, highs: np.ndarray, lows: np.ndarray, volumes: np.ndarray) -> list[float] | None:
    """Build one feature vector from price/volume history ending at the current bar."""
    if len(closes) < LOOKBACK:
        return None
    c = closes
    rets = np.diff(c) / np.where(c[:-1] == 0, 1.0, c[:-1])

    def lag_ret(n: int) -> float:
        return float(rets[-n]) if len(rets) >= n else 0.0

    sma5 = c[-5:].mean()
    sma20 = c[-20:].mean()
    vol10 = float(rets[-10:].std()) if len(rets) >= 10 else float(rets.std() or 0.0)
    vol20 = float(rets[-20:].std()) if len(rets) >= 20 else vol10
    mom5 = float(c[-1] / c[-6] - 1.0) if len(c) >= 6 and c[-6] > 0 else 0.0
    mom10 = float(c[-1] / c[-11] - 1.0) if len(c) >= 11 and c[-11] > 0 else 0.0
    hl_range = float((highs[-1] - lows[-1]) / c[-1]) if c[-1] > 0 else 0.0
    vol_sma = volumes[-20:].mean() if len(volumes) >= 20 else (volumes.mean() or 1.0)
    vol_ratio = float(volumes[-1] / vol_sma) if vol_sma > 0 else 1.0

    return [
        lag_ret(1), lag_ret(2), lag_ret(3), lag_ret(5), lag_ret(10),
        float(sma5 / sma20 - 1.0) if sma20 > 0 else 0.0,
        float(c[-1] / sma20 - 1.0) if sma20 > 0 else 0.0,
        vol10, vol20,
        _rsi(c), mom5, mom10, hl_range, vol_ratio,
    ]


def _bars_to_arrays(bars: list[dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    closes = np.array([float(b.get("close", 0.0) or 0.0) for b in bars], dtype=float)
    highs = np.array([float(b.get("high", b.get("close", 0.0)) or 0.0) for b in bars], dtype=float)
    lows = np.array([float(b.get("low", b.get("close", 0.0)) or 0.0) for b in bars], dtype=float)
    volumes = np.array([float(b.get("volume", 0.0) or 0.0) for b in bars], dtype=float)
    return closes, highs, lows, volumes


def _row_ts(bar: dict, fallback: float) -> float:
    """Epoch-seconds timestamp for global ordering; falls back to a row index."""
    raw = bar.get("ts", bar.get("timestamp"))
    if raw is None:
        return fallback
    try:
        from datetime import datetime
        if isinstance(raw, (int, float)):
            return float(raw)
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except Exception:
        return fallback


def _build_rows(bars: list[dict], horizon: int) -> tuple[list[list[float]], list[int], list[float], list[float]]:
    """Per-series feature rows. Returns (X_rows, y, timestamps, abs_forward_moves).

    Row i uses data up to bar i (no lookahead); label is 1 if the forward
    `horizon`-bar return is positive. Only indices with a realised outcome included.
    """
    closes, highs, lows, volumes = _bars_to_arrays(bars)
    n = len(closes)
    X: list[list[float]] = []
    y: list[int] = []
    ts: list[float] = []
    fwd_moves: list[float] = []
    for i in range(LOOKBACK, n - horizon):
        row = _feature_row(closes[: i + 1], highs[: i + 1], lows[: i + 1], volumes[: i + 1])
        if row is None:
            continue
        fwd = (closes[i + horizon] - closes[i]) / closes[i] if closes[i] > 0 else 0.0
        X.append(row)
        y.append(1 if fwd > 0 else 0)
        ts.append(_row_ts(bars[i], float(i)))
        fwd_moves.append(abs(fwd))
    return X, y, ts, fwd_moves


def _build_dataset(bars: list[dict], horizon: int) -> tuple[np.ndarray, np.ndarray, float]:
    """Single-series (X, y, avg_abs_forward_return), already chronological."""
    X, y, _ts, fwd_moves = _build_rows(bars, horizon)
    avg_abs = float(np.mean(fwd_moves)) if fwd_moves else 0.0
    return np.array(X, dtype=float), np.array(y, dtype=int), avg_abs


def _make_model(random_state: int):
    """Single-threaded GradientBoostingClassifier.

    We deliberately avoid HistGradientBoostingClassifier: its OpenMP threadpool
    deadlocks on macOS Python.framework builds. Plain GradientBoostingClassifier
    is what the rest of the codebase (RegimeClassifier) uses reliably.
    """
    from sklearn.ensemble import GradientBoostingClassifier
    return GradientBoostingClassifier(
        n_estimators=150, max_depth=3, learning_rate=0.05,
        subsample=0.8, random_state=random_state,
    )


def _walk_forward_metrics(X: np.ndarray, y: np.ndarray, n_splits: int, random_state: int) -> dict | None:
    """Expanding-window walk-forward: train on the past, score the next block.

    Returns out-of-sample accuracy / AUC accumulated across all test blocks, plus
    the majority-class baseline accuracy.  None if there is not enough data.
    """
    try:
        from sklearn.metrics import roc_auc_score
    except Exception as exc:
        logger.warning("sklearn unavailable for forecaster validation: %s", exc)
        return None

    n = len(y)
    if n < MIN_TRAIN_ROWS:
        return None

    fold = n // (n_splits + 1)
    if fold < 20:
        return None

    oos_true: list[int] = []
    oos_pred: list[int] = []
    oos_proba: list[float] = []

    for k in range(1, n_splits + 1):
        train_end = fold * k
        test_end = min(fold * (k + 1), n)
        if test_end - train_end < 10:
            continue
        X_tr, y_tr = X[:train_end], y[:train_end]
        X_te, y_te = X[train_end:test_end], y[train_end:test_end]
        if len(np.unique(y_tr)) < 2:
            continue
        model = _make_model(random_state)
        model.fit(X_tr, y_tr)
        proba = model.predict_proba(X_te)[:, list(model.classes_).index(1)] if 1 in model.classes_ else np.zeros(len(X_te))
        pred = (proba >= 0.5).astype(int)
        oos_true.extend(y_te.tolist())
        oos_pred.extend(pred.tolist())
        oos_proba.extend(proba.tolist())

    if len(oos_true) < 30 or len(set(oos_true)) < 2:
        return None

    oos_true_arr = np.array(oos_true)
    acc = float((np.array(oos_pred) == oos_true_arr).mean())
    majority = float(max(oos_true_arr.mean(), 1 - oos_true_arr.mean()))
    try:
        auc = float(roc_auc_score(oos_true, oos_proba))
    except Exception:
        auc = 0.5
    # Hanley-McNeil SE of AUC under the no-skill null (AUC = 0.5): how far
    # pure noise wanders from 0.5 at this sample size.
    n1 = int(oos_true_arr.sum())
    n0 = len(oos_true_arr) - n1
    auc_null_se = math.sqrt((n1 + n0 + 1) / (12.0 * n1 * n0))
    return {
        "oos_accuracy": acc,
        "oos_auc": auc,
        "majority_baseline": majority,
        "oos_samples": len(oos_true),
        "auc_null_se": auc_null_se,
    }


class GradientBoostedReturnForecaster:
    """Validated sklearn direction forecaster with an MA-compatible interface."""

    def __init__(self, horizon: int = DEFAULT_HORIZON) -> None:
        self._horizon = horizon
        self._model: Any = None
        self._avg_abs_move: float = 0.0
        self._accepted: bool = False
        self._last_metrics: dict | None = None

    def is_available(self) -> bool:
        return self._accepted and self._model is not None

    @property
    def last_train_metrics(self) -> dict | None:
        return self._last_metrics

    def train(self, bars: list[dict], *, n_splits: int = 5, random_state: int = 42) -> bool:
        """Walk-forward validate on a single symbol's series, then refit. See _fit_validate."""
        X, y, avg_abs = _build_dataset(bars, self._horizon)
        return self._fit_validate(X, y, avg_abs, n_splits=n_splits, random_state=random_state)

    def train_pooled(
        self,
        bars_by_symbol: dict[str, list[dict]],
        *,
        n_splits: int = 5,
        random_state: int = 42,
    ) -> bool:
        """Train ONE general model across many symbols' real history.

        Feature rows are built per symbol (so returns never cross a symbol
        boundary), then pooled and sorted by timestamp so every walk-forward test
        block is strictly later in time than its training data — no lookahead.
        This is the method the real-data pipeline uses.
        """
        all_X: list[list[float]] = []
        all_y: list[int] = []
        all_ts: list[float] = []
        fwd_all: list[float] = []
        for sym, bars in bars_by_symbol.items():
            rows, ys, tss, fwd = _build_rows(bars, self._horizon)
            all_X.extend(rows)
            all_y.extend(ys)
            all_ts.extend(tss)
            fwd_all.extend(fwd)

        if len(all_y) < MIN_TRAIN_ROWS or len(set(all_y)) < 2:
            self._last_metrics = {"rejected": True, "reason": f"insufficient_pooled_rows ({len(all_y)})"}
            self._accepted = False
            return False

        order = np.argsort(np.array(all_ts))
        X = np.array(all_X, dtype=float)[order]
        y = np.array(all_y, dtype=int)[order]
        avg_abs = float(np.mean(fwd_all)) if fwd_all else 0.0
        return self._fit_validate(X, y, avg_abs, n_splits=n_splits, random_state=random_state,
                                  symbols=list(bars_by_symbol.keys()))

    def _fit_validate(
        self, X: np.ndarray, y: np.ndarray, avg_abs: float, *,
        n_splits: int, random_state: int, symbols: list[str] | None = None,
    ) -> bool:
        """Shared walk-forward gate: accept only if OOS metrics beat baseline."""
        if len(y) < MIN_TRAIN_ROWS or len(np.unique(y)) < 2:
            self._last_metrics = {"rejected": True, "reason": f"insufficient_rows ({len(y)})"}
            self._accepted = False
            return False

        metrics = _walk_forward_metrics(X, y, n_splits, random_state)
        if metrics is None:
            self._last_metrics = {"rejected": True, "reason": "walk_forward_unavailable"}
            self._accepted = False
            return False

        auc_threshold = 0.5 + max(MIN_AUC_GAIN, 2.0 * metrics["auc_null_se"])
        beats_auc = metrics["oos_auc"] >= auc_threshold
        beats_acc = metrics["oos_accuracy"] >= metrics["majority_baseline"] + MIN_ACC_GAIN
        accepted = bool(beats_auc and beats_acc)
        metrics.update({"accepted": accepted, "rejected": not accepted,
                        "auc_threshold": auc_threshold,
                        "horizon": self._horizon, "model_id": MODEL_ID,
                        "n_symbols": len(symbols) if symbols else 1})
        self._last_metrics = metrics

        if not accepted:
            self._model = None
            self._accepted = False
            logger.info("ReturnForecaster REJECTED: AUC=%.3f acc=%.3f vs majority=%.3f — no validated edge",
                        metrics["oos_auc"], metrics["oos_accuracy"], metrics["majority_baseline"])
            return False

        # Validated → refit on the full history for live serving.
        model = _make_model(random_state)
        model.fit(X, y)
        self._model = model
        self._avg_abs_move = avg_abs
        self._accepted = True
        logger.info("ReturnForecaster ACCEPTED: OOS AUC=%.3f acc=%.3f (n=%d) — serving validated edge",
                    metrics["oos_auc"], metrics["oos_accuracy"], metrics["oos_samples"])
        return True

    def predict(self, symbol: str, bars: list[dict]) -> ForecastPrediction | None:
        """Return a ForecastPrediction, or None to defer to the MA baseline."""
        if not self.is_available() or len(bars) < LOOKBACK:
            return None
        closes, highs, lows, volumes = _bars_to_arrays(bars)
        row = _feature_row(closes, highs, lows, volumes)
        if row is None:
            return None
        try:
            classes = list(self._model.classes_)
            proba = self._model.predict_proba([row])[0]
            p_up = float(proba[classes.index(1)]) if 1 in classes else 0.5
        except Exception as exc:
            logger.warning("ReturnForecaster.predict failed for %s: %s", symbol, exc)
            return None

        p_up = min(0.95, max(0.05, p_up))
        edge = (p_up - 0.5) * 2.0            # signed conviction in [-1, 1]
        expected_return = edge * self._avg_abs_move
        # Uncertainty: highest at p=0.5, lowest at the extremes.
        uncertainty = min(1.0, max(0.05, 1.0 - abs(edge)))

        return ForecastPrediction(
            symbol=symbol,
            direction_probability=p_up,
            expected_return=expected_return,
            model_id=MODEL_ID,
            confidence=max(0.1, abs(edge)),
            model_uncertainty=uncertainty,
        )

    # ── Persistence (mirrors RegimeClassifier convention) ─────────────────────
    def save(self, path: str) -> None:
        import joblib, json as _json
        if self._model is not None:
            joblib.dump(self._model, f"{path}_sklearn.pkl")
        meta = {
            "accepted": self._accepted,
            "horizon": self._horizon,
            "avg_abs_move": self._avg_abs_move,
            "last_metrics": self._last_metrics,
            "model_id": MODEL_ID,
        }
        with open(f"{path}_meta.json", "w") as f:
            _json.dump(meta, f)

    def load(self, path: str) -> bool:
        import joblib, json as _json
        try:
            with open(f"{path}_meta.json") as f:
                meta = _json.load(f)
            # Only load the pickle when the saved model was actually accepted.
            if meta.get("accepted") and meta.get("model_id") == MODEL_ID:
                self._model = joblib.load(f"{path}_sklearn.pkl")
                self._accepted = True
            self._horizon = meta.get("horizon", self._horizon)
            self._avg_abs_move = meta.get("avg_abs_move", 0.0)
            self._last_metrics = meta.get("last_metrics")
            return self._accepted
        except FileNotFoundError:
            return False
        except Exception:
            logger.warning("ReturnForecaster.load failed", exc_info=True)
            return False
