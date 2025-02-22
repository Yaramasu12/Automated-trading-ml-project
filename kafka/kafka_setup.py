from kafka.admin import KafkaAdminClient, NewTopic
import logging

# Configuration
BROKER = 'localhost:9092'
TOPICS = ['market_live_data']
REPLICATION_FACTOR = 1
PARTITIONS = 3

# Logging Configuration
logging.basicConfig(
    filename='logs/market_data.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def create_kafka_topics():
    try:
        admin_client = KafkaAdminClient(
            bootstrap_servers=[BROKER],
            client_id='kafka_topic_creator'
        )

        topic_list = []
        for topic in TOPICS:
            topic_list.append(NewTopic(
                name=topic,
                num_partitions=PARTITIONS,
                replication_factor=REPLICATION_FACTOR
            ))

        admin_client.create_topics(new_topics=topic_list, validate_only=False)
        logging.info(f"Kafka topics created successfully: {TOPICS}")

    except Exception as e:
        logging.error(f"Failed to create Kafka topics: {e}")
    finally:
        admin_client.close()
        logging.info("Kafka admin client connection closed.")

if __name__ == "__main__":
    create_kafka_topics()