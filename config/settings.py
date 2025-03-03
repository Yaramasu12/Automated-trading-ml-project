# config/settings.py
import os
from dotenv import load_dotenv  # For local development.

load_dotenv()

# Kite API Configuration
KITE_API_KEY = os.environ.get("KITE_API_KEY")
KITE_API_SECRET = os.environ.get("KITE_API_SECRET")
REDIRECT_URI = os.environ.get("REDIRECT_URI")  # For authentication
KITE_REQUEST_TOKEN = os.environ.get("KITE_REQUEST_TOKEN")  # ONLY for testing.

# Redis Configuration
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# Kafka Configuration
KAFKA_BOOTSTRAP_SERVERS = os.environ.get(
    "KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"
)
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "ticks") #Use ticks.

# News API Configuration
NEWS_API_KEY = os.environ.get("NEWS_API_KEY")

# Database Configuration
POSTGRES_DB = os.environ.get("POSTGRES_DB", "trading")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "password")
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5432")
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")

# General Trading Configuration
EXCHANGE = "NSE"  # Or BSE
INITIAL_CAPITAL = 1000000  # 10 Lakhs
MAX_DRAWDOWN = 0.10  # 10% Drawdown
MLFLOW_TRACKING_URI = os.environ.get(
    "MLFLOW_TRACKING_URI", "http://localhost:5000"
)
MLFLOW_EXPERIMENT_NAME = os.environ.get(
    "MLFLOW_EXPERIMENT_NAME", "trading_experiment"
)

# Alerting (Email)
ALERTS_EMAIL_SENDER = os.environ.get("ALERTS_EMAIL_SENDER") #From Address for alerts.
ALERTS_EMAIL_RECIPIENT = os.environ.get("ALERTS_EMAIL_RECIPIENT") #Alert recipient address.
AWS_REGION = os.environ.get("AWS_REGION")  # If using AWS for alerts
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")