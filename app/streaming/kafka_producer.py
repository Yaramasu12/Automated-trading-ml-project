import json
import logging
from datetime import time

from app.services.live_data_ingestion import fetch_live_ticks
from kafka import KafkaProducer
from kiteconnect import KiteTicker
from kite_auth import KiteAuth

# Configuration
API_KEY = "your_api_key_here"
API_SECRET = "your_api_secret_here"
BROKER = 'localhost:9092'
TOPIC = 'market_live_data'
ACCESS_TOKEN_FILE = 'secrets/access_token.json'

# Logging Configuration
logging.basicConfig(
    filename='logs/market_data.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def get_access_token():
    try:
        with open(ACCESS_TOKEN_FILE, 'r') as file:
            data = json.load(file)
            return data['access_token']
    except Exception as e:
        logging.error(f"Error reading access token: {e}")
        raise

def start_kafka_producer(symbols, topic='market_data', bootstrap_servers='localhost:9092'):
    """
    Starts a Kafka producer that streams tick-by-tick data in real-time.
    Args:
        symbols (list): List of stock symbols to simulate.
        topic (str): Kafka topic to publish the data.
        bootstrap_servers (str): Kafka server address.
    """
    producer = KafkaProducer(
        bootstrap_servers=bootstrap_servers,
        value_serializer=lambda v: json.dumps(v).encode('utf-8')
    )

    print(f"[Kafka Producer] Started producing to topic '{topic}'...")
    ticks = fetch_live_ticks(symbols, tick_count=20)

    for tick in ticks:
        producer.send(topic, tick)
        print(f"[Kafka Producer] Sent: {tick}")
        time.sleep(0.05)  # Simulate real-time stream

    producer.flush()
    print("[Kafka Producer] Finished streaming data.")


def main():
    access_token = get_access_token()
    producer = KafkaProducer(
        bootstrap_servers=[BROKER],
        value_serializer=lambda v: json.dumps(v).encode('utf-8')
    )
    kite_ticker = KiteTicker(API_KEY, access_token)

    def on_ticks(ws, ticks):
        logging.info(f"Received tick: {ticks}")
        producer.send(TOPIC, ticks)
        producer.flush()

    def on_connect(ws, response):
        logging.info("WebSocket connected. Subscribing to instruments...")
        ws.subscribe([256265, 260105])  # Example: NIFTY & BANKNIFTY

    def on_close(ws, code, reason):
        logging.warning(f"WebSocket closed: {reason}")

    def on_error(ws, code, reason):
        logging.error(f"WebSocket error: {reason}")

    kite_ticker.on_ticks = on_ticks
    kite_ticker.on_connect = on_connect
    kite_ticker.on_close = on_close
    kite_ticker.on_error = on_error

    logging.info("Starting live data producer...")
    kite_ticker.connect(threaded=True)

    while True:
        try:
            pass  # Keeps the main thread running
        except KeyboardInterrupt:
            kite_ticker.close()
            producer.close()
            logging.info("Live data producer stopped by user.")
            break
        except Exception as e:
            logging.error(f"Unexpected error: {e}")
            kite_ticker.close()
            producer.close()
            break

if __name__ == "__main__":
    main()