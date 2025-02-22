import pandas as pd

def mean_reversion_strategy(df, window=20):
    df['rolling_mean'] = df['price'].rolling(window=window).mean()
    df['rolling_std'] = df['price'].rolling(window=window).std()
    df['z_score'] = (df['price'] - df['rolling_mean']) / df['rolling_std']
    df['signal'] = df['z_score'].apply(lambda x: 'BUY' if x < -1 else 'SELL' if x > 1 else 'HOLD')
    return df[['price', 'rolling_mean', 'z_score', 'signal']]