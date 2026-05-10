from __future__ import annotations

"""Cross-asset correlation graph model — rolling correlation baseline."""

from trading_platform.neural.schemas import CorrelationRiskPrediction


class RollingCorrelationGraph:
    """Computes pairwise rolling correlations as a proxy for contagion risk."""

    def __init__(self, window: int = 30) -> None:
        self.window = window

    def predict(
        self,
        symbols: list[str],
        returns_map: dict[str, list[float]],
    ) -> CorrelationRiskPrediction:
        if len(symbols) < 2:
            return CorrelationRiskPrediction(
                symbols=symbols,
                max_pairwise_correlation=0.0,
                contagion_risk_score=0.0,
            )

        correlations: list[float] = []
        sym_list = list(symbols)
        for i in range(len(sym_list)):
            for j in range(i + 1, len(sym_list)):
                a = returns_map.get(sym_list[i], [])
                b = returns_map.get(sym_list[j], [])
                n = min(len(a), len(b), self.window)
                if n < 3:
                    continue
                ra = a[-n:]
                rb = b[-n:]
                corr = _pearson(ra, rb)
                correlations.append(abs(corr))

        if not correlations:
            return CorrelationRiskPrediction(
                symbols=symbols,
                max_pairwise_correlation=0.0,
                contagion_risk_score=0.0,
            )

        max_corr = max(correlations)
        mean_corr = sum(correlations) / len(correlations)
        contagion = min(1.0, (max_corr * 0.6 + mean_corr * 0.4))

        return CorrelationRiskPrediction(
            symbols=symbols,
            max_pairwise_correlation=max_corr,
            contagion_risk_score=contagion,
            model_id="rolling_corr_30",
        )


def _pearson(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n < 2:
        return 0.0
    ma = sum(a) / n
    mb = sum(b) / n
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    sa = (sum((v - ma) ** 2 for v in a)) ** 0.5
    sb = (sum((v - mb) ** 2 for v in b)) ** 0.5
    denom = sa * sb
    return cov / denom if denom > 1e-10 else 0.0


class GNNCorrelationModel:
    """Optional GNN model — lazy import; graceful fallback."""

    def is_available(self) -> bool:
        try:
            import torch  # noqa: F401
            return True
        except ImportError:
            return False

    def predict(self, symbols: list[str], returns_map: dict[str, list[float]]) -> CorrelationRiskPrediction | None:
        if not self.is_available():
            return None
        return None  # Placeholder
