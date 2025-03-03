# core/order_management.py
import logging
from config.settings import EXCHANGE #import the value.
from kiteconnect import KiteConnect  #For using the kite connect object to modify/cancel orders.

logger = logging.getLogger(__name__)

def place_order(kite, symbol, quantity, trade_type, price=None, order_type="MARKET", stoploss_price=None): #Function, includes the arguments.
    """Handles order placement.  This centralizes order logic.

    Args:
        kite: kite connect object.
        symbol: The symbol to trade.
        quantity: number of shares.
        trade_type: buy/sell
        price: set the price for the order.
        order_type: Market, limit.
    """
    try:
        if not kite:
            logger.error("Kite API is not initialised, skipping order.")
            return None #Or raise exception.

        #Basic Checks.
        if quantity <= 0:
           logger.error("Quantity is invalid, skipping order.")
           return None
        if trade_type.upper() not in ("BUY", "SELL"):
          logger.error("Invalid trade type, skipping order.")
          return None
        #Construct order parameters
        order_params = {
            "tradingsymbol":symbol,
            "exchange": EXCHANGE,
            "transaction_type": kite.TRANSACTION_TYPE_BUY if trade_type.upper() == "BUY" else kite.TRANSACTION_TYPE_SELL,
            "quantity": quantity,
            "product": kite.PRODUCT_MIS, #Intraday.
            "variety": kite.VARIETY_REGULAR
        }

        #Set the order type
        if order_type.upper() == "MARKET":
            order_params["order_type"] = kite.ORDER_TYPE_MARKET
        elif order_type.upper() == "LIMIT" and price is not None: #Limit Order.
            order_params["order_type"] = kite.ORDER_TYPE_LIMIT
            order_params["price"] = price
        elif order_type.upper() == "STOP" and stoploss_price:
            #Use Stop Loss
            order_params["order_type"] = kite.ORDER_TYPE_SL
            order_params["trigger_price"] = stoploss_price #Set price
        else:
          logger.error(f"Invalid order type {order_type}")
          return None #Return None, invalid type

        #Place the order
        order_id = kite.place_order(**order_params)
        logger.info(f"Order Placed {order_id} for {trade_type} {quantity} of {symbol} at {price or 'Market Price'}")
        return order_id # return OrderID

    except Exception as e:
        logger.error(f"Error placing the order for {symbol}: {e}")
        return None

def cancel_order(kite:KiteConnect, order_id: str, variety=kite.VARIETY_REGULAR) -> bool:
  """Cancels an open order"""
  try:
    if not kite:
      logger.error("Kite not initilised, cannot cancel order.")
      return False
    kite.cancel_order(order_id=order_id, variety = variety)
    logger.info(f"Cancelled Order {order_id}")
    return True

  except Exception as e:
    logger.error(f"Error cancelling the order: {e}")
    return False
#Add functions to handle the logic of modification.
def modify_order(kite:KiteConnect, order_id:str, quantity:int=None, price:float=None,trigger_price:float=None,order_type=None, variety=kite.VARIETY_REGULAR) -> bool:
  """Modifies the order."""
  try:
    if not kite:
      logger.error("Kite not initalised, cannot modify order.")
      return False
    #Set the modify parameters:
    order_modify_params = {
       "order_id": order_id, #The order id.
       "variety": variety
    }
    #Set Price parameters:
    if quantity is not None:
        order_modify_params["quantity"] = quantity #New Quantity
    if price is not None:
        order_modify_params["price"] = price
    if trigger_price is not None:
       order_modify_params["trigger_price"] = trigger_price
    if order_type is not None:
      order_modify_params["order_type"] = order_type
    #Modify the order
    kite.modify_order(**order_modify_params)
    logger.info(f"Modified order: {order_id}")
    return True
  except Exception as e:
      logger.error(f"Error modifying the order:{e}")
      return False