import logging
import json
from datetime import datetime

# Logging configuration
def setup_logger(name, log_file, level=logging.INFO):
    """
    Setup a logger for the application.
    """
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    handler = logging.FileHandler(log_file)
    handler.setFormatter(formatter)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.addHandler(handler)

    return logger

# Load JSON data safely
def load_json(file_path):
    """
    Load JSON data from a file with error handling.
    """
    try:
        with open(file_path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"File not found: {file_path}")
    except json.JSONDecodeError as e:
        raise Exception(f"JSON decode error in {file_path}: {e}")
    except Exception as e:
        raise Exception(f"Error reading {file_path}: {e}")

# Save JSON data safely
def save_json(file_path, data):
    """
    Save JSON data to a file with error handling.
    """
    try:
        with open(file_path, 'w') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        raise Exception(f"Error saving JSON to {file_path}: {e}")

# Timestamp utility
def get_current_timestamp():
    """
    Return the current timestamp in ISO format.
    """
    return datetime.now().isoformat()

# Error handler
def handle_exception(e, logger):
    """
    Handle exceptions and log the error.
    """
    logger.error(f"Exception occurred: {e}")
    print(f"[ERROR] {e}")

# Example logger usage
logger = setup_logger('market_data_logger', 'logs/market_data.log')

if __name__ == "__main__":
    # Example usage of utility functions
    logger.info("Utils module loaded successfully.")
    print(f"Current Timestamp: {get_current_timestamp()}")