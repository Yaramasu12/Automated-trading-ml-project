# ai_models/garch_model.py
import arch
import pandas as pd
import numpy as np
import logging
from statsmodels.regression.rolling import RollingOLS #Rolling to handle different market conditions.
import statsmodels.api as sm

logger = logging.getLogger(__name__)

def train_garch_model(data):
    """Trains a GARCH model for volatility forecasting."""
    try:
        # Returns calculation.
        returns = data['Close'].pct_change().dropna()  # Calculate returns
        # Fit GARCH model
        garch_model = arch.arch_model(returns, vol='Garch', p=1, q=1)
        results = garch_model.fit(disp='off')  # Suppress output
        return results
    except Exception as e:
        logger.error(f"GARCH Model training error: {e}")
        return None

def predict_volatility_garch(results, data, horizon=1):
    """Predicts volatility using a trained GARCH model."""
    try:
        # Make volatility forecast
        forecasts = results.forecast(horizon=horizon, reindex=False)
        # Get the conditional volatility for the next period
        predicted_volatility = np.sqrt(forecasts.variance.iloc[-1, 0])
        return predicted_volatility
    except Exception as e:
        logger.error(f"GARCH Volatility prediction error: {e}")
        return 0.02 #return default