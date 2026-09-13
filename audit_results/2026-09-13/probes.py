"""Focused reproductions. All broker submissions are isolated PAPER simulations."""
import exercise as e
import asyncio
import json
import tempfile
from pathlib import Path
from dataclasses import replace
from trading_platform.backtesting.metrics import round_trip_pnls
from trading_platform.domain.enums import OrderStatus
from trading_platform.domain.models import Order
from trading_platform.portfolio.ledger import PortfolioLedger
from trading_platform.backtesting.short_vol_backtest import ShortVolBacktester, load_daily_closes

async def main():
    result = {}
    with tempfile.TemporaryDirectory(prefix='indian-trading-probes-',ignore_cleanup_errors=True) as temp:
        p=Path(temp)
        e.persistence._DEFAULT_DB_PATH=p/'db.sqlite'
        e.oms_store._DEFAULT_DB_PATH=p/'oms.sqlite'
        e.learning_journal._DEFAULT_DB_PATH=p/'journal.sqlite'
        e.trace_store._DEFAULT_DB_PATH=p/'trace.sqlite'
        e.trace_store._DEFAULT_DIR=p/'traces'
        rt=e.TradingRuntime()
        await rt.scheduler.start()
        rt.set_kill_switch(True)
        response=await rt.enqueue_order({'symbol':'RELIANCE','side':'BUY','quantity':1000,'price':1000,'strategy_name':'audit_forged_close','priority':'TARGET','metadata':{'opens_position':False}})
        await asyncio.wait_for(rt.scheduler._queue.join(),timeout=20)
        result['forged_close_execution']={'response':response,'actual_fills':len(rt.portfolio.trades),'position_quantity':rt.portfolio.positions['RELIANCE'].quantity if 'RELIANCE' in rt.portfolio.positions else 0,'kill_switch_active':rt.kill_switch_active}
        await rt.scheduler.stop()
        # Preserve exact instrument multiplier in the ledger, then show metrics dropping it.
        intent=rt._intent_from_payload({'symbol':'RELIANCE','side':'BUY','price':100,'quantity':1})
        intent=replace(intent,instrument=replace(intent.instrument,lot_size=50))
        ledger=PortfolioLedger(1000000)
        ledger.apply_fill(Order(intent,status=OrderStatus.FILLED),100,e.now_ist(),0)
        sell=replace(intent,signal=replace(intent.signal,side=e.Side.SELL))
        ledger.apply_fill(Order(sell,status=OrderStatus.FILLED),110,e.now_ist(),0)
        result['lot_multiplier_metrics']={'lot_size':50,'price_change':10,'lots':1,'actual_ledger_pnl':ledger.cash-1000000,'round_trip_pnls_output':round_trip_pnls(list(ledger.trades))}
        rt.db.close(); rt.oms.close(); rt.paper_learning_journal.close()
    result['shortvol_fixed_parameter_runs']=[]
    vix={b.day:b.close for b in load_daily_closes(e.ROOT/'data/historical/INDIAVIX__ONE_DAY_deep.csv')}
    for symbol in ('NIFTY','BANKNIFTY','FINNIFTY'):
        path=e.ROOT/'data/historical'/f'{symbol}__ONE_DAY_deep.csv'
        if not path.exists(): continue
        bars=load_daily_closes(path)
        for spread in (1.0,2.0):
            bt=ShortVolBacktester(underlying=symbol,spread_points=spread)
            r=bt.run(bars,vix)
            result['shortvol_fixed_parameter_runs'].append({**r.to_dict(),'spread_points_per_leg_per_side':spread,'first':str(bars[0].day),'last':str(bars[-1].day),'bars':len(bars),'data_type':'Cached underlying/VIX; model-derived option premiums, fixed default strategy.'})
    (e.OUT/'probes-results.json').write_text(json.dumps(result,indent=2,default=str),encoding='utf-8')
    print(json.dumps(result,indent=2,default=str))

asyncio.run(main())
