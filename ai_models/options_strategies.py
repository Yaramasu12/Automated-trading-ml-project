# ai_models/options_strategies.py
import logging
import datetime
from core.utils import instrument_lookup
from data_ingestion.kite_api import get_option_chain
from ai_models.technical_indicators import calculate_indicators  # Get indicators.
from ai_models.greeks_calculator import calculate_greeks  # Import greek calculator.

logger = logging.getLogger(__name__)

def get_atm_strikes(kite, underlying_symbol, expiry_date, strike_price_range=100):
    """Helper function to get ATM (At The Money) strike prices."""
    try:
        # Get Options Chain
        option_chain = get_option_chain(
            kite, underlying_symbol, expiry_date, strike_price_range
        )
        if option_chain is None:
            logger.warning(f"Could not retrieve options chain.")
            return None, None

        # Get the spot price, ATM.
        quotes = kite.quote([underlying_symbol])
        if quotes is None or underlying_symbol not in quotes:
            logger.warning(f"Cannot retrieve current quote.")
            return None, None
        spot_price = quotes[underlying_symbol]["last_price"]
        # Calculate the strike price.
        atm_strike = round(spot_price / 100, 0) * 100  # Round to closest 100.
        return int(atm_strike), option_chain #Return values

    except Exception as e:
        logger.error(f"Error getting ATM Strike price {e}")
        return None, None

def covered_call(kite, underlying_symbol, expiry_date, quantity, risk_tolerance=0.05):
    """Implements a covered call strategy."""
    # Requires an existing long position in underlying
    # Consider risk based on underlying vol and position size.
    try:
        # Get At The Money Strike Price.
        atm_strike, option_chain = get_atm_strikes(
            kite, underlying_symbol, expiry_date
        )  # Get strikes and chain

        if atm_strike is None or option_chain is None:
            logger.warning(
                "Could not get ATM strikes, cannot implement Covered Call"
            )
            return None, None, None

        call_symbol = f"{underlying_symbol}{expiry_date.strftime('%y%m%d')}{atm_strike}CE"  # Construct the symbol
        if call_symbol not in option_chain:
            logger.warning(
                f"Symbol does not exist in option chain: {call_symbol}"
            )
            return None, None, None

        # Get the necessary data.
        option_quote = option_chain.get(
            call_symbol, {}
        )  # Get the quote object.
        if not option_quote:  # if there isn't data, skip.
            logger.warning(f"No quote available for symbol: {call_symbol}")
            return None, None, None

        # Get the premium.
        premium = option_quote.get("last_price", None)
        if premium is None:
            logger.warning(f"Could not get premium for symbol: {call_symbol}")
            return None, None, None
        # Now Calculate the Greeks
        greeks = calculate_greeks(
            option_quote,  # Pass the option quote.
            spot_price=kite.quote([underlying_symbol])[underlying_symbol]["last_price"],
            strike_price=atm_strike,
            time_to_expiry=(
                expiry_date - datetime.date.today()
            ).days/365,  # Time to expiry (years).
            option_type="call",  # option type
        )  # Calculate greeks.
        return call_symbol, premium, greeks  # return the data.
    except Exception as e:
        logger.error(f"Error implementing covered call: {e}")
        return None, None, None