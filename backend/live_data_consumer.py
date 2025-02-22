import json
import logging
from kafka import KafkaConsumer

# Configuration
BROKER = 'localhost:9092'
TOPIC = 'market_live_data'
GROUP_ID = 'live_data_consumers'

# Logging Configuration
logging.basicConfig(
    filename='logs/market_data.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def main():
    try:
        consumer = KafkaConsumer(
            TOPIC,
            bootstrap_servers=[BROKER],
            auto_offset_reset='latest',
            enable_auto_commit=True,
            group_id=GROUP_ID,
            value_deserializer=lambda m: json.loads(m.decode('utf-8'))
        )

        logging.info(f"Connected to Kafka topic: {TOPIC}")

        for message in consumer:
            tick_data = message.value
            logging.info(f"Received Tick Data: {tick_data}")
            process_tick_data(tick_data)

    except Exception as e:
        logging.error(f"Error in Kafka consumer: {e}")
    finally:
        consumer.close()
        logging.info("Kafka consumer connection closed.")

def process_tick_data(tick_data):
    """
    Process and analyze the tick data for strategy application.
    """
    try:
        instrument_token = tick_data[0].get('instrument_token', 'N/A')
        last_price = tick_data[0].get('last_price', 'N/A')
        timestamp = tick_data[0].get('timestamp', 'N/A')

        logging.info(f"Instrument: {instrument_token} | Price: {last_price} | Time: {timestamp}")
        print(f"[{timestamp}] Instrument: {instrument_token} | Price: {last_price}")

        # Placeholder for strategy logic
        # Example: Trigger signal generation, store in DB, etc.

    except Exception as e:
        logging.error(f"Error processing tick data: {e}")

if __name__ == "__main__":
    main()