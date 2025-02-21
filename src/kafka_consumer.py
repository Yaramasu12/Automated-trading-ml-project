from kafka import KafkaConsumer
import json

consumer = KafkaConsumer(
    'nse_bse_ticks',
    bootstrap_servers=['<EC2_IP>:9092'],
    auto_offset_reset='earliest',
    value_deserializer=lambda x: json.loads(x.decode('utf-8'))
)

for message in consumer:
    print(f"Received: {message.value}")