"""IntradayReturnForecaster — a *real*, validated intraday direction model.

This is the production sibling of `GradientBoostedReturnForecaster` (daily bars),
built for the untested avenue flagged in the project's honesty log: genuinely
INTRADAY microstructure features on 5-minute bars. It follows the identical
earn-deployment discipline as the daily model and `ai.models.RegimeClassifier`:

  * Features at bar i use ONLY bars [day_open .. i]; the label is the sign of the
    forward `horizon`-bar return, and is only formed when i and i+horizon are the
    SAME trading day (no overnight leakage).
  * `train_pooled()` pools rows across symbols, sorts globally by time, and runs
    expanding-window **walk-forward** validation. The model is ACCEPTED only when
    its out-of-sample ROC-AUC clears 0.5 + max(0.02, 2·SE_null) AND its accuracy
    beats the majority-class baseline. On a driftless random walk it correctly
    FAILS — the live system never gets a fake "intraday neural" edge.
  * `predict()` returns a `ForecastPrediction` only when a validated model is
    loaded AND it is given enough same-day intraday bars; otherwise it returns
    None so the caller falls back to the moving-average baseline. Feeding it daily
    bars therefore yields None, not a garbage signal.

The feature set mirrors `scripts/research_intraday_edge.py` so the research
verdict and the production model agree bar-for-bar.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any

import numpy as np

from trading_platform.neural.schemas import ForecastPrediction

logger = logging.getLogger(__name__)

# Forward horizon (5-min bars) the direction label is computed over. 3 bars = 15
# minutes — the default the research script sweeps around. The training script can
# override and keep whatever horizon validates.
DEFAULT_HORIZON = 3
# Minimum intraday bars of same-day history before a feature row can form. Matches
# the research script (features start at i=12, need >=13 bars that day).
MIN_INTRADAY_HISTORY = 13
# Acceptance margins (out-of-sample), identical philosophy to the daily model:
# the effective AUC bar is 0.5 + max(MIN_AUC_GAIN, 2 * SE_null).
MIN_AUC_GAIN = 0.02
MIN_ACC_GAIN = 0.01
MIN_TRAIN_ROWS = 400     # intraday pools far more rows than daily; demand a real sample
MODEL_ID = "intraday_gbm_v1"

# Same 15 features as scripts/research_intraday_edge.py, in the same order.
FEATURE_NAMES = [
    "ret_1", "ret_3", "ret_6", "ret_12",      # short-horizon momentum
    "vwap_dev",                                  # deviation from intraday VWAP (reversion)
    "or_pos",                                    # position within the 30-min opening range
    "bar_of_day",                                # time-of-day (0=open .. 1=close)
    "rvol_6", "rvol_ratio",                      # realized vol + short/long vol ratio
    "rel_volume", "vol_mom",                     # volume vs recent avg + volume trend
    "body_ratio",                                # candle body / range
    "dist_hi_12", "dist_lo_12",                  # distance from recent high/low
    "run_len",                                    # signed consecutive up/down bar count
]


# ── Bar parsing ────────────────────────────────────────────────────────────────

def _bar_dt(bar: dict) -> datetime | None:
    """Datetime for a bar, from a 'ts' or 'timestamp' field (datetime or ISO str)."""
    raw = bar.get("ts", bar.get("timestamp"))
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


def _group_by_day(bars: list[dict]) -> list[tuple[Any, list[dict]]]:
    """Group bars by calendar (trading) day, each day's bars sorted by time.

    Returns [(date, day_bars), ...] in chronological day order. Bars without a
    parseable timestamp are dropped — an intraday model must know bar times.
    """
    by_day: dict = {}
    for b in bars:
        dt = _bar_dt(b)
        if dt is None:
            continue
        by_day.setdefault(dt.date(), []).append((dt, b))
    out: list[tuple[Any, list[dict]]] = []
    for day in sorted(by_day):
        rows = [b for _dt, b in sorted(by_day[day], key=lambda t: t[0])]
        out.append((day, rows))
    return out


def _arrays(day_bars: list[dict]):
    close = np.array([float(b.get("close", 0.0) or 0.0) for b in day_bars], float)
    high = np.array([float(b.get("high", b.get("close", 0.0)) or 0.0) for b in day_bars], float)
    low = np.array([float(b.get("low", b.get("close", 0.0)) or 0.0) for b in day_bars], float)
    openp = np.array([float(b.get("open", b.get("close", 0.0)) or 0.0) for b in day_bars], float)
    vol = np.array([max(float(b.get("volume", 0.0) or 0.0), 0.0) for b in day_bars], float)
    return close, high, low, openp, vol


def _feature_row_at(close, high, low, openp, vol, logret, vwap, or_lo, or_rng, n, i) -> list[float]:
    """Build the 15-feature vector at bar index i (uses only bars [0..i])."""
    def r(k: int) -> float:
        return float(np.log(close[i] / max(close[i - k], 1e-9)))

    rvol6 = float(np.std(logret[i - 5:i + 1]))
    rvol36 = float(np.std(logret[max(0, i - 35):i + 1])) or 1e-9
    recent_vol = vol[i - 11:i + 1]
    avg_vol = float(recent_vol.mean()) or 1e-9
    hi12, lo12 = high[i - 11:i + 1].max(), low[i - 11:i + 1].min()
    # signed run length of consecutive same-direction bars ending at i
    run = 0
    for j in range(i, 0, -1):
        s = np.sign(logret[j])
        if s == 0:
            break
        if run == 0 or np.sign(run) == s:
            run += int(s)
        else:
            break
    return [
        r(1), r(3), r(6), r(12),
        float((close[i] - vwap[i]) / max(close[i], 1e-9)),
        float((close[i] - or_lo) / or_rng),
        float(i / max(n - 1, 1)),
        rvol6, float(rvol6 / rvol36),
        float(vol[i] / avg_vol), float(recent_vol[-3:].mean() / avg_vol),
        float((close[i] - openp[i]) / max(high[i] - low[i], 1e-9)),
        float((close[i] - hi12) / max(close[i], 1e-9)),
        float((close[i] - lo12) / max(close[i], 1e-9)),
        float(run),
    ]


def _day_context(day_bars: list[dict]):
    """Precompute per-day arrays needed by _feature_row_at. Returns None if too short."""
    n = len(day_bars)
    if n < MIN_INTRADAY_HISTORY:
        return None
    close, high, low, openp, vol = _arrays(day_bars)
    logret = np.zeros(n)
    logret[1:] = np.log(close[1:] / np.maximum(close[:-1], 1e-9))
    typical = (high + low + close) / 3.0
    cum_pv = np.cumsum(typical * vol)
    cum_v = np.cumsum(vol)
    vwap = np.where(cum_v > 0, cum_pv / np.maximum(cum_v, 1e-9), close)
    or_hi, or_lo = high[:6].max(), low[:6].min()   # 30-min opening range = first 6 bars
    or_rng = max(or_hi - or_lo, 1e-9)
    return close, high, low, openp, vol, logret, vwap, or_lo, or_rng, n


def _day_features_labels(day_bars: list[dict], horizon: int) -> tuple[list[list[float]], list[int]]:
    """(X rows, y labels) for one trading day. No lookahead, no overnight leak."""
    ctx = _day_context(day_bars)
    if ctx is None:
        return [], []
    close, high, low, openp, vol, logret, vwap, or_lo, or_rng, n = ctx
    X: list[list[float]] = []
    y: list[int] = []
    for i in range(12, n - horizon):
        feats = _feature_row_at(close, high, low, openp, vol, logret, vwap, or_lo, or_rng, n, i)
        fwd = float(np.log(close[i + horizon] / max(close[i], 1e-9)))
        if not np.isfinite(fwd) or not all(np.isfinite(v) for v in feats):
            continue
        X.append(feats)
        y.append(1 if fwd > 0 else 0)
    return X, y


def _make_model(random_state: int):
    """Plain GradientBoostingClassifier — HistGradientBoosting deadlocks (OpenMP)
    on this repo's macOS Python build (see CLAUDE.md landmines)."""
    from sklearn.ensemble import GradientBoostingClassifier
    return GradientBoostingClassifier(
        n_estimators=120, max_depth=3, learning_rate=0.05, subsample=0.8,
        random_state=random_state,
    )


