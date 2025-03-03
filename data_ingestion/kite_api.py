# data_ingestion/kite_api.py
import logging
from kiteconnect import KiteConnect
from config.settings import KITE_API_KEY, KITE_API_SECRET, REDIS_URL, KITE_REQUEST_TOKEN  # Import Settings
import redis
from datetime import date, timedelta # Import Time.

logger = logging.getLogger(__name__)

redis_client = redis.from_url(REDIS_URL)

def get_kite_connection():
    """Handles Kite API authentication and connection."""
    try:
        kite = KiteConnect(api_key=KITE_API_KEY)
        # Check for access token first
        access_token = redis_client.get('kite_access_token')
        if access_token:
            kite.set_access_token(access_token.decode('utf-8'))
            logger.info("Kite API: Access token retrieved from Redis.")
            return kite

        # If no token or token expired, get request token.
        # (This part requires manual authentication via web browser).
        if not REDIRECT_URI:
            raise ValueError(
                "REDIRECT_URI not set in environment variables.")
        if not KITE_REQUEST_TOKEN:
            raise ValueError("KITE_REQUEST_TOKEN is not set in environment variables.")

        data = kite.generate_session(
            KITE_REQUEST_TOKEN, api_secret=KITE_API_SECRET)
        kite.set_access_token(data["access_token"])
        redis_client.set(
            'kite_access_token', data["access_token"])  # Store in redis
        logger.info(
            "Kite API: New access token generated and stored in Redis.")
        return kite
    except Exception as e:
        logger.error(f"Kite API Initialization Error: {e}")
        return None

def get_quote(kite, instruments):
    """Fetches real-time quotes for a list of instruments."""
    try:
        quotes = kite.quote(instruments)
        return quotes
    except Exception as e:
        logger.error(f"Error fetching quotes: {e}")
        return None

def get_option_chain(kite, underlying_symbol, expiry_date, strike_price_range=100):
    """Fetches option chain data (simplified - uses hardcoded NFO exchange)."""
    try:
        # Construct the instrument list for the option chain
        expiry_str = expiry_date.strftime("%y%m%d")
        instruments = []
        for strike in range(strike_price_range * -1, strike_price_range + 1, 100):  # Get strikes.
            strike_price = round(strike + strike_price_range, 2)
            # Format the symbol. This formatting will vary based on the Exchange
            call_symbol = f"{underlying_symbol}{expiry_str}{strike_price}CE"
            put_symbol = f"{underlying_symbol}{expiry_str}{strike_price}PE"
            instruments.append(call_symbol)  # Call option.
            instruments.append(put_symbol)  # Put Option

        # Fetch quotes (assuming you have instruments)
        quotes = kite.quote(instruments)  # Get the quotes.

        # Extract and return the options chain (Simplified, incomplete)
        option_chain = {}
        for symbol, quote in quotes.items():
            if quote:
                option_chain[symbol] = quote  # Store the whole quote object
        return option_chain
    except Exception as e:
        logger.error(
            f"Error fetching options chain for {underlying_symbol}: {e}")
        return None