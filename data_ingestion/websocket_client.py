# data_ingestion/websocket_client.py (Simplified - replace with KiteConnect's)
import websocket
import json
import logging
import threading
import time
from datetime import datetime #Date time
import numpy as np  # Simulate data.
from core.utils import instrument_lookup

logger = logging.getLogger(__name__)

class KiteWebSocket:
    def __init__(self, api_key, access_token, on_ticks, on_connect, on_close, on_error, symbols):
        self.api_key = api_key
        self.access_token = access_token
        self.on_ticks = on_ticks
        self.on_connect = on_connect
        self.on_close = on_close
        self.on_error = on_error
        self.symbols = symbols
        self.running = False
        self.thread = None

    def connect(self):
        self.running = True
        self.thread = threading.Thread(target=self._run)
        self.thread.daemon = True  # Allow program to exit even if thread is running
        self.thread.start()
        if self.on_connect:
            self.on_connect()

    def _run(self):
        # Simulate real-time data
        while self.running:
            try:
                # Simulate tick data for the requested symbols (Equities and Options)
                tick_data = []
                for symbol in self.symbols:
                    price = np.random.uniform(1000, 2000)
                    change = np.random.uniform(-0.01, 0.01)
                    last_price = price * (1 + change)

                    #Create the instrument token
                    tick_data.append({
                       "instrument_token": symbol,
                       "last_trade_time": datetime.now().isoformat(),
                       "last_price": last_price,
                       "high": last_price * (1 + np.random.uniform(0,0.02)),
                       "low": last_price * (1- np.random.uniform(0,0.02)),
                       "open": last_price * (1 - np.random.uniform(0, 0.01)),
                       "close": last_price * (1+ np.random.uniform(-0.01,0.01)),
                       "volume": np.random.randint(10000,50000)

                    })

                if self.on_ticks:
                    self.on_ticks(tick_data) #Data to the consumer.
            except Exception as e:
                logger.error(f"Simulated WebSocket Error: {e}")
                if self.on_error:
                    self.on_error(e)
            time.sleep(1)  # Simulate data updates every 1 second

    def close(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        if self.on_close:
            self.on_close()