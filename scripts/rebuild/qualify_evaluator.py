"""Real CLI process controls; all inputs are synthetic, never SME acceptance."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.evaluator.fixtures import bundle, external_controls


def main():
    evidence = ROOT / 'verification/block-03'
    controls_dir = ROOT / 'var/evaluation/block-03-process-controls'
    controls_dir.mkdir(parents=True, exist_ok=True)
    declared = bundle(30)
    declared['dataset'] = {'id': 'SYNTHETIC-MECHANICAL-PASS-BRANCH-NOT-ACCEPTANCE', 'kind': 'acceptance',
        'sme_acceptance': {'status': 'accepted', 'reviewer_id': 'TEST-NOT-A-HUMAN',
            'reviewed_at': '2026-09-23T00:00:00Z',
            'evidence_references': ['synthetic://NOT-AN-APPROVAL'],
            'case_ids': [case['id'] for case in declared['cases']]}}
    declared['external_measurements'] = external_controls()
    broken = bundle()
    broken['outcomes'][0]['result'] = {'status': 'draft'}
    latency = deepcopy(declared)
    latency['outcomes'][0]['latency_seconds'] = 180.04
    examples = [('empty', bundle(0), 2, 'INCOMPLETE'),
                ('synthetic_only', bundle(30), 2, 'INCOMPLETE'),
                ('invalid_result', broken, 1, 'FAIL'),
                ('unrounded_latency', latency, 1, 'FAIL'),
                ('declared_acceptance_branch_only', declared, 0, 'PASS'),
                ('malformed_input', {'PRIVATE_SENTINEL': 'MUST_NOT_BE_ECHOED'}, 3, 'ERROR')]
    checks = []
    for name, data, expected_exit, expected_verdict in examples:
        source = controls_dir / (name + '-input.json')
        output = controls_dir / (name + '-report.md')
        source.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        completed = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'scripts/run_evaluation.py'),
                                    '--input', str(source), '--output', str(output)],
                                   cwd=ROOT, capture_output=True, text=True, timeout=30)
        report = json.loads(output.with_suffix('.json').read_text(encoding='utf-8'))
        safe = 'MUST_NOT_BE_ECHOED' not in completed.stdout + completed.stderr + output.read_text(encoding='utf-8')
        passed = completed.returncode == expected_exit and report['verdict'] == expected_verdict and safe
        checks.append({'name': name, 'expected_exit': expected_exit, 'exit_code': completed.returncode,
                       'expected_verdict': expected_verdict, 'verdict': report['verdict'], 'passed': passed,
                       'stdout': completed.stdout.strip(), 'safe_diagnostics': safe})
    result = {'checked_at_utc': datetime.now(timezone.utc).isoformat(),
              'status': 'PASS' if all(check['passed'] for check in checks) else 'FAIL', 'checks': checks,
              'scope': 'CLI qualification only. The PASS branch intentionally uses declared synthetic approval metadata; it is NOT a human decision or product acceptance.'}
    (evidence / 'cli-process-controls.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
