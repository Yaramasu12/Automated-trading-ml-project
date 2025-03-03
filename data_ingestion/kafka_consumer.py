# data_ingestion/kafka_consumer.py
from kafka import KafkaConsumer
import json
import logging
from config.settings import KAFKA_BOOTSTRAP_SERVERS

logger = logging.getLogger(__name__)

def create_kafka_consumer(topic):
    try:
        consumer = KafkaConsumer(
            topic,
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            auto_offset_reset='latest',  # or 'earliest'
            enable_auto_commit=True,
            group_id='trading_group',  # Consumer group ID
            value_deserializer=lambda x: json.loads(x.decode('utf-8'))
        )
        logger.info(f"Kafka Consumer created for topic: {topic}")
        return consumer
    except Exception as e:
        logger.error(f"Kafka Consumer creation error: {e}")
        return None