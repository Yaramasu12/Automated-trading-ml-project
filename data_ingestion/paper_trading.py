# data_ingestion/paper_trading.py
import pandas as pd
import logging
import datetime
from core.risk_management import check_drawdown, calculate_options_exposure  # Import it.
from core.utils import instrument_lookup
from config.settings import INITIAL_CAPITAL, MAX_DRAWDOWN, EXCHANGE

logger = logging.getLogger(__name__)

class PaperTradingEngine:
    def __init__(self, initial_capital=INITIAL_CAPITAL):
        self.portfolio = {
            "cash": initial_capital,
            "positions": {},  # {symbol: {"quantity": x, "avg_price": y, "greeks":{}}} # Add Greeks
            "trades": [],  # History
        }
        self.historical_data = {}  # Set this.
        self.max_drawdown = 0
        self.initial_capital = initial_capital
        self.current_drawdown = 0

    def load_historical_data(self, symbol, data):
        """Loads historical data into the engine"""
        self.historical_data[symbol] = data

    def get_current_price(self, symbol):
        """Gets current price from historical data (simulated)."""
        if symbol in self.historical_data: #Check.
            return self.historical_data[symbol]["Close"].iloc[-1]
        else:
            return None #Return Null.
    def calculate_portfolio_value(self):
        """Calculates current portfolio value (including options)."""
        try:
            portfolio_value = self.portfolio["cash"]  # Start
            for symbol, position in self.portfolio["positions"].items():
                current_price = self.get_current_price(symbol)  # Get current price
                if current_price is not None: # Check.
                    portfolio_value += position["quantity"] * current_price  # Value of Position
                #For Options, apply delta based pricing to portfolio valuation.
                if "CE" in symbol or "PE" in symbol:
                    delta = position.get("greeks",{}).get("delta", 0) #Set value.
                    if delta !=0: # Check.
                        portfolio_value += position["quantity"] * delta * current_price  #Use delta price.

            return portfolio_value  # Return total value.
        except Exception as e:
            logger.error(f"Could not calculate the portfolio value. {e}")
            return 0

    def check_risk(self):
        """Checks the risk, current_drawdown, etc."""
        portfolio_value = self.calculate_portfolio_value()
        self.current_drawdown = (1 - (portfolio_value / self.initial_capital))  # Calculate the value.
        if self.current_drawdown > self.max_drawdown:
            self.max_drawdown = self.current_drawdown # Set the drawdown.
        return check_drawdown(portfolio_value, self.initial_capital, MAX_DRAWDOWN)  # Use risk, return value.

    def execute_trade(self, symbol, quantity, trade_type, price=None, order_type="MARKET", greeks=None):  # Include Greeks
        """Executes a paper trade."""
        try:
            symbol = instrument_lookup(symbol)  # Translate the symbol

            # Get current price
            current_price = price if order_type.upper() == "LIMIT" and price is not None else self.get_current_price(symbol)
            if current_price is None:
                logger.warning(f"Cannot get current price for {symbol}. Trade failed")
                return False

            trade_value = quantity * current_price
            if trade_type.upper() == "BUY": #If its buy order
                if self.portfolio["cash"] >= trade_value: #Check sufficient cash
                   #If the buy is valid add it to the order.
                   if symbol in self.portfolio["positions"]: #Check if there is an existing
                       self.portfolio["positions"][symbol]["quantity"] += quantity #Update with price
                       #Recalculate average.
                       existing_value = self.portfolio["positions"][symbol]["quantity"] #
                       existing_cost = self.portfolio["positions"][symbol]["avg_price"] #Check and recalculate average cost.
                       new_avg_price = ((existing_cost * (existing_value - quantity)) + (current_price * quantity)) / existing_value #recalculate
                       self.portfolio["positions"][symbol]["avg_price"] = new_avg_price  #New price, replace.
                       #Add the greeks.
                       if greeks: #For the greeks.
                         self.portfolio["positions"][symbol]["greeks"] = greeks
                   else:
                      self.portfolio["positions"][symbol] = {
                           "quantity":quantity,
                           "avg_price": current_price,
                           "greeks": greeks
                      } #Add the greeks value.

                   self.portfolio["cash"] -= trade_value  #Adjust Cash
                   self.portfolio["trades"].append(
                       {
                           "symbol": symbol,
                           "quantity": quantity,
                           "price": current_price,
                           "trade_type": trade_type.upper(),
                           "timestamp": datetime.datetime.now(),
                           "order_type": order_type.upper(),
                           "greeks": greeks #The Greeks.
                       }
                   ) #Add.

                   logger.info(f"Paper Trade Executed - BUY {quantity} of {symbol} at {current_price}")
                   return True  # Success.
                else:
                    logger.warning(f"Insufficient funds for BUY trade of {symbol}")
                    return False  # Insufficient Funds
            elif trade_type.upper() == "SELL": # Sell Order
                if symbol in self.portfolio["positions"] and self.portfolio["positions"][symbol]["quantity"] >= quantity:
                    self.portfolio["positions"][symbol]["quantity"] -= quantity
                    self.portfolio["cash"] += trade_value # Update cash balance
                    if self.portfolio["positions"][symbol]["quantity"] == 0:
                        del self.portfolio["positions"][symbol]  # Remove the position if it closes
                    self.portfolio["trades"].append(
                       {
                           "symbol": symbol,
                           "quantity": quantity,
                           "price": current_price,
                           "trade_type": trade_type.upper(),
                           "timestamp": datetime.datetime.now(),
                           "order_type": order_type.upper(),
                           "greeks": greeks
                       }
                    )
                    logger.info(
                        f"Paper Trade Executed - SELL {quantity} of {symbol} at {current_price}"
                    )
                    return True  # Success
                else:
                    logger.warning(
                        f"Insufficient position for SELL trade of {symbol}")
                    return False  # Insufficient Position
            else:
                logger.warning(f"Invalid trade type: {trade_type}")
                return False
        except Exception as e:
            logger.error(f"Error executing paper trade: {e}")
            return False

    def get_position(self, symbol):
       """Gets the position of the trade"""
       return self.portfolio.get("positions",{}).get(symbol,{}) #Get the position