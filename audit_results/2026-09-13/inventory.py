import ast
import csv
import json
import sqlite3
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
out = {'source': [], 'historical': [], 'databases': {}}
for folder in ('trading_platform', 'scripts', 'tests', 'hft_frontend/src'):
    for p in sorted((ROOT / folder).rglob('*')):
        if p.suffix not in ('.py', '.ts', '.tsx'): continue
        s = p.read_text(encoding='utf-8-sig')
        row = {'path': str(p.relative_to(ROOT)), 'lines': len(s.splitlines())}
        if p.suffix == '.py':
            tree = ast.parse(s)
            row['bare_pass_handlers'] = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and len(n.body) == 1 and isinstance(n.body[0], ast.Pass)]
        out['source'].append(row)
for p in sorted((ROOT / 'data/historical').glob('*.csv')):
    with p.open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    ts = [r.get('timestamp', r.get('date', '')) for r in rows]
    out['historical'].append({'file': p.name, 'rows': len(rows), 'first': min(ts) if ts else None, 'last': max(ts) if ts else None, 'duplicate_timestamps': len(ts)-len(set(ts))})
for name in ('trading.db', 'oms_events.db', 'paper_learning_journal.db'):
    path = ROOT / 'data' / name
    con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    out['databases'][name] = {}
    for t in tables:
        qt = '"' + t.replace('"', '""') + '"'
        out['databases'][name][t] = {'count': con.execute(f'SELECT count(*) FROM {qt}').fetchone()[0], 'columns': [r[1] for r in con.execute(f'PRAGMA table_info({qt})')]}
    con.close()
(Path(__file__).parent / 'inventory.json').write_text(json.dumps(out, indent=2), encoding='utf-8')
print(json.dumps({'source_files': len(out['source']), 'source_lines': sum(r['lines'] for r in out['source']), 'historical_files': len(out['historical']), 'databases': out['databases']}, indent=2))
