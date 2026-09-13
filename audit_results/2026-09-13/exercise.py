"""Offline paper execution and historical experiments. No live credentials or orders."""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
for key in list(os.environ):
    if key.startswith(('ANGEL_ONE_', 'UPSTOX_')): os.environ.pop(key)
for key in ('LIVE_TRADING_ENABLED', 'AUTO_START_AGENT', 'AUTO_START_LIVE_FEED',
            'ENABLE_AI_COUNCIL', 'ENABLE_NEURAL_LAB', 'ENABLE_QUANTUM_LAB',
            'ENABLE_MARL_LAB', 'ENABLE_GOAL_GOVERNOR', 'QDRANT_ENABLED',
            'AUTO_LOAD_MODELS', 'AUTO_LOAD_INSTRUMENT_CACHE', 'API_AUTH_REQUIRED'):
    os.environ[key] = 'false'
os.environ['EXECUTION_MODE'] = 'PAPER'
os.environ['LOCAL_LLM_GATEWAY'] = 'disabled'
os.environ['DATABASE_URL'] = ''
from trading_platform import config
config.load_local_env_files = lambda: None
from trading_platform.data import persistence
from trading_platform.execution import oms_store
from trading_platform.trace import learning_journal, store as trace_store
from trading_platform.api.runtime import TradingRuntime
from trading_platform.backtesting.engine import BacktestEngine, BacktestConfig
from trading_platform.backtesting.metrics import round_trip_pnls
from trading_platform.data.market_data import HistoricalDataProvider
from trading_platform.domain.enums import Side, OrderType, ExecutionMode
from trading_platform.domain.models import Trade
from trading_platform.agent.market_hours import is_trading_day, market_status, now_ist

results = {'scope': 'Offline isolated PAPER execution; cached historical and synthetic backtests. No live market session.', 'run_at': datetime.now(timezone.utc).isoformat()}

def save():
    (OUT / 'exercise-results.json').write_text(json.dumps(results, indent=2, default=str), encoding='utf-8')

def summarize(result):
    return {**result.to_dict(), 'charges': result.total_charges,
            'matched_pnl_count': len(result.trade_pnls),
            'rejections': dict(Counter(r.risk_decision.reason for r in result.reports if not r.risk_decision.approved)),
            'equity_samples': len(result.equity_curve),
            'unique_equity_dates': len(set(t.date() for t, _ in result.equity_curve)),
            'filled_segments': dict(Counter(r.order.intent.instrument.segment.value for r in result.reports if r.trade))}

