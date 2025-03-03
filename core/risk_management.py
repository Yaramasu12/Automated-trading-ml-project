# core/risk_management.py
import logging
from config.settings import MAX_DRAWDOWN
import datetime

logger = logging.getLogger(__name__)

def check_drawdown(portfolio_value, initial_capital, max_drawdown=MAX_DRAWDOWN):
    """Checks if the drawdown exceeds the allowed limit."""
    drawdown = (initial_capital - portfolio_value) / initial_capital
    return drawdown >= max_drawdown  # Use >= for accurate comparison
def insert_risk_management_data(conn, risk_data):
    """Inserts risk management data into the database."""
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO Risk_Management (max_drawdown, current_drawdown, margin_available, timestamp)
            VALUES (%s, %s, %s, NOW());
        """, (risk_data['max_drawdown'], risk_data['current_drawdown'], risk_data['margin_available']))
        conn.commit()
        logger.info(f"PostgreSQL: Risk management data inserted: {risk_data}")
    except Exception as e:
        conn.rollback()
        logger.error(f"PostgreSQL: Risk management data insertion error: {e}")
    finally:
        if 'cursor' in locals():
            cursor.close()
def calculate_options_exposure(portfolio):
    """Calculates delta, gamma, and vega exposure for options."""
    # This is a very simplified example. In a production system, you'd iterate
    # through all options positions, calculate their Greeks, and sum the exposures.
    try:
        total_delta = 0
        total_gamma = 0
        total_vega = 0
        for symbol, position in portfolio.get("positions", {}).items():
           if "CE" in symbol or "PE" in symbol: # Options Symbol
               #For options we would need to calculate greeks using the current price
               # and then we could calculate exposure.  Simplified example:
               greeks = position.get("greeks", {}) #Use greeks.
               delta = greeks.get("delta",0) * position.get("quantity", 0)
               gamma = greeks.get("gamma", 0) * position.get("quantity", 0)
               vega = greeks.get("vega", 0) * position.get("quantity", 0)
               total_delta += delta
               total_gamma += gamma
               total_vega += vega
        return total_delta, total_gamma, total_vega #Return values

    except Exception as e:
        logger.error(f"Error calculating options exposure: {e}")
        return 0, 0, 0