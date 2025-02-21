from kiteconnect import KiteConnect, KiteTicker
from kafka_producer import publish_market_data
import pandas as pd
import asyncio

kite = KiteConnect(api_key="your_api_key")
session = kite.generate_session("request_token", api_secret="your_api_secret")
kite.set_access_token(session["access_token"])

kws = KiteTicker("your_api_key", session["access_token"])

def on_ticks(ws, ticks):
    df = pd.DataFrame(ticks)
    if not df.empty:
        publish_market_data(df.to_dict(orient='records'))

def on_connect(ws, response):
    ws.subscribe([738561])  # Example instrument_token

def on_close(ws, code, reason):
    asyncio.run(reconnect())

async def reconnect():
    await asyncio.sleep(5)
    kws.connect(threaded=True)

kws.on_ticks = on_ticks
kws.on_connect = on_connect
kws.on_close = on_close
kws.connect(threaded=True)