# ai_models/greeks_calculator.py
import numpy as np
from scipy.stats import norm  # For Black-Scholes
import logging
import datetime
from typing import Union  # to show the types for parameters in the calculation.

logger = logging.getLogger(__name__)

def get_implied_volatility_from_market(symbol: str, strike_price: float, expiry_date: str) -> float:
    """Gets Implied Volatility from the market (Placeholder)."""
    try:
       #Code to fetch the real IV.
       #Replace this with the logic to call the data and calculate the function.
       # Get real time data from your options
       # Get the latest quotes.
       if symbol == "INFY" and strike_price == 1700:
         return 0.20  # Example - Volatility.
       return 0.30  # Example
    except Exception as e:
        logger.error(f"Error calculating implied volatility: {e}")
        return 0.30  # Return 30%.

# Black Scholes Pricing
def black_scholes_call(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Calculates the Black-Scholes price of a European call option."""
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    call_price = S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    return call_price

def black_scholes_delta_call(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Calculates the delta of a European call option."""
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    delta = norm.cdf(d1)
    return delta

def black_scholes_gamma_call(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Calculates the gamma of a European call option."""
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    gamma = norm.pdf(d1) / (S * sigma * np.sqrt(T))
    return gamma

def black_scholes_theta_call(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Calculates the theta of a European call option."""
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    theta = -(S * norm.pdf(d1) * sigma) / (2 * np.sqrt(T)) - r * K * np.exp(-r * T) * norm.cdf(d2)
    return theta/365 #Annualized.

def black_scholes_vega_call(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Calculates the vega of a European call option."""
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    vega = S * norm.pdf(d1) * np.sqrt(T)
    return vega/100 #Divide by
def calculate_greeks(option_quote, spot_price: float, strike_price: float, time_to_expiry: float, option_type: str, risk_free_rate: float=0.05) -> dict:
    """Calculates the Greeks for a *simplified* European option using Black-Scholes."""
    #  Simplified approach.  Requires volatility (sigma).
    try:
        # Time to expiry (in years).  Assuming expiry_date already in days from now.
        T = time_to_expiry #Time to expiry (days /365.0 )
        if T <= 0:  # Handle cases near expiration.
            return {"delta": 0, "gamma": 0, "theta": 0, "vega": 0}

        # Get Implied Volatility (This is a simplified placeholder.
        #  A proper system needs to calculate Implied Volatility.)
        implied_volatility = get_implied_volatility_from_market(option_quote.get("tradingsymbol",""), strike_price,option_quote.get("expiry_date",None))  #Call the new function to get it, instead of the constant.

        # Apply Black-Scholes formula
        if option_type.lower() == "call":
            delta = black_scholes_delta_call(spot_price, strike_price, T, risk_free_rate, implied_volatility)
            gamma = black_scholes_gamma_call(spot_price, strike_price, T, risk_free_rate, implied_volatility)
            theta = black_scholes_theta_call(spot_price, strike_price, T, risk_free_rate, implied_volatility)
            vega = black_scholes_vega_call(spot_price, strike_price, T, risk_free_rate, implied_volatility)
        elif option_type.lower() == "put":
            # Implement put greeks here, but the process is similar (using Black-Scholes put formulas)
            # Example code for put delta (modify other greeks similarly)
            d1 = (np.log(spot_price / strike_price) + (risk_free_rate + 0.5 * implied_volatility ** 2) * T) / (implied_volatility * np.sqrt(T))
            delta = norm.cdf(d1) - 1  #Put Delta
            gamma = black_scholes_gamma_call(spot_price, strike_price, T, risk_free_rate, implied_volatility) #Gamma the same for put/call.
            theta = (-(spot_price * norm.pdf(d1) * implied_volatility) / (2 * np.sqrt(T)) + risk_free_rate * strike_price * np.exp(-risk_free_rate * T) * norm.cdf(-d1))/365
            vega = black_scholes_vega_call(spot_price, strike_price, T, risk_free_rate, implied_volatility)
        else:
            return {"delta": 0, "gamma": 0, "theta": 0, "vega": 0} #Not an option type.
        return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}

    except Exception as e:
        logger.error(f"Error calculating Greeks: {e}")
        return {"delta": 0, "gamma": 0, "theta": 0, "vega": 0}  # Handle errors.