"""Run meaningful offline checks and save an evidence record, without API calls."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from futureeval_bot.core import Config, source_hashes

os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
(root / 'work').mkdir(exist_ok=True)
(root / 'outputs').mkdir(exist_ok=True)
temp_root = root / 'work' / 'tmp'
temp_root.mkdir(exist_ok=True)
os.environ['TEMP'] = str(temp_root)
os.environ['TMP'] = str(temp_root)
start = time.monotonic()
test_dir = root / 'work' / 'test-runs' / ('run-' + uuid.uuid4().hex)
test_dir.parent.mkdir(exist_ok=True)
result = subprocess.run([sys.executable, '-m', 'pytest', 'tests', '-q', '--tb=short',
    '--basetemp=' + str(test_dir), '--junitxml=work/offline-tests.xml'], cwd=root)
tree = ET.parse(root / 'work/offline-tests.xml')
cases = list(tree.iter('testcase'))
checks = [{'name': case.attrib['name'], 'passed': not any(case.find(tag) is not None for tag in ['failure','error','skipped'])} for case in cases]
sources = sorted((root / 'futureeval_bot').glob('*.py'))
report = {'at': datetime.now(timezone.utc).isoformat(), 'passed': result.returncode == 0,
    'test_count': len(cases), 'elapsed_seconds': round(time.monotonic() - start, 2),
    'config_fingerprint': Config.load(root / 'config.json').fingerprint,
    'source_hashes': source_hashes(),
    'checks': checks, 'scope': 'Offline synthetic fixtures and simulated failures. Does not prove real API submissions, scheduled uptime, or forecasting accuracy.'}
(root / 'outputs/offline-verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
sys.exit(result.returncode)
