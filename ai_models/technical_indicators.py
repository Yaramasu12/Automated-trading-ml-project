# ai_models/technical_indicators.py
import pandas as pd
import numpy as np
import talib # Install talib
import logging

logger = logging.getLogger(__name__)

def calculate_indicators(data):
    """Calculates technical indicators (Moving Averages, RSI, MACD) using TA-Lib."""
    try:
        if not isinstance(data, pd.DataFrame) or data.empty:
            logger.warning("Input data is not a valid DataFrame or is empty.")
            return None

        # Ensure data has 'high', 'low', 'close', and 'volume' columns.
        if not all(col in data.columns for col in ['high', 'low', 'close', 'volume']):
            logger.warning("DataFrame is missing required columns (high, low, close, volume).")
            return None
        # Moving Averages
        data['SMA_20'] = talib.SMA(data['close'], timeperiod=20)
        data['SMA_50'] = talib.SMA(data['close'], timeperiod=50)
        data['SMA_200'] = talib.SMA(data['close'], timeperiod=200)

        # Relative Strength Index (RSI)
        data['RSI'] = talib.RSI(data['close'], timeperiod=14)
        # Moving Average Convergence Divergence (MACD)
        macd, macdsignal, macdhist = talib.MACD(data['close'], fastperiod=12, slowperiod=26, signalperiod=9)
        data['MACD'] = macd
        data['MACD_Signal'] = macdsignal
        data['MACD_Hist'] = macdhist
        return data
    except Exception as e:
        logger.error(f"Error calculating technical indicators: {e}")
        return None