def _walk_forward_metrics(X: np.ndarray, y: np.ndarray, n_splits: int, random_state: int) -> dict | None:
    """Expanding-window walk-forward: train on the past, score the next block."""
    try:
        from sklearn.metrics import roc_auc_score
    except Exception as exc:
        logger.warning("sklearn unavailable for intraday validation: %s", exc)
        return None

    n = len(y)
    if n < MIN_TRAIN_ROWS:
        return None
    fold = n // (n_splits + 1)
    if fold < 40:
        return None

    oos_true: list[int] = []
    oos_pred: list[int] = []
    oos_proba: list[float] = []
    for k in range(1, n_splits + 1):
        train_end = fold * k
        test_end = min(fold * (k + 1), n)
        if test_end - train_end < 20:
            continue
        X_tr, y_tr = X[:train_end], y[:train_end]
        X_te, y_te = X[train_end:test_end], y[train_end:test_end]
        if len(np.unique(y_tr)) < 2 or len(np.unique(y_te)) < 2:
            continue
        model = _make_model(random_state)
        model.fit(X_tr, y_tr)
        classes = list(model.classes_)
        proba = model.predict_proba(X_te)[:, classes.index(1)] if 1 in classes else np.zeros(len(X_te))
        pred = (proba >= 0.5).astype(int)
        oos_true.extend(y_te.tolist())
        oos_pred.extend(pred.tolist())
        oos_proba.extend(proba.tolist())

    if len(oos_true) < 200 or len(set(oos_true)) < 2:
        return None

    yt = np.array(oos_true)
    acc = float((np.array(oos_pred) == yt).mean())
    majority = float(max(yt.mean(), 1 - yt.mean()))
    try:
        auc = float(roc_auc_score(oos_true, oos_proba))
    except Exception:
        auc = 0.5
    n1 = int(yt.sum())
    n0 = len(yt) - n1
    # Hanley-McNeil SE of AUC under the no-skill null.
    auc_null_se = math.sqrt((n1 + n0 + 1) / (12.0 * max(n1, 1) * max(n0, 1)))
    return {
        "oos_accuracy": acc,
        "oos_auc": auc,
        "majority_baseline": majority,
        "oos_samples": len(oos_true),
        "auc_null_se": auc_null_se,
    }


