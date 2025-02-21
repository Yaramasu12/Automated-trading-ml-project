from kafka import KafkaProducer
import json

producer = KafkaProducer(
    bootstrap_servers=['<EC2_IP>:9092'],
    value_serializer=lambda v: json.dumps(v).encode('utf-8')
)

def publish_market_data(data):
    producer.send('nse_bse_ticks', data)
    producer.flush()
    print(f"Published: {data}")