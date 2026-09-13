"""Run existing tests without loading local credentials or production stores."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
for key in list(os.environ):
    if key.startswith(('ANGEL_ONE_', 'UPSTOX_')):
        os.environ.pop(key)
for key in ('LIVE_TRADING_ENABLED', 'AUTO_START_AGENT', 'AUTO_START_LIVE_FEED',
            'ENABLE_AI_COUNCIL', 'ENABLE_NEURAL_LAB', 'ENABLE_QUANTUM_LAB',
            'ENABLE_MARL_LAB', 'ENABLE_GOAL_GOVERNOR', 'QDRANT_ENABLED',
            'AUTO_LOAD_MODELS', 'AUTO_LOAD_INSTRUMENT_CACHE', 'API_AUTH_REQUIRED'):
    os.environ[key] = 'false'
os.environ['EXECUTION_MODE'] = 'PAPER'
os.environ['API_AUTH_TOKEN'] = ''
os.environ['LOCAL_LLM_GATEWAY'] = 'disabled'
from trading_platform import config
original_load = config.load_local_env_files
config.load_local_env_files = lambda: original_load() if Path.cwd().resolve() != ROOT else None
import pytest
args = sys.argv[1:] or ['tests/', '-q', '--tb=short',
                       '--junitxml=audit_results/2026-09-13/backend-tests.xml']
if '--audit-governor-on' in args:
    args.remove('--audit-governor-on')
    os.environ['ENABLE_GOAL_GOVERNOR'] = 'true'
raise SystemExit(pytest.main(args))
