"""Run only the separate, explicit live QA profile; no global integration bypass."""
import argparse
from datetime import datetime, timezone
import json
import os
import secrets
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.qa_profile import QAProfile, checked_path
from qa.tools.environment import child_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    profile = QAProfile(args.run_id).validate()
    output = checked_path(ROOT / args.output_dir)
    if not (output.is_relative_to(profile.base) or output.is_relative_to(ROOT / 'verification')):
        raise ValueError('INVALID_QA_EVIDENCE_PATH')
    if output.exists():
        raise ValueError('QA_EVIDENCE_ALREADY_EXISTS')
    output.mkdir(parents=True)
    environment = child_environment(profile)
    environment['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    environment['AML_QA_PROBE_ID'] = secrets.token_hex(16)
    os.environ.clear()
    os.environ.update(environment)
    import pytest
    code = pytest.main(['-c', str(ROOT / 'qa/pytest.ini'), str(ROOT / 'qa/integration'),
                        '-q', '--junitxml=' + str(output / 'tests.xml')])
    suites = ET.parse(output / 'tests.xml')
    rows = list(suites.iter('testcase'))
    failures = sum(any(child.tag in ('failure', 'error', 'skipped') for child in row) for row in rows)
    result = {'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'run_id': profile.run_id,
              'probe_id': environment['AML_QA_PROBE_ID'],
              'status': 'PASS' if code == 0 and rows and failures == 0 else 'FAIL',
              'pytest_exit_code': int(code), 'testcases': len(rows), 'nonpassing': failures,
              'scope': 'Explicit real QA environment integration. No model preparation, browser E2E or acceptance.'}
    (output / 'result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    if (profile.base / 'last-qualification.json').exists():
        raw = (profile.base / 'last-qualification.json').read_bytes()
        if json.loads(raw).get('probe_id') == result['probe_id']:
            (output / 'observations.json').write_bytes(raw)
    print(json.dumps(result))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