class IntradayReturnForecaster:
    """Validated sklearn intraday direction forecaster with an MA-compatible interface."""

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

    @property
    def horizon(self) -> int:
        return self._horizon

    def train_pooled(
        self,
        bars_by_symbol: dict[str, list[dict]],
        *,
        n_splits: int = 6,
        random_state: int = 42,
    ) -> bool:
        """Train ONE model across many symbols' intraday history.

        Feature rows are built per (symbol, trading-day) so returns never cross a
        symbol OR an overnight boundary, then pooled and sorted by timestamp so
        every walk-forward test block is strictly later in time than its training
        data. Accepts only if OOS metrics beat baseline.
        """
        all_X: list[list[float]] = []
        all_y: list[int] = []
        all_ts: list[float] = []
        fwd_all: list[float] = []
        for _sym, bars in bars_by_symbol.items():
            for day, day_bars in _group_by_day(bars):
                X, y = _day_features_labels(day_bars, self._horizon)
                if not X:
                    continue
                ctx = _day_context(day_bars)
                close = ctx[0]
                # forward-move magnitude for expected_return scaling + a monotone
                # within-day timestamp for the global chronological sort
                base = day.toordinal() * 1_000_000
                for offset, (xi, yi) in enumerate(zip(X, y)):
                    i = 12 + offset
                    fwd = abs(float(np.log(close[i + self._horizon] / max(close[i], 1e-9))))
                    all_X.append(xi)
                    all_y.append(yi)
                    all_ts.append(float(base + i))
                    fwd_all.append(fwd)

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
            logger.info("IntradayForecaster REJECTED: AUC=%.3f acc=%.3f vs majority=%.3f — no validated edge",
                        metrics["oos_auc"], metrics["oos_accuracy"], metrics["majority_baseline"])
            return False

        model = _make_model(random_state)
        model.fit(X, y)
        self._model = model
        self._avg_abs_move = avg_abs
        self._accepted = True
        logger.info("IntradayForecaster ACCEPTED: OOS AUC=%.3f acc=%.3f (n=%d) — serving validated edge",
                    metrics["oos_auc"], metrics["oos_accuracy"], metrics["oos_samples"])
        return True

    def predict(self, symbol: str, bars: list[dict]) -> ForecastPrediction | None:
        """Return a ForecastPrediction, or None to defer to the MA baseline.

        Requires enough SAME-DAY intraday bars ending at the most recent bar.
        Given daily bars (one bar per day) it returns None — it never fabricates
        an intraday signal from the wrong data frequency.
        """
        if not self.is_available() or not bars:
            return None
        days = _group_by_day(bars)
        if not days:
            return None
        _day, day_bars = days[-1]   # most recent trading day only
        ctx = _day_context(day_bars)
        if ctx is None:
            return None
        close, high, low, openp, vol, logret, vwap, or_lo, or_rng, n = ctx
        i = n - 1                   # predict from the latest completed bar
        if i < 12:
            return None
        try:
            row = _feature_row_at(close, high, low, openp, vol, logret, vwap, or_lo, or_rng, n, i)
            if not all(np.isfinite(v) for v in row):
                return None
            classes = list(self._model.classes_)
            proba = self._model.predict_proba([row])[0]
            p_up = float(proba[classes.index(1)]) if 1 in classes else 0.5
        except Exception as exc:
            logger.warning("IntradayForecaster.predict failed for %s: %s", symbol, exc)
            return None

        p_up = min(0.95, max(0.05, p_up))
        edge = (p_up - 0.5) * 2.0
        expected_return = edge * self._avg_abs_move
        uncertainty = min(1.0, max(0.05, 1.0 - abs(edge)))
        return ForecastPrediction(
            symbol=symbol,
            direction_probability=p_up,
            expected_return=expected_return,
            model_id=MODEL_ID,
            confidence=max(0.1, abs(edge)),
            model_uncertainty=uncertainty,
        )

    # ── Persistence (mirrors GradientBoostedReturnForecaster) ─────────────────
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
            logger.warning("IntradayForecaster.load failed", exc_info=True)
            return False
