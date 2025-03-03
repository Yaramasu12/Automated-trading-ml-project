# data_ingestion/simulated_market_data.py
import pandas as pd
import numpy as np
import datetime

def generate_simulated_ohlc(symbol, start_date, end_date, initial_price=1000, volatility=0.01):
    """Generates simulated OHLC data."""
    try:
        num_days = (end_date - start_date).days
        dates = pd.date_range(start_date, end_date, freq='D') #Day frequency.
        close_prices = [initial_price] #Start price.
        for _ in range(num_days):
            change = np.random.normal(0, volatility)
            new_price = close_prices[-1] * (1 + change)
            close_prices.append(new_price)

        # Create a DataFrame
        data = pd.DataFrame({
            'Close': close_prices,
            'Open': [price * (1 + np.random.uniform(-0.01, 0.01)) for price in close_prices], #Set the open value.
            'High': [price * (1 + np.random.uniform(0, 0.02)) for price in close_prices],
            'Low': [price * (1 - np.random.uniform(0, 0.02)) for price in close_prices],
            'Volume': np.random.randint(10000, 50000, size=len(close_prices)) #Volume
        }, index=dates)
        data.index.name = 'Date'
        return data
    except Exception as e:
        logger.error(f"Error during simulated market data {e}")
        return None