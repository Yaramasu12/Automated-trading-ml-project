# core/trading_engine.py
import logging
from config.settings import EXCHANGE
from core.regulatory_compliance import validate_order, apply_risk_rules #Import regulations.
from core.order_management import place_order, cancel_order, modify_order
logger = logging.getLogger(__name__)

def execute_trade(kite, symbol, quantity, trade_type, price=None, order_type="MARKET", stoploss_price=None, greeks=None): #Add stoploss price.
    """Executes an equity trade."""
    try:
        if not kite:
            logger.error("Kite API not initialized.")
            return False

        # 1. Order Validation.
        if not validate_order(symbol, quantity, trade_type, price, order_type):
            logger.error("Order validation failed, not implementing the trade.")
            return False

        # 2. Apply Risk Management Rules.
        if not apply_risk_rules(symbol, quantity, trade_type, price, order_type, greeks=greeks):
            logger.error("Risk Management rules blocked this order.")
            return False
        # 3. Build parameters.
        if order_type.upper() == "MARKET":
            order_params = {
                "tradingsymbol": symbol,
                "exchange": EXCHANGE,
                "transaction_type": kite.TRANSACTION_TYPE_BUY if trade_type == "BUY" else kite.TRANSACTION_TYPE_SELL,
                "quantity": quantity,
                "order_type": kite.ORDER_TYPE_MARKET,
                "product": kite.PRODUCT_MIS,
                "variety": kite.VARIETY_REGULAR,
            }
        elif order_type.upper() == "LIMIT" and price is not None:
            order_params = {
                "tradingsymbol": symbol,
                "exchange": EXCHANGE,
                "transaction_type": kite.TRANSACTION_TYPE_BUY if trade_type == "BUY" else kite.TRANSACTION_TYPE_SELL,
                "quantity": quantity,
                "order_type": kite.ORDER_TYPE_LIMIT,
                "price": price,
                "product": kite.PRODUCT_MIS,
                "variety": kite.VARIETY_REGULAR,
            }
        # Stoploss order, code.
        elif order_type.upper() == "STOP" and stoploss_price is not None:
            order_params = { #Stop Loss
                "tradingsymbol": symbol,
                "exchange": EXCHANGE,
                "transaction_type": kite.TRANSACTION_TYPE_BUY if trade_type == "BUY" else kite.TRANSACTION_TYPE_SELL,
                "quantity": quantity,
                "order_type": kite.ORDER_TYPE_SL,
                "product": kite.PRODUCT_MIS,
                "variety": kite.VARIETY_REGULAR,
                "trigger_price": stoploss_price, #The stop loss.
            }
        else:
            logger.error("Invalid Order Type, will not implement the trade.")
            return False
        # Place the order
        # Place the order
        if not place_order(kite,symbol,quantity,trade_type,price,order_type,stoploss_price):
          logger.error(f"Order could not be placed.")
          return False

        # The order was placed, return to the caller the trade worked.
        logger.info(f"Order placed: {symbol} - {trade_type} {quantity} at {price or 'market price'}")
        return True
    except Exception as e:
        logger.error(f"Order placement failed: {e}")
        return False

def execute_options_trade(kite, symbol, strike_price, expiry_date, option_type, quantity, trade_type, price=None, order_type="MARKET", greeks=None):  # Add Greeks
    """Executes an options trade."""
    try:
        if not kite:
            logger.error("Kite API not initialized.")
            return False
        #Validate.
        if trade_type.upper() not in ("BUY", "SELL"):
            logger.error(f"Invalid trade type: {trade_type}")
            return False

        # Construct the trading symbol for options (Example)
        expiry_str = expiry_date.strftime("%y%m%d")
        option_symbol = f"{symbol}{expiry_str}{strike_price}{option_type.upper()}" #Create the symbol

        # Set the parameters.
        if order_type.upper() == "MARKET":
            order_params = {
                "tradingsymbol": option_symbol,
                "exchange": "NFO",  # National Stock Options
                "transaction_type": kite.TRANSACTION_TYPE_BUY if trade_type == "BUY" else kite.TRANSACTION_TYPE_SELL,
                "quantity": quantity,
                "order_type": kite.ORDER_TYPE_MARKET,
                "product": kite.PRODUCT_MIS,
                "variety": kite.VARIETY_REGULAR,
            }
        elif order_type.upper() == "LIMIT" and price is not None:
            order_params = {
                "tradingsymbol": option_symbol,
                "exchange": "NFO",  # National Stock Options
                "transaction_type": kite.TRANSACTION_TYPE_BUY if trade_type == "BUY" else kite.TRANSACTION_TYPE_SELL,
                "quantity": quantity,
                "order_type": kite.ORDER_TYPE_LIMIT,
                "price": price,
                "product": kite.PRODUCT_MIS,
                "variety": kite.VARIETY_REGULAR,
            }
        else:
            logger.error(f"Invalid order parameters for trade, order skipped.")
            return False
        order_id = kite.place_order(**order_params)
        logger.info(f"Options order placed: {order_id} - {trade_type} {quantity} of {option_symbol} at {price or 'market price'}")
        return True
    except Exception as e:
        logger.error(f"Options order placement failed: {e}")
        return False