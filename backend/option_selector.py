import logging
import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.preprocessing import StandardScaler
from utils import setup_logger
from risk_manager import RiskManager

# Logger setup
logger = setup_logger('option_selector_logger', 'logs/market_data.log')

class OptionSelector:
    def __init__(self, risk_manager: RiskManager):
        """
        Initialize the Option Selector with a linked Risk Manager.
        """
        self.risk_manager = risk_manager
        self.scaler = StandardScaler()
        self.model = RandomForestRegressor(n_estimators=100, random_state=42)
        logger.info("OptionSelector initialized with AI/ML model for option prediction.")

    def preprocess_data(self, option_data):
        """
        Preprocess option data for model input.
        :param option_data: Raw option data.
        :return: Processed data suitable for ML prediction.
        """
        try:
            features = np.array([
                option_data['implied_volatility'],
                option_data['open_interest'],
                option_data['change_in_oi'],
                option_data['volume'],
                option_data['last_price']
            ]).reshape(1, -1)
            processed_features = self.scaler.fit_transform(features)
            logger.info(f"Preprocessed data: {processed_features}")
            return processed_features
        except Exception as e:
            logger.error(f"Error in preprocessing data: {e}")
            return None

    def predict_option_score(self, processed_data):
        """
        Predict the profitability score of an option using the ML model.
        :param processed_data: Preprocessed data.
        :return: Predicted score indicating option attractiveness.
        """
        try:
            score = self.model.predict(processed_data)[0]
            logger.info(f"Predicted option score: {score}")
            return score
        except Exception as e:
            logger.error(f"Prediction error: {e}")
            return -1

    def select_best_option(self, option_chain):
        """
        Select the best option from the provided option chain.
        :param option_chain: List of available options with details.
        :return: Best option based on predicted scores.
        """
        best_option = None
        best_score = -np.inf

        for option in option_chain:
            processed_data = self.preprocess_data(option)
            if processed_data is not None:
                score = self.predict_option_score(processed_data)
                if score > best_score:
                    best_score = score
                    best_option = option

        if best_option:
            logger.info(f"Selected best option: {best_option}")
        else:
            logger.warning("No suitable option found in the chain.")
        return best_option

    def execute_trade(self, option, stop_loss_distance):
        """
        Execute the trade for the selected option using risk parameters.
        :param option: Selected option contract details.
        :param stop_loss_distance: Stop-loss distance for the trade.
        """
        position_size = self.risk_manager.calculate_position_size(stop_loss_distance)
        if position_size > 0:
            logger.info(f"Executing trade: {option['symbol']} | Qty: {position_size}")
            print(f"✅ Executing trade for {option['symbol']} with quantity {position_size}.")
        else:
            logger.warning("Position size is zero. Trade not executed.")

if __name__ == "__main__":
    # Example usage
    risk_manager = RiskManager(max_drawdown=0.1, risk_per_trade=0.02)
    option_selector = OptionSelector(risk_manager=risk_manager)

    # Simulated option chain data
    option_chain = [
        {'symbol': 'NIFTY22FEB17600CE', 'implied_volatility': 0.2, 'open_interest': 15000, 'change_in_oi': 500, 'volume': 2000, 'last_price': 120},
        {'symbol': 'BANKNIFTY22FEB41500PE', 'implied_volatility': 0.25, 'open_interest': 18000, 'change_in_oi': 300, 'volume': 2500, 'last_price': 150}
    ]

    best_option = option_selector.select_best_option(option_chain)
    if best_option:
        option_selector.execute_trade(best_option, stop_loss_distance=50)