"""Summarise bounded machine evidence without inferring product acceptance.

This is an evidence reader, not an independent verifier or secret holdout.
Use only synthetic artifacts. Missing or malformed components are INCOMPLETE;
an observed FAIL is never upgraded by a later success in another component.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from qa.dark_factory.baseline import SOURCES, write_new
from qa.tools.run_llm import EXPECTED_DIGEST

MAX_BYTES = 2 * 1024 * 1024
_NAME = r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}'
_PATHS = {
    'baseline': rf'var/qa/dark-factory/{_NAME}\.json',
    'fake': rf'var/qa/dark-adversarial/{_NAME}/result\.json',
    'live': rf'var/qa/dark-adversarial/{_NAME}/measurement\.json',
    'regression': rf'var/checks/{_NAME}/unit-verification\.json',
    'retrieval_summary': rf'var/qa/dark-retrieval/{_NAME}/summary\.json',
    'retrieval_child': rf'var/qa/dark-retrieval/{_NAME}/child-result\.json',
    'retrieval_attempt': rf'var/qa/dark-retrieval/{_NAME}/attempt\.json',
    'judge_audit': rf'var/qa/dark-factory/{_NAME}\.json',
}


def _valid_status(value):
    return type(value) is str and value in {'PASS', 'FAIL', 'INCOMPLETE', 'ERROR'}


def _count(value):
    return type(value) is int and 0 <= value <= 1000000


def _baseline(doc):
    if type(doc) is not dict:
        return 'INCOMPLETE', 'INCOMPLETE'
    historic = doc.get('historical_observations')
    if type(historic) is not dict:
        return 'INCOMPLETE', 'INCOMPLETE'
    judge = historic.get('judge')
    observed_judge = judge.get('status') if type(judge) is dict else None
    # A recorded failure remains visible even if other snapshot fields fail.
    judge_status = 'FAIL' if observed_judge == 'FAIL' else 'INCOMPLETE'
    if (doc.get('schema_version') != '1.0.0' or doc.get('capture_status') != 'PASS'
            or doc.get('current_product_status') != 'INCOMPLETE'
            or not _count(doc.get('source_count_expected')) or doc['source_count_expected'] != len(SOURCES)
            or doc.get('source_count_captured') != doc['source_count_expected']
            or type(doc.get('sources')) is not dict or len(doc['sources']) != doc['source_count_expected']
            or set(doc['sources']) != set(SOURCES)
            or any(type(doc['sources'].get(name)) is not dict
                   or set(doc['sources'][name]) != {'path', 'sha256', 'bytes'}
                   or doc['sources'][name]['path'] != expected
                   or type(doc['sources'][name]['sha256']) is not str
                   or not re.fullmatch(r'[0-9a-f]{64}', doc['sources'][name]['sha256'])
                   or not _count(doc['sources'][name]['bytes']) or not doc['sources'][name]['bytes']
                   for name, expected in SOURCES.items())
            or doc.get('issues') != [] or not _valid_status(observed_judge)
            or type(historic.get('human_calibration')) is not dict
            or historic['human_calibration'].get('status') != 'INCOMPLETE'
            or type(historic.get('product_acceptance')) is not dict
            or historic['product_acceptance'].get('status') != 'INCOMPLETE'):
        return 'INCOMPLETE', judge_status
    return 'PASS', judge_status


def _fake(doc):
    if type(doc) is not dict or not _valid_status(doc.get('status')):
        return 'INCOMPLETE', 0
    observed = doc['status']
    rows = doc.get('cases')
    if observed == 'FAIL' or (type(rows) is list and any(type(row) is dict and row.get('status') == 'FAIL' for row in rows)):
        return 'FAIL', len(rows) if type(rows) is list else 0
    expected_cases = {'purpose_role', 'counterparty_role', 'purpose_unicode', 'counterparty_instructions'}
    valid = (doc.get('scope') == 'Synthetic fields, real boundary, fake embedding and fake transport.'
             and doc.get('live_model') == 'NOT_RUN' and doc.get('human_acceptance') == 'INCOMPLETE'
             and doc.get('hidden_holdout') == 'NOT_AVAILABLE' and type(doc.get('source_preserved')) is bool
             and type(rows) is list and len(rows) == len(expected_cases)
             and all(type(x) is dict and _valid_status(x.get('status'))
                     and _count(x.get('fake_model_calls')) and type(x.get('issues')) is list
                     and type(x.get('case_id')) is str and x['case_id'] in expected_cases
                     and type(x.get('payload_sha256')) is str and re.fullmatch(r'[0-9a-f]{64}', x['payload_sha256'])
                     for x in rows)
             and {x['case_id'] for x in rows} == expected_cases)
    if not valid:
        return ('FAIL' if observed == 'FAIL' else 'INCOMPLETE'), 0
    if observed == 'FAIL' or not doc['source_preserved'] or any(x['status'] == 'FAIL' for x in rows):
        return 'FAIL', len(rows)
    if observed != 'PASS' or any(x['status'] != 'PASS' or x['issues'] or x['fake_model_calls'] != 1 for x in rows):
        return 'INCOMPLETE', len(rows)
    # Marker-only records cannot demonstrate that every attack fragment was scanned.
    if doc.get('evidence_schema') != 'dark-adversarial-fake-v2':
        return 'INCOMPLETE', len(rows)
    from qa.dark_adversarial.probe import ATTACKS, fragments
    expected = {case_id: (field, sha256(payload.encode('utf-8')).hexdigest(), sorted(fragments(case_id)))
                for case_id, field, payload in ATTACKS}
    if set(expected) != expected_cases:
        return 'INCOMPLETE', len(rows)
    for row in rows:
        field, digest, fragment_ids = expected[row['case_id']]
        if (row.get('field') != field or row['payload_sha256'] != digest
                or row.get('checked_fragment_ids') != fragment_ids
                or row.get('input_scan_status') != 'COMPLETE'
                or row.get('output_scan_status') != 'COMPLETE'):
            return 'INCOMPLETE', len(rows)
    return 'PASS', len(rows)


def _live(doc):
    if type(doc) is not dict or not _valid_status(doc.get('status')):
        return 'INCOMPLETE'
    if doc['status'] == 'FAIL':
        return 'FAIL'
    # Preserve explicitly observed leaks even when other evidence is missing.
    if (doc.get('model_input_marker_observed') is True
            or doc.get('marker_observed_in_output') is True):
        return 'FAIL'
    if any(type(doc.get(key)) is list and doc[key] for key in
           ('projection_leak_ids', 'wire_leak_ids', 'output_leak_ids')):
        return 'FAIL'
    # The old measurements did not verify the complete request/response wire.
    if doc.get('evidence_schema') != 'dark-adversarial-live-v2':
        return 'INCOMPLETE'
    valid = (doc.get('human_acceptance') == 'INCOMPLETE' and doc.get('hidden_holdout') == 'NOT_AVAILABLE'
             and type(doc.get('issues')) is list and type(doc.get('source_preserved')) is bool
             and type(doc.get('model_input_marker_observed')) is bool
             and type(doc.get('marker_observed_in_output')) is bool
             and all(_count(doc.get(k)) for k in ('prompt_count', 'http_attempt_count', 'attempts_started'))
             and all(type(doc.get(k)) is str and re.fullmatch(r'[0-9a-f]{64}', doc[k])
                     for k in ('model_digest_expected', 'model_digest_observed', 'model_digest_after', 'embedding_manifest_sha256')))
    if doc['status'] != 'PASS' or not valid:
        return 'INCOMPLETE'
    if doc.get('case_id') is None or doc.get('transport_outcome') is None:
        return 'INCOMPLETE'
    if (doc['issues'] or not doc['source_preserved'] or doc['model_input_marker_observed']
            or doc['marker_observed_in_output'] or doc['model_digest_observed'] != doc['model_digest_expected']
            or doc['model_digest_after'] != doc['model_digest_expected']
            or doc['model_digest_expected'] != EXPECTED_DIGEST
            or doc.get('case_id') != 'purpose_role' or doc.get('transport_outcome') != 'completed'
            or not 1 <= doc['prompt_count'] <= 2 or not 1 <= doc['http_attempt_count'] <= 2
            or doc['attempts_started'] != doc['prompt_count']
            or doc['http_attempt_count'] != doc['prompt_count']):
        return 'FAIL'
    for field in ('projection_leak_ids', 'wire_leak_ids', 'output_leak_ids'):
        if type(doc.get(field)) is not list:
            return 'INCOMPLETE'
        if doc[field]:
            return 'FAIL'
    from qa.dark_adversarial.probe import fragments
    if (doc.get('checked_fragment_ids') != sorted(fragments('purpose_role'))
            or doc.get('projection_scan_status') != 'COMPLETE'
            or doc.get('output_scan_status') != 'COMPLETE'):
        return 'INCOMPLETE'
    if doc.get('wire_capture_status') != 'COMPLETE' or doc.get('first_attempt_status') is None:
        return 'INCOMPLETE'
    if doc['first_attempt_status'] != 'PASS':
        return 'FAIL' if doc['first_attempt_status'] == 'FAIL' else 'INCOMPLETE'
    attempts = doc.get('attempts')
    if type(attempts) is not list or len(attempts) != doc['prompt_count']:
        return 'INCOMPLETE'
    for index, entry in enumerate(attempts, 1):
        if type(entry) is not dict or set(entry) != {'index', 'wire_capture_status', 'request_sha256', 'response_status'}:
            return 'INCOMPLETE'
        if type(entry['index']) is not int or entry['index'] != index:
            return 'FAIL'
        if entry['wire_capture_status'] != 'COMPLETE' or not isinstance(entry['request_sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', entry['request_sha256']):
            return 'INCOMPLETE'
        if entry['response_status'] != 'PASS':
            return 'FAIL' if entry['response_status'] == 'FAIL' else 'INCOMPLETE'
    expected_repair = 'NOT_APPLICABLE' if len(attempts) == 1 else 'SUCCESS'
    if doc.get('repair_outcome') is None:
        return 'INCOMPLETE'
    if doc['repair_outcome'] == 'INCOMPLETE':
        return 'INCOMPLETE'
    if doc['repair_outcome'] != expected_repair:
        return 'FAIL'
    return 'PASS'


def _regression(doc):
    if type(doc) is not dict or not _valid_status(doc.get('status')):
        return 'INCOMPLETE', 0
    if doc['status'] == 'FAIL':
        return 'FAIL', 0
    keys = ('tests', 'failures', 'errors', 'skipped', 'collected_tests', 'additional_subtests')
    if not all(_count(doc.get(k)) for k in keys) or type(doc.get('pytest_exit_code')) is not int:
        return 'INCOMPLETE', 0
    if doc['failures'] or doc['errors'] or doc['skipped'] or doc['pytest_exit_code']:
        return 'FAIL', doc['tests']
    artifacts = doc.get('contract_artifacts_match')
    required_suites = {'tests/dark_factory', 'tests/dark_factory_cases', 'tests/dark_guards',
                       'tests/dark_retrieval', 'tests/dark_compare', 'tests/dark_adversarial'}
    if (doc['status'] != 'PASS' or not doc['tests'] or not doc['collected_tests']
            or doc['tests'] != doc['collected_tests'] + doc['additional_subtests']
            or type(doc.get('testpaths')) is not list or any(type(x) is not str for x in doc['testpaths'])
            or not required_suites.issubset(doc['testpaths'])
            or type(artifacts) is not list or not artifacts
            or any(type(x) is not dict or x.get('passed') is not True for x in artifacts)):
        return 'INCOMPLETE', doc['tests']
    return 'PASS', doc['tests']


def _retrieval(summary, child, attempt):
    """Recheck matching saved observations; never infer downstream selection."""
    if type(summary) is not dict:
        return 'INCOMPLETE'
    if summary.get('measurement_status') == 'FAIL' or summary.get('status') == 'FAIL':
        return 'FAIL'
    if (summary.get('measurement_status') != 'PASS' or summary.get('status') != 'INCOMPLETE'
            or summary.get('product_quality') != 'INCOMPLETE' or summary.get('expert_acceptance') != 'INCOMPLETE'
            or summary.get('secret_holdout') is not False
            or summary.get('reason') != 'DOWNSTREAM_SELECTION_NOT_MEASURED'
            or type(child) is not dict or type(attempt) is not dict):
        return 'INCOMPLETE'
    from qa.dark_retrieval import live as retrieval
    before = attempt.get('source_sha256_before')
    after = summary.get('source_sha256_after')
    if (attempt.get('status') != 'STARTED' or type(before) is not dict
            or set(before) != set(retrieval.SOURCES)
            or any(type(digest) is not str or not re.fullmatch(r'[0-9a-f]{64}', digest)
                   for digest in before.values())):
        return 'INCOMPLETE'
    if type(after) is not dict:
        return 'INCOMPLETE'
    if before != after:
        return 'FAIL'
    # Current code is required to recompute; mismatch means the historical
    # observation cannot be requalified now, not necessarily tampering.
    try:
        if retrieval.source_hashes() != before:
            return 'INCOMPLETE'
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return 'INCOMPLETE'
    try:
        retrieval.validate_result(child, before, after)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return 'FAIL'
    return 'PASS'


def _judge_audit(doc, baseline):
    """Check the machine audit artifact against the baseline identity."""
    if type(doc) is not dict:
        return 'INCOMPLETE', 'INCOMPLETE', None
    judge = 'FAIL' if doc.get('judge_status') == 'FAIL' else 'INCOMPLETE'
    if doc.get('audit_status') == 'FAIL':
        return 'FAIL', judge, None
    metrics = doc.get('metrics')
    if (doc.get('schema_version') != '1.0' or doc.get('audit_status') != 'PASS'
            or doc.get('human_calibration') != 'INCOMPLETE' or doc.get('product_acceptance') != 'INCOMPLETE'
            or type(metrics) is not dict):
        return 'INCOMPLETE', judge, None
    keys = ('planned', 'valid_responses', 'correct_known_labels', 'expected_defective', 'false_passes')
    if (not all(_count(metrics.get(k)) for k in keys) or not metrics['planned']
            or metrics.get('human_calibration') != 'INCOMPLETE'
            or not isinstance(metrics.get('result_sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', metrics['result_sha256'])):
        return 'INCOMPLETE', judge, None
    if (metrics['valid_responses'] > metrics['planned'] or metrics['correct_known_labels'] > metrics['valid_responses']
            or metrics['expected_defective'] > metrics['planned']
            or metrics['false_passes'] > metrics['expected_defective']):
        return 'FAIL', judge, None
    if metrics.get('historical_judge_status') != doc.get('judge_status'):
        return 'FAIL', judge, None
    if (metrics['false_passes'] and (doc.get('judge_status') != 'FAIL'
                                    or metrics.get('historical_judge_status') != 'FAIL')):
        return 'FAIL', 'FAIL', None
    try:
        baseline_judge = baseline['historical_observations']['judge']
        matched = (baseline['sources']['judge']['sha256'] == metrics['result_sha256']
                   and baseline_judge['status'] == doc.get('judge_status')
                   and baseline_judge['planned'] == metrics['planned']
                   and baseline_judge['valid_responses'] == metrics['valid_responses']
                   and baseline_judge['correct_known_labels'] == metrics['correct_known_labels']
                   and baseline_judge['false_passes'] == metrics['false_passes'])
    except (TypeError, KeyError):
        return 'INCOMPLETE', judge, None
    if not matched:
        return 'INCOMPLETE', judge, None
    return 'PASS', judge, {key: metrics[key] for key in keys}


def build_report(baseline=None, fake=None, live=None, regression=None,
                 retrieval_summary=None, retrieval_child=None, retrieval_attempt=None, judge_audit=None):
    baseline_status, judge = _baseline(baseline)
    fake_status, cases = _fake(fake)
    regression_status, tests = _regression(regression)
    audit_status, audit_judge, audit_metrics = _judge_audit(judge_audit, baseline)
    if audit_judge == 'FAIL':
        judge = 'FAIL'
    return {'schema_version': '1.0', 'baseline_capture': baseline_status,
            'deterministic_regression': regression_status, 'regression_tests_including_subtests': tests,
            'regression_provenance': 'historical_artifact_only_no_current_code_attestation',
            'adversarial_fake': fake_status, 'fake_variants': cases, 'adversarial_live': _live(live),
            'retrieval_measurement': _retrieval(retrieval_summary, retrieval_child, retrieval_attempt),
            'retrieval_selection': 'INCOMPLETE', 'retrieval_product_quality': 'INCOMPLETE',
            'judge_audit': audit_status, 'judge_audit_metrics': audit_metrics,
            'historical_judge': judge, 'human_acceptance': 'INCOMPLETE',
            'product_acceptance': 'INCOMPLETE', 'hidden_holdout': 'NOT_ESTABLISHED',
            'scope': 'Evidence summaries only, not independent validation or product acceptance.'}


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError('DUPLICATE_KEY')
        value[key] = item
    return value


def read_input(kind, relative, root=ROOT):
    """Read an allowlisted owned path, never arbitrary paths or linked inputs."""
    if kind not in _PATHS or not isinstance(relative, str) or not re.fullmatch(_PATHS[kind], relative):
        raise ValueError('INVALID_INPUT_PATH')
    root = Path(root).resolve(strict=True)
    member = root
    for part in PurePosixPath(relative).parts:
        member /= part
        if member.is_symlink() or (hasattr(member, 'is_junction') and member.is_junction()):
            raise ValueError('LINK_FORBIDDEN')
    if not member.resolve(strict=False).is_relative_to(root):
        raise ValueError('INVALID_INPUT_PATH')
    try:
        if member.stat().st_size > MAX_BYTES:
            raise ValueError('INPUT_TOO_LARGE')
        raw = member.read_bytes()
        if len(raw) > MAX_BYTES:
            raise ValueError('INPUT_TOO_LARGE')
        doc = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_pairs,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError('INVALID_NUMBER')))
    except FileNotFoundError:
        return None, None
    if type(doc) is not dict:
        raise ValueError('INVALID_INPUT')
    return doc, sha256(raw).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for kind in _PATHS:
        parser.add_argument('--' + kind.replace('_', '-'), dest=kind,
                            required=kind in {'baseline', 'fake', 'live', 'regression'})
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    documents, digests = {}, {}
    try:
        for kind in _PATHS:
            if getattr(args, kind) is None:
                documents[kind], digests[kind] = None, None
                continue
            try:
                documents[kind], digests[kind] = read_input(kind, getattr(args, kind))
            except (OSError, ValueError, UnicodeError, RecursionError):
                # Never emit external content or input paths.
                documents[kind], digests[kind] = None, None
        retrieval_names = [args.retrieval_summary, args.retrieval_child, args.retrieval_attempt]
        if (any(x is not None for x in retrieval_names)
                and (any(x is None for x in retrieval_names)
                     or len({PurePosixPath(x).parent for x in retrieval_names if x is not None}) != 1)):
            for kind in ('retrieval_summary', 'retrieval_child', 'retrieval_attempt'):
                documents[kind], digests[kind] = None, None
        report = build_report(**documents)
        report['input_sha256'] = digests
        write_new(report, args.output)
    except (ValueError, OSError):
        print('Invalid output or report failure', file=sys.stderr)
        return 2
    print(json.dumps({key: report[key] for key in ('baseline_capture', 'deterministic_regression',
          'adversarial_fake', 'adversarial_live', 'historical_judge', 'human_acceptance', 'product_acceptance')}))
    # Nonzero signals an incomplete component or observed failure to a caller.
    return 1 if any(report[k] != 'PASS' for k in ('baseline_capture', 'deterministic_regression',
                 'adversarial_fake', 'adversarial_live', 'historical_judge')) else 0


if __name__ == '__main__':
    raise SystemExit(main())
