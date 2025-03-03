# main.py (Complete - main logic)
import uvicorn  # For running FastAPI
import gunicorn.app.base  # For Gunicorn, will need pip install gunicorn
from core.api import app  # Import the FastAPI app
import logging
import time
import datetime
import json # for parsing json
import threading #For parallel threads.
from data_ingestion.kite_api import get_kite_connection,get_option_chain
from data_ingestion.yahoo_finance import fetch_historical_data
from data_ingestion.kafka_producer import create_kafka_producer, send_to_kafka
from data_ingestion.kafka_consumer import create_kafka_consumer
from data_ingestion.websocket_client import KiteWebSocket
from core.utils import instrument_lookup, assess_option_liquidity #For instruments and to assess the liquidity
from config.settings import (
    KAFKA_TOPIC,
    INITIAL_CAPITAL,
    MLFLOW_TRACKING_URI,
    MLFLOW_EXPERIMENT_NAME,
    ALERTS_EMAIL_SENDER, #Get email senders.
    ALERTS_EMAIL_RECIPIENT, #Get the alerts from above
    AWS_REGION,
    AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY
) # Get settings.
from ai_models.model_manager import (
    log_model_mlflow,
    load_model_mlflow,
)  # Load Models
from ai_models.lstm_model import (
    create_lstm_model,
    train_lstm_model,
    predict_lstm_price,
)  # Load Model.
from ai_models.garch_model import (
    train_garch_model,
    predict_volatility_garch,
)  # Garch
from ai_models.bert_sentiment import (
    load_sentiment_analysis_model,
    analyze_sentiment,
)
from ai_models.technical_indicators import calculate_indicators # For Indicators.
from ai_models.options_strategies import covered_call
from core.risk_management import check_drawdown, insert_risk_management_data, calculate_options_exposure #For risk
from data_ingestion.paper_trading import PaperTradingEngine
from core.trading_engine import execute_trade, execute_options_trade  # Trading Engine.
from core.alerts import send_email_alert, generate_cloudwatch_alert  # The alert.
from redis import Redis  # For redis values
    #from core.risk_management import  check_drawdown #Use the risk module, check the value of drawndow
    import logging
    import smtplib  # or use an SMS gateway, Slack, etc.
    from email.mime.text import MIMEText  # for emails.
    from config.settings import (
        AWS_REGION,
        AWS_ACCESS_KEY_ID,
        AWS_SECRET_ACCESS_KEY,
        ALERTS_EMAIL_RECIPIENT,
        ALERTS_EMAIL_SENDER
    )
    logger = logging.getLogger(__name__)

    def send_email_alert(subject, body):
        """Sends an email alert (placeholder - use a real SMTP server or AWS SES)."""
        try:
            if not ALERTS_EMAIL_RECIPIENT or not ALERTS_EMAIL_SENDER:
                logger.warning("Email alert configuration missing, skipping email.")
                return

            msg = MIMEText(body)
            msg['Subject'] = subject
            msg['From'] = ALERTS_EMAIL_SENDER
            msg['To'] = ALERTS_EMAIL_RECIPIENT

            # Replace with your SMTP server details
            with smtplib.SMTP('localhost', 25) as server: # Replace with your server.
                server.send_message(msg)
                logger.info(f"Email alert sent: {subject}")
        except Exception as e:
            logger.error(f"Email alert failed: {e}")

    def generate_cloudwatch_alert(metric_name, metric_value,  dimensions=None):
        """Generates a CloudWatch alert (Conceptual).
        In real-world production, this would integrate with AWS CloudWatch or a
        similar monitoring system. This is a stub.
        """
        try:
            alert_message = f"Alert: {metric_name} is at {metric_value}"
            logger.warning(alert_message) #Use log to monitor.

            #Send the alert.
            send_email_alert("Trading Alert", alert_message) #Example.
        except Exception as e:
            logger.error(f"Error during cloudwatch alert: {e}")
    # Configure logging (as before)
    logging.basicConfig(
        level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger(__name__)

    # --- Global variables for models (loaded at startup) ---
    lstm_model = None
    lstm_scaler = None
    garch_model_results = None
    bert_model = None  # Load Sentiment Analysis model.

    # --- Initialize Paper Trading Engine
    paper_trading_engine = PaperTradingEngine(
        initial_capital=INITIAL_CAPITAL
    )  # Use initial capital.

    #Initialize.
    redis_client = None # Redis for trade log
    q = None
    postgres_conn = None #Postgres connection.
    mongo_db = None #MongoDB

    # --- Model Loading Function ---
    def load_models():
        """Loads pre-trained models from MLflow (or trains them if not found)."""
        global lstm_model, lstm_scaler, garch_model_results, bert_model
        try:
            # Load LSTM Model.
            lstm_model, lstm_scaler, lstm_run_id = load_model_mlflow(
                "lstm_price_predictor", model_type="lstm"
            )
            if lstm_model is None:
                logger.info(
                    "LSTM model not found in MLflow. Training a new one..."
                )
                # Train a new model.  Load a long time.
                today = datetime.date.today()
                historical_data = fetch_historical_data(
                    "INFY",
                    today - datetime.timedelta(days=365),
                    today,
                )  # Use a year's worth of data for initial training.
                if historical_data is not None:
                    input_shape = (60, 1)  # Sequence length, number of features
                    lstm_model = create_lstm_model(input_shape)
                    lstm_scaler = train_lstm_model(
                        lstm_model, historical_data, epochs=5
                    )
                    if lstm_model and lstm_scaler:
                        log_model_mlflow(
                            lstm_model,
                            "lstm_price_predictor",
                            scaler=lstm_scaler,
                        )  # Log it after training
                    else:
                        logger.error(
                            "Failed to create and train the LSTM Model."
                        )
                else:
                    logger.error(
                        "Could not fetch initial historical data for training"
                    )
            else:
                logger.info("LSTM model loaded from MLflow.")

            # Load GARCH Model
            # (GARCH training is often done offline, but example provided)
            # GARCH model training is more involved, needs historical returns
            today = datetime.date.today()
            historical_data = fetch_historical_data(
                "INFY", today - datetime.timedelta(days=365), today
            )  # Use a year's worth of data.
            if historical_data is not None:
                garch_model_results = train_garch_model(historical_data)
                if garch_model_results is not None:
                    logger.info("GARCH model (re)trained successfully.")
                else:
                    logger.warning("Could not train GARCH model.")
            else:
                logger.warning(
                    "Could not fetch historical data for GARCH training."
                )

            # Load BERT Sentiment Analysis Model
            bert_model = load_sentiment_analysis_model()
            if bert_model:
                logger.info("BERT sentiment analysis model loaded.")
            else:
                logger.warning("Failed to load BERT sentiment analysis model.")
        except Exception as e:
            logger.exception(f"Error loading models: {e}")

# --- Tick Processing (Modified for Paper Trading) ---
def process_tick_data(
    kite, redis_client, postgres_conn, mongo_db, tick_data, paper_trading_engine
):  # Add paper_trading_engine
    """Processes tick data and makes decisions."""
    try:
        if not tick_data:
            return
        for tick in tick_data:
            symbol = instrument_lookup(tick.get("instrument_token")) # instrument.
            if not symbol:
                continue
            # 1. Fetch Historical Data (For Predictions)
            today = datetime.date.today()
            historical_data = fetch_historical_data(symbol, today - datetime.timedelta(days=30), today)  # Fetch recent data for prediction.
            if not isinstance(historical_data, pd.DataFrame):
                logger.warning(f"No historical data available for {symbol}")
                continue
            # 1a. Calculate Indicators (Before Predictions)
            historical_data_with_indicators = calculate_indicators(historical_data.copy())  # Use a copy.
            if historical_data_with_indicators is None:
               logger.warning(f"Could not calculate indicators for {symbol}")
               continue
            # 2. AI/ML Model Predictions (Use historical_data_with_indicators now)
            predicted_price = None
            volatility = 0.0
            sentiment_score = 0

            # Run LSTM, GARCH, Sentiments
            current_price = tick.get("last_price") # Set.
            # Use a time based function.
            if lstm_model and lstm_scaler and current_price:
                predicted_price = predict_lstm_price(
                    lstm_model,
                    historical_data_with_indicators,  # Use with indicators
                    lstm_scaler,
                )
            if garch_model_results:
                volatility = predict_volatility_garch(
                    garch_model_results, historical_data_with_indicators
                )  # use the same.

            # 3. News Sentiment Analysis (Unchanged)
            if bert_model:
                news_data = get_news_sentiment(
                    symbol, num_articles=3
                )  # Get the news data
                if news_data:  # Make sure we have data
                    sentiment_analysis_result = analyze_sentiment(
                        bert_model,
                        f"{news_data.get('source','')} {news_data.get('keywords','')} {news_data.get('title','')}",
                    )
                    sentiment_score = sentiment_analysis_result.get(
                        "score", 0
                    )  # Use score.

            # 4. Trading Strategy (Example - Covered Call) - Expanded
            if (current_price is not None and volatility > 0):
                # Basic covered call logic.
                #You would add more strategies.  This has to be more complicated to make money.
                if "INFY" in symbol:  # Just an example.  Implement more here.
                    long_position = paper_trading_engine.portfolio["positions"].get(
                        symbol, {}
                    ).get(
                        "quantity", 0
                    )  # Get the quantity from the portfolio, for paper trading.
                    if long_position >= 100:  # Covered Call Requirement.
                        #Get the dates:
                        expiry_date = today + datetime.timedelta(days=30)  # Set expiry date
                        covered_call_symbol, premium, greeks = covered_call(
                            None, symbol, expiry_date, long_position
                        )  # Get the covered call.
                        if covered_call_symbol and premium is not None and greeks: #Check that data is available.
                            # Check the liquidity, apply all rules.
                            spread, open_interest, volume = assess_option_liquidity(None, covered_call_symbol, expiry_date) #We already load it here, since we have historical
                            if spread is not None and open_interest is not None:
                              logger.info(f"Liquidity check on {covered_call_symbol}, Spread: {spread}, Open Interest: {open_interest}, Volume: {volume}") #Get and check it.
                              # Implement all the requirements here to make a covered call.
                              # The paper trading engine.
                              if not paper_trading_engine.execute_trade(
                                  covered_call_symbol,
                                  1,  # set the amount.  This has to be set correctly.
                                  "SELL",
                                  premium,
                                  "LIMIT", #Set Price
                                  greeks = greeks, #Set Greeks
                               ):
                                  logger.warning("Covered call trade failed to execute.")
                              else:
                                logger.info("Covered call sold")
                            # If you do not have covered call, do this.
                                # If you want to test or change the trading signal, you must put this to create it.

                else:
                    logger.info(
                        f"Symbol: {symbol}, Current Price: {current_price}, Volatility: {volatility}, Sentiment: {sentiment_score}"
                    )
                    # Apply Equity trading
                    # 5. Risk Management.
                    delta_exposure, gamma_exposure, vega_exposure = calculate_options_exposure(paper_trading_engine.portfolio) #Check options.
                    # Limit it here.

                # 5. Risk Management
                #Calculate Exposure to measure
                # You would implement the risk.

                portfolio_value = paper_trading_engine.calculate_portfolio_value() # Get portfolio value.
                # Check the risk.
                drawdown = paper_trading_engine.check_risk()  # Check the risk.
                if drawdown:
                   logger.warning(
                       f"Drawdown limit reached. Halting trading. Current Drawdown: {paper_trading_engine.current_drawdown:.4f}, max_drawdown: {paper_trading_engine.max_drawdown}"
                   )
                   #Implement alerts.
                   generate_cloudwatch_alert(
                       "Drawdown Breach",
                       f"Portfolio Drawdown: {paper_trading_engine.current_drawdown:.4f}",
                   )
                insert_risk_management_data(
                    postgres_conn,
                    {
                        "max_drawdown": paper_trading_engine.max_drawdown,
                        "current_drawdown": paper_trading_engine.current_drawdown,
                        "margin_available": paper_trading_engine.portfolio[
                            'cash'
                        ],  # Margin is the cash we have.
                    },
                )
                # 6.  Log News Sentiment in MongoDB (Optional)
                if news_data:
                    insert_news_sentiment(mongo_db, news_data)
            else:
                logger.debug(
                    f"Insufficient data for trading decision for {symbol}."
                )
    except Exception as e:
        logger.error(f"Error processing tick data: {e}")

# --- Background Tasks ---
def risk_check_task(
    kite, redis_client, postgres_conn, paper_trading_engine
):  # Pass Engine.
    """Background task to perform periodic risk checks."""
    try:
        while True:
            # 1. Fetch Portfolio Value (Placeholder)
            portfolio_value = paper_trading_engine.calculate_portfolio_value() # Get portfolio value

            # 2.  Drawdown Check
            drawdown = paper_trading_engine.check_risk()
            if drawdown:
                logger.warning(
                    f"Risk Check: Drawdown Limit Reached.  Current Drawdown: {paper_trading_engine.current_drawdown:.4f}"
                )
                # Send an alert.
                generate_cloudwatch_alert(
                    "Risk - Drawdown Breach",
                    f"Current Drawdown: {paper_trading_engine.current_drawdown:.4f}",
                )
            else:
                logger.info(
                    f"Risk Check: Portfolio OK. Current Drawdown: {drawdown:.4f}"
                )

            # 3.  Record Risk Management Data in PostgreSQL
            insert_risk_management_data(
                postgres_conn,
                {
                    "max_drawdown": paper_trading_engine.max_drawdown,
                    "current_drawdown": paper_trading_engine.current_drawdown,
                    "margin_available": paper_trading_engine.portfolio["cash"],
                },
            )
            time.sleep(60)  # Check every 60 seconds
    except Exception as e:
        logger.error(f"Risk Check Task Error: {e}")

# --- Main Execution ---
def run_fastapi():
    """Runs the FastAPI application using uvicorn."""
    uvicorn.run(app, host="0.0.0.0", port=8000)  # Or configure with environment variables

# --- Gunicorn Setup (For production deployments)
class StandaloneApplication(gunicorn.app.base.BaseApplication):
    def __init__(self, app, options=None):
        self.options = options or {}
        self.app = app
        super().__init__()

    def load_config(self):
        config = {
            key: value for key, value in self.options.items()
            if key in self.cfg.settings and value is not None
        }
        for key, value in config.items():
            self.cfg.set(key.lower(), value)

    def load(self):
        return self.app

def main():
    """Main function to initialize and run the trading system."""
    try:
        # 1. Initialize
        global redis_client, postgres_conn, mongo_db, q #Set the variables.
        # 1. Initialize connections
        redis_client = redis.from_url(REDIS_URL) #Set this
        q = Queue(connection=redis_client) #Set queue.
        postgres_conn = connect_to_postgres()  # Set the value to postgres.
        mongo_db = connect_to_mongodb()  # Set to mongo.
        if not postgres_conn or not mongo_db:
            logger.error("Failed to connect to databases. Exiting.")
            return  # Exit.

        create_postgres_tables(postgres_conn) # Create the tables.

        # 2. Load AI/ML Models (or Train)
        load_models()

        # 3. Kafka Consumer/Websocket.
        #   3a. KITE
        # 4. WebSocket Setup (Simulated)
        symbols = ["INFY", "TCS", "RELIANCE"]  # Example symbols
        instrument_tokens = symbols #Set the symbols.
        # Set up the Kite Websocket, here.
        def on_ticks(ws, tick_data): #Get the data for the ticker, set the ticker.
            try:
                process_tick_data(
                    None,  # No Kite Connection
                    None,  # No Redis Connection.
                    postgres_conn,
                    mongo_db,
                    tick_data,
                    paper_trading_engine,  # The engine.
                )
            except Exception as e:
                logger.error(f"Error processing websocket ticks: {e}")

        def on_connect(ws):
            logger.info("WebSocket connected")

        def on_close(ws):
            logger.info("WebSocket closed")

        def on_error(ws, error):
            logger.error(f"WebSocket error: {error}")

        #Set this.
        ws = KiteWebSocket(
            api_key="",
            access_token="",
            symbols=symbols,
            on_ticks=on_ticks,
            on_connect=on_connect,
            on_close=on_close,
            on_error=on_error,
        )
        ws.connect()

        # Start background task.
        risk_thread = threading.Thread(target=risk_check_task, args=(None,redis_client,postgres_conn, paper_trading_engine,), daemon=True)  # Set the engine.
        risk_thread.start() #Start

        # Start the API, side thread.
        api_thread = threading.Thread(target=run_fastapi, daemon=True)  # Start FastAPI
        api_thread.start()

        # 5. Keep the main thread alive - and let API and other things operate in the threads.
        while True:
            time.sleep(60)  # Main thread to run
    except Exception as e:
        logger.exception(f"An unexpected error occurred: {e}")
    finally:
        if postgres_conn:
            postgres_conn.close()  # Close.
        if mongo_db:
            mongo_db.client.close()
        if "ws" in locals() and ws:
            ws.close()

if __name__ == "__main__":
    main()