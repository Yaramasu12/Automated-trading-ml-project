# data_ingestion/yahoo_finance.py
import yfinance as yf
import pandas as pd
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

def fetch_historical_data(symbol, start_date, end_date):
    """Fetches historical data from Yahoo Finance."""
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(
            start=start_date, end=end_date)  # Adjust period as needed.
        if data.empty:
            logger.warning(
                f"No historical data found for {symbol} on Yahoo Finance."
            )
            return None
        return data
    except Exception as e:
        logger.error(f"Error fetching historical data for {symbol}: {e}")
        return None