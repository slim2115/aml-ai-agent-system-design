"""Two one-attempt local Judge probes on a synthetic known-defect case."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from qa.dark_judge_injection.probe import VARIANTS, assess, make_payload
from src.qa_profile import checked_path

SOURCES = ('qa/dark_judge_injection/live.py', 'qa/dark_judge_injection/probe.py',
           'qa/judge/transport.py', 'qa/judge/contracts.py', 'qa/judge/protocol_v2.py',
           'qa/judge/dataset.py', 'qa/judge/rubric-v1.json', 'qa/tools/run_llm.py',
           'qa/tools/run_judge.py', 'qa/datasets/judge-v1/cases.json',
           'qa/datasets/judge-v1/manifest.json')


def source_hashes():
    return {name: sha256(checked_path(ROOT / name, ROOT).read_bytes()).hexdigest() for name in SOURCES}


def write_new(path, value):
    checked_path(path, ROOT)
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()


def output_path(value):
    path = Path(value)
    path = checked_path(path if path.is_absolute() else ROOT / path, ROOT)
    if (len(path.relative_to(ROOT).parts) != 4
            or path.relative_to(ROOT).parts[:3] != ('var', 'qa', 'dark-judge-injection')
            or path.exists()):
        raise ValueError('NEW_PROBE_DIRECTORY_REQUIRED')
    return path


def child(output):
    from qa.judge.dataset import load_dataset
    from qa.judge.transport import JudgeTransport
    from qa.tools.run_judge import same_identity
    from qa.tools.run_llm import RecordingHTTPConnection, identity, network_guard
    try:
        marker = json.loads((output / 'attempt.json').read_text(encoding='utf-8'))
        if marker.get('status') != 'STARTED' or marker.get('source_sha256_before') != source_hashes():
            raise ValueError('SOURCE_OR_ATTEMPT_CHANGED')
        dataset = load_dataset()
        selected = [case for case in dataset['cases'] if case['id'] == 'J-003']
        if (len(selected) != 1 or selected[0]['expected_verdict'] != 'fail'
                or selected[0]['payload']['mode'] != 'single'
                or selected[0]['payload']['criterion'] != 'grounding'
                or selected[0]['programmatic_gate']['status'] != 'FAIL'):
            raise ValueError('KNOWN_DEFECT_NOT_VALIDATED')
        case = selected[0]
        rubric = json.loads((ROOT / 'qa/judge/rubric-v1.json').read_text(encoding='utf-8'))
        rows = []
        with network_guard():
            initial = identity()
            for variant, text in VARIANTS:
                current = identity()
                if not same_identity(initial, current):
                    raise ValueError('MODEL_IDENTITY_CHANGED')
                payload = make_payload(case['payload'], text)
                write_new(output / f'{variant}-start.json', {'status': 'REQUEST_MAY_BE_IN_FLIGHT',
                    'attempt': 1, 'model_digest': current['model']['digest'],
                    'payload_sha256': sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                                        separators=(',', ':')).encode('utf-8')).hexdigest()})
                captured = []
                transport = JudgeTransport(profile='compact-v2', connection_factory=lambda host, port, **kwargs:
                    RecordingHTTPConnection(host, port, records=captured, **kwargs))
                record = transport.complete(payload, rubric, deadline=time.monotonic() + 180)
                assessment = assess(payload, text, record, captured, rubric)
                after = identity()
                if not same_identity(initial, after):
                    assessment['identity'] = 'CHANGED'
                    assessment['judge_against_known_defect'] = 'INCOMPLETE'
                else:
                    assessment['identity'] = 'STABLE'
                item = {'variant': variant, 'planned_attempts': 1, 'attempt': 1,
                        'assessment': assessment, 'transport': record, 'wire': captured,
                        'model_digest_before': current['model']['digest'],
                        'model_digest_after': after['model']['digest'],
                        'expected_verdict': 'fail', 'programmatic_gate': case['programmatic_gate']}
                write_new(output / f'{variant}-result.json', item)
                rows.append({'variant': variant, **assessment})
                if assessment['identity'] != 'STABLE':
                    break
        after_sources = source_hashes()
        if after_sources != marker['source_sha256_before']:
            raise ValueError('SOURCE_CHANGED')
        complete = len(rows) == 2 and all(row['delivery'] == 'COMPLETE'
            and row['contract'] == 'VALID' and row['identity'] == 'STABLE' for row in rows)
        result = {'measurement_status': 'PASS' if complete else 'INCOMPLETE',
                  'measurement_meaning': 'Observed and checked requests and valid first responses only.',
                  'historical_judge_status': 'FAIL', 'human_calibration': 'INCOMPLETE',
                  'product_acceptance': 'INCOMPLETE', 'hidden_holdout': 'NOT_ESTABLISHED',
                  'scope': 'Two variants of one open synthetic known-defect Judge case; not product generator.',
                  'network_limit': 'Python localhost guard, not an operating-system firewall.',
                  'delivery_limit': 'HTTP delivery and local response observed; internal token use not independently proven.',
                  'source_sha256_before': marker['source_sha256_before'], 'source_sha256_after': after_sources,
                  'model_digest': initial['model']['digest'], 'cases': rows}
        write_new(output / 'child-result.json', result)
        return 0
    except Exception as exc:
        write_new(output / 'child-error.json', {'status': 'INCOMPLETE', 'error_type': type(exc).__name__,
                                               'reason': 'Probe did not finish; inspect saved attempt markers.'})
        return 1


def validate_saved(output, before, after):
    """Re-evaluate retained first wire/response, not just the child summary."""
    from qa.judge.dataset import load_dataset
    from qa.tools.run_llm import EXPECTED_DIGEST
    if before != after:
        raise ValueError('SOURCE_CHANGED')
    result = json.loads((output / 'child-result.json').read_text(encoding='utf-8'))
    if (result.get('source_sha256_before') != before or result.get('source_sha256_after') != after
            or result.get('model_digest') != EXPECTED_DIGEST
            or result.get('historical_judge_status') != 'FAIL'):
        raise ValueError('INVALID_RESULT_METADATA')
    case = next(case for case in load_dataset()['cases'] if case['id'] == 'J-003')
    if case['expected_verdict'] != 'fail' or case['programmatic_gate']['status'] != 'FAIL':
        raise ValueError('KNOWN_DEFECT_NOT_VALIDATED')
    rubric = json.loads((ROOT / 'qa/judge/rubric-v1.json').read_text(encoding='utf-8'))
    saved = result['cases']
    if type(saved) is not list or len(saved) > 2:
        raise ValueError('INVALID_ATTEMPT_COUNT')
    rows = []
    for (variant, text), previous in zip(VARIANTS, saved):
        item = json.loads((output / f'{variant}-result.json').read_text(encoding='utf-8'))
        start = json.loads((output / f'{variant}-start.json').read_text(encoding='utf-8'))
        payload = make_payload(case['payload'], text)
        payload_hash = sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')).encode('utf-8')).hexdigest()
        if (item.get('variant') != variant or item.get('planned_attempts') != 1 or item.get('attempt') != 1
                or item.get('expected_verdict') != 'fail' or item.get('programmatic_gate') != case['programmatic_gate']
                or item.get('model_digest_before') != EXPECTED_DIGEST
                or item.get('model_digest_after') != EXPECTED_DIGEST
                or start.get('status') != 'REQUEST_MAY_BE_IN_FLIGHT' or start.get('attempt') != 1
                or start.get('model_digest') != EXPECTED_DIGEST or start.get('payload_sha256') != payload_hash):
            raise ValueError('INVALID_SAVED_ATTEMPT')
        assessment = assess(payload, text, item['transport'], item['wire'], rubric)
        identity = item.get('assessment', {}).get('identity')
        assessment['identity'] = identity
        if identity != 'STABLE':
            assessment['judge_against_known_defect'] = 'INCOMPLETE'
        if item['assessment'] != assessment or previous != {'variant': variant, **assessment}:
            raise ValueError('INCONSISTENT_ASSESSMENT')
        rows.append(previous)
    complete = len(rows) == 2 and all(row['delivery'] == 'COMPLETE'
        and row['contract'] == 'VALID' and row['identity'] == 'STABLE' for row in rows)
    expected = 'PASS' if complete else 'INCOMPLETE'
    if result.get('measurement_status') != expected:
        raise ValueError('INCONSISTENT_SUMMARY')
    return expected, rows


def run(value, timeout=420):
    if type(timeout) is not int or not 1 <= timeout <= 600:
        raise ValueError('INVALID_TIMEOUT')
    output = output_path(value)
    output.mkdir(parents=True, exist_ok=False)
    before = source_hashes()
    write_new(output / 'attempt.json', {'status': 'STARTED', 'timeout_seconds': timeout,
                                        'source_sha256_before': before})
    summary = {'measurement_status': 'INCOMPLETE', 'historical_judge_status': 'FAIL',
               'human_calibration': 'INCOMPLETE', 'product_acceptance': 'INCOMPLETE',
               'hidden_holdout': 'NOT_ESTABLISHED'}
    try:
        process = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                                  '--child', str(output)], cwd=ROOT, timeout=timeout,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        path = checked_path(output / 'child-result.json', ROOT)
        if process.returncode != 0 or not path.is_file():
            summary['reason'] = 'CHILD_ERROR_OR_MISSING'
        else:
            summary['measurement_status'], summary['cases'] = validate_saved(output, before, source_hashes())
            summary['reason'] = 'RECOMPUTED_FROM_SAVED_FIRST_ATTEMPTS'
    except subprocess.TimeoutExpired:
        summary['reason'] = 'CHILD_TIMEOUT'
    except (OSError, ValueError, TypeError, KeyError):
        summary['reason'] = 'SUPERVISOR_OR_VALIDATION_ERROR'
    write_new(output / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir')
    parser.add_argument('--timeout-seconds', type=int, default=420)
    parser.add_argument('--child', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        path = checked_path(Path(args.child), ROOT)
        if len(path.relative_to(ROOT).parts) != 4 or path.relative_to(ROOT).parts[:3] != ('var', 'qa', 'dark-judge-injection'):
            return 2
        return child(path)
    if not args.output_dir:
        parser.error('--output-dir is required')
    result = run(args.output_dir, args.timeout_seconds)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['measurement_status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
