"""Read-only inspection of the running application using its configured API auth."""
import json
import subprocess
from pathlib import Path
code = '''
import json, os, urllib.request, urllib.error, hashlib, pathlib
paths = ['/state','/health','/agent/status','/portfolio/positions','/portfolio/target-progress','/live/readiness','/db/trades?execution_mode=PAPER&limit=1000','/data/status','/db/summary','/db/equity-curve?execution_mode=PAPER&limit=1000','/db/daily-pnl?limit=100','/ai-council/skill-eval','/backtests/gates','/strategies/promotions']
out = {}
out['_deployment_config'] = {k:os.environ.get(k) for k in ['EXECUTION_MODE','LIVE_TRADING_ENABLED','API_AUTH_REQUIRED','INITIAL_CAPITAL','AGENT_DIRECTIONAL_ENABLED','SHORTVOL_AUTO_ENABLED','SHORTVOL_AUTO_UNDERLYINGS','SHORTVOL_STRUCTURES','SHORTVOL_CONTINUOUS','SHORTVOL_MIN_DTE','ENABLE_GOAL_GOVERNOR']}
out['_deployment_config']['authentication_configured'] = bool(os.environ.get('API_AUTH_TOKEN'))
try:
    with urllib.request.urlopen('http://127.0.0.1:8000/portfolio/positions',timeout=10) as response:
        out['_unauthenticated_portfolio_status'] = response.status
except urllib.error.HTTPError as exc:
    out['_unauthenticated_portfolio_status'] = exc.code
out['_source_hashes'] = {p:hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest() for p in ['trading_platform/api/runtime.py','trading_platform/risk/engine.py','trading_platform/broker/simulated.py','trading_platform/backtesting/engine.py','trading_platform/backtesting/metrics.py','trading_platform/agent/market_hours.py','trading_platform/portfolio/ledger.py']}
for path in paths:
    req=urllib.request.Request('http://127.0.0.1:8000'+path,headers={'Authorization':'Bearer '+os.environ.get('API_AUTH_TOKEN','')})
    try:
        with urllib.request.urlopen(req,timeout=20) as r: out[path]=json.load(r)
    except urllib.error.HTTPError as e: out[path]={'http_status':e.code}
    except Exception as e: out[path]={'error_type':type(e).__name__}
def scrub(obj):
    if isinstance(obj,dict): return {k:('[REDACTED]' if any(s in k.lower() for s in ['token','password','secret','api_key','client_code']) else scrub(v)) for k,v in obj.items()}
    if isinstance(obj,list): return [scrub(v) for v in obj]
    return obj
print(json.dumps(scrub(out),default=str))
'''
proc=subprocess.run(['docker','exec','trading-api','python','-c',code],capture_output=True,text=True,check=True)
data=json.loads(proc.stdout)
(Path(__file__).parent/'deployment-readonly.json').write_text(json.dumps(data,indent=2),encoding='utf-8')
for key in ('/state','/portfolio/positions','/portfolio/target-progress','/agent/status'):
    print(key, json.dumps(data.get(key),default=str)[:7000])
print('Health keys:',list(data.get('/health',{})))
print('Trade response keys:',list(data.get('/db/trades?execution_mode=PAPER&limit=1000',{})))
