import json
import os

# Configuration file paths
SECRETS_FILE = 'secrets/secrets.json'
ACCESS_TOKEN_FILE = 'secrets/access_token.json'

def load_secrets():
    """
    Load API credentials securely from secrets.json.
    """
    try:
        with open(SECRETS_FILE, 'r') as file:
            return json.load(file)
    except FileNotFoundError:
        raise FileNotFoundError(f"Secrets file not found at {SECRETS_FILE}")
    except Exception as e:
        raise Exception(f"Error loading secrets: {e}")

# Load API credentials
secrets = load_secrets()
API_KEY = secrets.get('api_key')
API_SECRET = secrets.get('api_secret')

# Kafka configurations
KAFKA_BROKER = os.getenv('KAFKA_BROKER', 'localhost:9092')
KAFKA_TOPIC = os.getenv('KAFKA_TOPIC', 'market_live_data')
KAFKA_GROUP_ID = os.getenv('KAFKA_GROUP_ID', 'live_data_consumers')

# Logging configurations
LOG_FILE = 'logs/market_data.log'
LOG_LEVEL = 'INFO'

# WebSocket instruments (Example: NIFTY & BANKNIFTY)
INSTRUMENTS = [256265, 260105]

# Access token storage
ACCESS_TOKEN_PATH = ACCESS_TOKEN_FILE