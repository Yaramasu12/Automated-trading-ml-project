# app/services/live_data_ingestion.py
import time
import random
import json

def fetch_live_ticks(symbols, tick_count=50):
    """
    Simulates Zerodha Kite API tick-by-tick data.
    Args:
        symbols (list): List of stock symbols to simulate.
        tick_count (int): Number of ticks to generate per symbol.

    Returns:
        list: List of tick data dictionaries in JSON format.
    """
    data = []
    for _ in range(tick_count):
        for symbol in symbols:
            tick = {
                'symbol': symbol,
                'price': round(random.uniform(1000, 4000), 2),
                'volume': random.randint(100, 1000),
                'timestamp': time.time()
            }
            data.append(tick)
            print(f"[Live Tick] {json.dumps(tick)}")  # Real-time tick print
            time.sleep(0.05)  # Simulate tick frequency
    return data


if __name__ == "__main__":
    # Example run
    symbols = ['NSE:NIFTY50', 'NSE:BANKNIFTY', 'BSE:SENSEX', 'NSE:FINNIFTY', 'NSE:MIDCAPNIFTY']
    ticks = fetch_live_ticks(symbols, tick_count=10)