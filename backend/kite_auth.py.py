import json
import logging
from kiteconnect import KiteConnect

# Configure logging
logging.basicConfig(
    filename='logs/market_data.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)


class KiteAuth:
    def __init__(self, api_key, api_secret):
        self.api_key = api_key
        self.api_secret = api_secret
        self.kite = KiteConnect(api_key=self.api_key)
        self.access_token_file = 'secrets/access_token.json'

    def generate_access_token(self, request_token):
        try:
            data = self.kite.generate_session(request_token, api_secret=self.api_secret)
            access_token = data['access_token']

            # Save access token
            with open(self.access_token_file, 'w') as f:
                json.dump({'access_token': access_token}, f)

            self.kite.set_access_token(access_token)
            logging.info("Access token generated and saved successfully.")
            return access_token
        except Exception as e:
            logging.error(f"Failed to generate access token: {e}")
            raise

    def get_access_token(self):
        try:
            with open(self.access_token_file, 'r') as f:
                token_data = json.load(f)
                access_token = token_data['access_token']
                self.kite.set_access_token(access_token)
                logging.info("Access token loaded successfully.")
                return access_token
        except FileNotFoundError:
            logging.error("Access token file not found. Generate token first.")
            raise
        except Exception as e:
            logging.error(f"Error loading access token: {e}")
            raise


if __name__ == "__main__":
    try:
        # These should be loaded from a secure source in real use
        API_KEY = "your_api_key_here"
        API_SECRET = "your_api_secret_here"

        kite_auth = KiteAuth(API_KEY, API_SECRET)
        print("Login URL:", kite_auth.kite.login_url())
        request_token = input("Enter the request token from the URL: ")
        kite_auth.generate_access_token(request_token)
    except Exception as e:
        logging.error(f"Authentication process failed: {e}")
