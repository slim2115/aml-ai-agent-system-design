"""Actual CLI processes for terminal states which require no embedding/model."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.contracts.v1 import ADAPTERS


def main():
    rows = []
    cases = [([], 3, 'MISSING_INPUT'), (['bad-input'], 3, 'INVALID_INPUT_FORMAT'),
             (['INC-000123'], 2, 'POLICY_NOT_APPROVED'),
             (['INC-999999', '--demo'], 3, 'NOT_FOUND'),
             (['INC-000999', '--demo'], 2, 'INSUFFICIENT_DATA'),
             (['INC-000307', '--demo'], 2, 'NO_APPLICABLE_RULE')]
    for args, expected_exit, expected_code in cases:
        process = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'scripts/run_investigation.py'), *args],
            cwd=ROOT, capture_output=True, encoding='utf-8', timeout=15)
        payload = json.loads(process.stdout)
        ADAPTERS['InvestigationResult'].validate_python(payload)
        code = payload.get('code', payload.get('reason_code'))
        rows.append({'args': args, 'exit_code': process.returncode, 'result_code': code,
                     'passed': process.returncode == expected_exit and code == expected_code and not process.stderr})
    passed = all(row['passed'] for row in rows)
    record = {'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'status': 'PASS' if passed else 'FAIL',
              'checks': rows, 'scope': 'Six real v1 CLI terminal-state processes; no LLM calls or network.'}
    (ROOT / 'verification/block-04/cli-process-controls.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(record, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
