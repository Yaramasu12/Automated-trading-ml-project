# core/api.py
from fastapi import FastAPI, HTTPException
from core.trading_engine import execute_trade, execute_options_trade  # Correct imports
import logging
from config.settings import KITE_API_KEY, KITE_API_SECRET, EXCHANGE
from data_ingestion.kite_api import get_kite_connection
from data_ingestion.paper_trading import PaperTradingEngine #For backend.
import datetime #Datetime imports.
from redis import Redis #For redis values

logger = logging.getLogger(__name__)
app = FastAPI()
# Initialize the connection
kite = get_kite_connection() #Get the Kite connection.
#Add in the trading engine.
paper_trading_engine = PaperTradingEngine() #set engine

@app.post("/trade")
async def place_trade(
    symbol: str,
    quantity: int,
    trade_type: str,
    order_type: str = "MARKET",
    price: float = None,
):
    """Places an equity trade order."""
    try:
        if not kite:
            raise HTTPException(
                status_code=500, detail="Kite API not initialized")

        if trade_type.upper() not in ("BUY", "SELL"):
            raise HTTPException(
                status_code=400, detail="Invalid trade type. Use BUY or SELL"
            )
        #Validate it is correct.
        if order_type.upper() not in ("MARKET", "LIMIT"):
           raise HTTPException(
               status_code=400, detail="Invalid order type. Use MARKET or LIMIT"
           )

        if order_type.upper() == "LIMIT" and price is None:
            raise HTTPException(
                status_code=400, detail="Price must be specified for limit orders."
            )

        if order_type.upper() == "MARKET" and price is not None:
            logger.warning(
                "Price specified for market order, this will be ignored by the Kite API."
            )

        if not execute_trade(
            kite, symbol, quantity, trade_type.upper(), price, order_type.upper()
        ):  # Call execution
            raise HTTPException(status_code=500, detail="Order placement failed.")

        return {"message": "Trade placed successfully", "order_placed": True}
    except HTTPException as e:
        raise e  # Re-raise HTTP exceptions
    except Exception as e:
        logger.error(f"Trade API error: {e}")
        raise HTTPException(
            status_code=500, detail=f"An unexpected error occurred: {e}")

@app.post("/options/trade")
async def place_options_trade(
    symbol: str,
    strike_price: float,
    expiry_date: str,  # YYYY-MM-DD
    option_type: str,  # "CE" or "PE"
    quantity: int,
    trade_type: str,
    order_type: str = "MARKET",
    price: float = None,
):
    """Places an options trade order."""
    try:
        if not kite:
            raise HTTPException(
                status_code=500, detail="Kite API not initialized"
            )
        if trade_type.upper() not in ("BUY", "SELL"):
            raise HTTPException(
                status_code=400, detail="Invalid trade type. Use BUY or SELL"
            )
        if option_type.upper() not in ("CE", "PE"):
            raise HTTPException(
                status_code=400, detail="Invalid option type. Use CE or PE"
            )
        if order_type.upper() not in ("MARKET", "LIMIT"):
           raise HTTPException(
               status_code=400, detail="Invalid order type. Use MARKET or LIMIT"
           )

        if order_type.upper() == "LIMIT" and price is None:
            raise HTTPException(
                status_code=400, detail="Price must be specified for limit orders"
            )
        if order_type.upper() == "MARKET" and price is not None:
            logger.warning(
                "Price specified for market order, this will be ignored by the Kite API"
            )

        # Convert expiry date string to datetime
        try:
            expiry_date_dt = datetime.datetime.strptime(
                expiry_date, "%Y-%m-%d"
            ).date()
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Invalid expiry date format. Use YYYY-MM-DD"
            )

        if not execute_options_trade(
            kite,
            symbol,
            strike_price,
            expiry_date_dt,
            option_type.upper(),
            quantity,
            trade_type.upper(),
            price,
            order_type.upper(),
        ):
            raise HTTPException(status_code=500, detail="Options order placement failed")

        return {"message": "Options trade placed successfully", "order_placed": True}
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Options trade API error: {e}")
        raise HTTPException(
            status_code=500, detail=f"An unexpected error occurred: {e}")

@app.get("/health")
async def health_check():
    """Health Check"""
    return {"status": "OK"}

@app.get("/portfolio")
async def get_portfolio():
    """Placeholder to return portfolio data."""
    try:
        #Return Portfolio from the Engine.  The Engine should be in the API code as the single source of truth.
        portfolio =  paper_trading_engine.portfolio #Call this from Engine.
        return {
           "cash":portfolio.get("cash", INITIAL_CAPITAL),
           "positions": portfolio.get("positions",{})
        }
    except Exception as e:
       logger.error(f"Failed to get the portfolio: {e}")
       return {"error":"Could not load portfolio."}