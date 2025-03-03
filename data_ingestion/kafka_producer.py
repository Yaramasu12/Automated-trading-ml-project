# data_ingestion/kafka_producer.py
from kafka import KafkaProducer
import json
import logging
from config.settings import KAFKA_BOOTSTRAP_SERVERS  # (Define this in settings.py)

logger = logging.getLogger(__name__)

def create_kafka_producer():
    try:
        producer = KafkaProducer(
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            value_serializer=lambda v: json.dumps(v).encode('utf-8')
        )
        logger.info("Kafka Producer created.")
        return producer
    except Exception as e:
        logger.error(f"Kafka Producer creation error: {e}")
        return None

def send_to_kafka(producer, topic, message):
    try:
        producer.send(topic, message)
        producer.flush()  # Ensure message is sent
        logger.debug(f"Sent to Kafka topic {topic}: {message}")
    except Exception as e:
        logger.error(f"Kafka send error: {e}")