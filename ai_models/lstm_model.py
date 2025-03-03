# ai_models/lstm_model.py
import pandas as pd
import numpy as np
from sklearn.preprocessing import MinMaxScaler
from tensorflow.keras.models import Sequential
from tensorflow.keras.layers import LSTM, Dense, Dropout
from sklearn.model_selection import train_test_split #For model training and testing.
import logging
import tensorflow as tf

logger = logging.getLogger(__name__)

def create_lstm_model(input_shape):
    """Creates an LSTM model."""
    try:
        model = Sequential()
        model.add(LSTM(50, return_sequences=True, input_shape=input_shape)) #Adjust units as needed
        model.add(Dropout(0.2))
        model.add(LSTM(50, return_sequences=False)) #Added dropout for regularization
        model.add(Dropout(0.2))
        model.add(Dense(25)) #Hidden layers
        model.add(Dense(1)) #Output layer (predicting one value)
        model.compile(optimizer='adam', loss='mean_squared_error')
        return model
    except Exception as e:
        logger.error(f"LSTM Model creation error: {e}")
        return None

def train_lstm_model(model, data, epochs=10, batch_size=32):
    """Trains the LSTM model."""
    try:
        # Assume data is a pandas DataFrame with a 'close' column
        # Preprocess data
        scaler = MinMaxScaler()
        data['Close'] = scaler.fit_transform(data[['Close']])  # Scale closing prices
        # Create sequences for training (sliding window approach).
        # Example:  Using 60 past prices to predict the next price
        sequence_length = 60
        X, y = [], []
        for i in range(sequence_length, len(data)):
            X.append(data['Close'][i-sequence_length:i].values) #X is input
            y.append(data['Close'][i]) #Y is output
        X, y = np.array(X), np.array(y)
        X = np.reshape(X, (X.shape[0], X.shape[1], 1))  # Reshape for LSTM input (samples, time steps, features)
        #Split into train and test.
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
        # Train model
        model.fit(X_train, y_train, epochs=epochs, batch_size=batch_size, verbose=0)
        # Evaluate the model (optional, but important)
        loss = model.evaluate(X_test, y_test, verbose=0)
        logger.info(f"LSTM Model Loss on test set: {loss}")
        return scaler # return the scaler to scale for later.
    except Exception as e:
        logger.error(f"LSTM Model training error: {e}")
        return None

def predict_lstm_price(model, data, scaler, sequence_length=60):
    """Predicts the price using the LSTM model."""
    try:
        # Data preprocessing (Scale before prediction)
        data['Close'] = scaler.transform(data[['Close']]) #Use the scaler that was fit.
        # Prepare input for the LSTM.  Needs to match the training data prep
        # Take the last 'sequence_length' data points.
        last_sequence = data['Close'].tail(sequence_length).values
        last_sequence = np.reshape(last_sequence, (1, sequence_length, 1)) #Reshape
        predicted_scaled_price = model.predict(last_sequence, verbose=0)[0][0] #Predict.
        # Inverse transform to get the actual price.
        predicted_price = scaler.inverse_transform([[predicted_scaled_price]])[0][0]
        return predicted_price
    except Exception as e:
        logger.error(f"LSTM Price prediction error: {e}")
        return None