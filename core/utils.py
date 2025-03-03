# core/utils.py
import logging
from config.settings import EXCHANGE
from data_ingestion.kite_api import get_option_chain #Import the get options chain

logger = logging.getLogger(__name__)

def instrument_lookup(instrument_token):
    """Helper function to map an instrument token to its trading symbol (simplified)."""
    #This needs to be implemented in a real system using a DB lookup or some kind of mapping
    #Make sure it matches the data.
    if instrument_token == "INFY":
        return f"INFY-{EXCHANGE}"  # Replace with dynamic lookup from DB
    if instrument_token == "TCS":
        return f"TCS-{EXCHANGE}"
    if instrument_token == "RELIANCE":
        return f"RELIANCE-{EXCHANGE}"
    #For options.
    return instrument_token  # Example: BANKNIFTY24092719300CE

def assess_option_liquidity(kite, option_symbol, expiry_date):
  """Assesses the liquidity of an option (simplified)."""
  try:
      option_chain = get_option_chain(kite, option_symbol.split(
          symbol)[0], expiry_date)  # get options chain.

      if option_chain is None:
          logger.warning("Could not get option chain, cannot assess.")
          return None, None, None

      #Get Option Quote.
      option_quote = option_chain.get(option_symbol,{}) #Get Quote
      if not option_quote:
          logger.warning(f"Quote not available for symbol: {option_symbol}")
          return None, None, None
      # Use last price to check for bid/ask spread.  In a real system. this needs to be more robust.
      bid_price = option_quote.get("depth",{}).get("buy",[]).get("price", None) #Get Bid.
      ask_price = option_quote.get("depth",{}).get("sell",[]).get("price", None) #Get Ask Price.

      if bid_price is not None and ask_price is not None: # check for data, should return a spread.
        spread = ask_price - bid_price #Spread.
      else:
        spread = None
      #Get Open Interest from the Quote:
      open_interest = option_quote.get("oi",None)  #Get open interest.
      volume = option_quote.get("volume",None) #Get Volume.

      return spread, open_interest, volume
  except Exception as e:
      logger.error(f"Error assessing liquidity for {option_symbol}: {e}")
      return None, None, None