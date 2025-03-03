# app/services/historical_data.py
import pandas as pd
import random
import datetime

def fetch_historical_data(symbol, days=30):
    """
    Simulates fetching 30-day historical data for a given symbol.
    Args:
        symbol (str): Stock symbol for which historical data is simulated.
        days (int): Number of days to simulate.

    Returns:
        pd.DataFrame: DataFrame with OHLC and volume data.
    """
    base_price = random.uniform(1000, 4000)
    data = {
        'date': [],
        'open': [],
        'high': [],
        'low': [],
        'close': [],
        'volume': []
    }

    for i in range(days):
        date = datetime.datetime.now() - datetime.timedelta(days=i)
        open_price = base_price + random.uniform(-50, 50)
        high_price = open_price + random.uniform(0, 20)
        low_price = open_price - random.uniform(0, 20)
        close_price = random.uniform(low_price, high_price)
        volume = random.randint(100000, 500000)

        data['date'].append(date.date())
        data['open'].append(round(open_price, 2))
        data['high'].append(round(high_price, 2))
        data['low'].append(round(low_price, 2))
        data['close'].append(round(close_price, 2))
        data['volume'].append(volume)

    df = pd.DataFrame(data)
    df['symbol'] = symbol
    df.sort_values(by='date', ascending=True, inplace=True)
    return df


if __name__ == "__main__":
    # Example run
    symbol = 'NSE:NIFTY50'
    df = fetch_historical_data(symbol)
    print(df.head())
    # Export to CSV (optional)
    df.to_csv(f"{symbol.replace(':', '_')}_historical_data.csv", index=False)
    print(f"[Saved] Historical data saved as {symbol.replace(':', '_')}_historical_data.csv")