def existing_history():
    con = sqlite3.connect((ROOT / 'data/trading.db').as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    queries = {
        'trade_groups': "SELECT execution_mode,is_test,count(*) fills,min(timestamp) first,max(timestamp) last,sum(charges) charges FROM trades GROUP BY execution_mode,is_test",
        'paper_strategies': "SELECT strategy_name,count(*) fills,sum(charges) charges,min(price) min_price FROM trades WHERE execution_mode='PAPER' AND coalesce(is_test,0)=0 GROUP BY strategy_name",
        'snapshot_ranges': "SELECT execution_mode,count(*) samples,min(recorded_at) first,max(recorded_at) last,min(equity) minimum_equity,max(equity) maximum_equity FROM portfolio_snapshots GROUP BY execution_mode",
        'latest_snapshots': "SELECT recorded_at,cash,equity,realized_pnl,unrealized_pnl,drawdown,execution_mode FROM portfolio_snapshots ORDER BY id DESC LIMIT 5",
        'first_snapshots': "SELECT recorded_at,cash,equity,realized_pnl,unrealized_pnl,drawdown,execution_mode FROM portfolio_snapshots ORDER BY id LIMIT 5",
        'zero_price_fills': "SELECT execution_mode,is_test,count(*) fills FROM trades WHERE price<=0 GROUP BY execution_mode,is_test",
    }
    data = {k: [dict(r) for r in con.execute(q)] for k,q in queries.items()}
    ts = [Trade(**{k:r[k] for k in ('trade_id','order_id','symbol','quantity','price','charges','strategy_name')}, side=Side(r['side']), timestamp=datetime.fromisoformat(r['timestamp'])) for r in con.execute("SELECT * FROM trades WHERE execution_mode='PAPER' AND coalesce(is_test,0)=0 ORDER BY timestamp,rowid")]
    pnls = round_trip_pnls(ts)
    data['fifo_diagnostic'] = {'matched_chunks':len(pnls), 'net_pnl':sum(pnls), 'wins':sum(p>0 for p in pnls), 'losses':sum(p<0 for p in pnls), 'profit_factor':sum(p for p in pnls if p>0)/-sum(p for p in pnls if p<0) if any(p<0 for p in pnls) else None,
                              'caveat':'Local non-test-tagged records; completeness, reset history and original quote provenance are not established.'}
    con.close()
    return data

class CachedProvider:
    def __init__(self, bars): self.bars = bars
    def generate_many(self, symbols, start, days): return {s:self.bars[s] for s in symbols}

async def main():
    results['existing_history'] = existing_history()
    results['calendar'] = {d: is_trading_day(date.fromisoformat(d)) for d in ('2026-09-13','2026-09-14','2026-03-03','2026-03-25')}
    results['market_status_now'] = market_status()
    save()
    with tempfile.TemporaryDirectory(prefix='indian-trading-audit-', ignore_cleanup_errors=True) as temp:
        p = Path(temp)
        persistence._DEFAULT_DB_PATH = p / 'trading.db'
        oms_store._DEFAULT_DB_PATH = p / 'oms.db'
        learning_journal._DEFAULT_DB_PATH = p / 'learning.db'
        trace_store._DEFAULT_DB_PATH = p / 'trace.db'
        trace_store._DEFAULT_DIR = p / 'traces'
        runtime = TradingRuntime()
        assert runtime.execution_mode == ExecutionMode.PAPER and not runtime.settings.live_trading_enabled
        results['paper_initial_state'] = runtime.state_payload()
        results['synthetic_backtests'] = []
        for days in (30,90):
            r = runtime.backtest_engine.run(BacktestConfig(days=days))
            results['synthetic_backtests'].append(summarize(r))
            save()
        # Use unchanged application equity strategies on cached daily bars, with no parameter selection.
        results['cached_backtests'] = []
        loader = HistoricalDataProvider()
        for symbol in ('RELIANCE','TCS','INFY'):
            bars = loader.load_csv(ROOT / 'data/historical' / f'{symbol}__ONE_DAY.csv', symbol)
            for strategy in ('equity_momentum','swing_trend'):
                cfg = BacktestConfig(start=bars[0].timestamp.date(), days=len(bars), underlyings=(symbol,), strategy_names=(strategy,))
                r = BacktestEngine(data_provider=CachedProvider({symbol:bars})).run(cfg)
                summary = summarize(r)
                summary['data_first'] = bars[0].timestamp.isoformat()
                summary['data_last'] = bars[-1].timestamp.isoformat()
                summary['bars'] = len(bars)
                summary['buy_hold_return'] = bars[-1].close/bars[21].open-1
                results['cached_backtests'].append(summary)
                save()
        # Execute a real runtime PAPER round trip through queue, final gate, fills and persistence.
        await runtime.scheduler.start()
        entry = {'symbol':'RELIANCE','side':'BUY','quantity':1,'price':1000,'strategy_name':'audit_paper_roundtrip','metadata':{'agent_auto':True},'stop_loss':950,'target':1050}
        enqueue = await runtime.enqueue_order(entry)
        await asyncio.wait_for(runtime.scheduler._queue.join(), timeout=20)
        exit_payload = {**entry,'side':'SELL','price':1010,'priority':'TARGET','metadata':{'opens_position':False,'agent_auto':True}}
        exit_enqueue = await runtime.enqueue_order(exit_payload)
        await asyncio.wait_for(runtime.scheduler._queue.join(), timeout=20)
        await runtime.scheduler.stop()
        results['paper_roundtrip'] = {'entry_response':enqueue, 'exit_response':exit_enqueue, 'trades':[asdict(t) for t in runtime.portfolio.trades], 'snapshot':asdict(runtime.portfolio.mark_to_market(now_ist(),{'RELIANCE':1010})), 'stats':runtime.scheduler.stats,
            'prices':'Deliberately supplied test prices 1000 and 1010; this tests execution, not alpha or market profit.'}
        # Demonstrate the risk implications of trusting opens_position supplied by callers.
        forged = runtime._intent_from_payload({**entry,'quantity':1000,'metadata':{'opens_position':False},'priority':'TARGET'})
        runtime.set_kill_switch(True)
        results['reproductions'] = {'forged_close_on_flat_portfolio':runtime._evaluate_final_execution_gate(forged).to_dict()}
        ordinary = runtime._intent_from_payload({**entry,'quantity':1000})
        results['reproductions']['ordinary_entry_under_kill_switch'] = runtime._evaluate_final_execution_gate(ordinary).to_dict()
        limit = runtime._intent_from_payload({**entry,'order_type':'LIMIT','limit_price':1000})
        fill = runtime.paper_broker.submit_order(limit)
        results['reproductions']['marketable_buy_limit'] = {'limit':1000,'fill':fill.average_price,'status':fill.status.value,'breaches_limit':fill.average_price > 1000}
        # This is a gate-only check; no broker object is created in LIVE mode.
        runtime.execution_mode = ExecutionMode.LIVE
        results['reproductions']['unarmed_live_entry'] = runtime._evaluate_final_execution_gate(ordinary).to_dict()
        runtime.execution_mode = ExecutionMode.PAPER
        save()
        runtime.db.close()
        runtime.oms.close()
        runtime.paper_learning_journal.close()
    print('Completed isolated paper roundtrip, 2 synthetic and 6 cached-data backtests. See exercise-results.json')

if __name__ == '__main__':
    asyncio.run(main())
