"""Capture an append-only inventory of existing, scoped historical QA evidence.

This does not rerun any model, verify every referenced evidence file, or grant
product approval. Only a fixed allowlist is read and no raw payload is emitted.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
VERSION = '1.0.0'
SOURCES = {
    'capture_program': 'qa/dark_factory/baseline.py',
    'policy': 'qa/strategy/quality-policy.json',
    'progress': 'qa/progress.json',
    'regression': 'verification/qa-judge-v2-2026-10-25/regression-qualified/unit-verification.json',
    'llm': 'verification/qa-step-07-2026-10-20/pilot-first/result.json',
    'retrieval': 'verification/qa-step-06-2026-10-19/model-final/ranking/result.json',
    'judge': 'verification/qa-judge-v2-2026-10-25/live-restart/result.json',
    'judge_qualification': 'verification/qa-judge-v2-2026-10-25/qualification.json',
    'aml_manifest': 'qa/datasets/aml-v1/manifest.json',
    'retrieval_manifest': 'qa/datasets/retrieval-v1/manifest.json',
    'judge_manifest': 'qa/datasets/judge-v1/manifest.json',
    'judge_rubric': 'qa/judge/rubric-v1.json',
}
MAX_BYTES = 8 * 1024 * 1024


def _strict_json(raw: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result

    def invalid(_value):
        raise ValueError('nonfinite number')

    return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=pairs,
                      parse_constant=invalid)


def _read_member(root: Path, relative: str) -> bytes:
    parts = PurePosixPath(relative).parts
    if not parts or any(p in {'.', '..'} or ':' in p for p in parts) or '\\' in relative:
        raise ValueError('unsafe path')
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('links not allowed')
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('missing or unsafe source')
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('source too large')
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise ValueError('source too large')
    return raw


def _extract(documents: dict) -> dict:
    """Extract selected historical observations; reject malformed conditions."""
    regression, llm = documents['regression'], documents['llm']
    retrieval, judge = documents['retrieval'], documents['judge']
    overall = judge['summary']['overall']
    progress = documents['progress']['judge_revision']
    if regression['status'] not in {'PASS', 'FAIL'} or llm['status'] not in {'PASS', 'FAIL'}:
        raise ValueError('invalid historical status')
    if judge['status'] not in {'PASS', 'FAIL', 'INCOMPLETE'}:
        raise ValueError('invalid judge status')
    if progress['judge_status'] != judge['status']:
        raise ValueError('conflicting judge observations')
    summary = llm['summary']
    for value in (regression['tests'], regression['failures'], regression['errors'],
                  regression['skipped'], regression['pytest_exit_code'],
                  regression['collected_tests'], regression['additional_subtests'],
                  summary['planned_repeat_count'], summary['observed_repeat_count'],
                  summary['missing_repeat_count'], summary['planned_case_count'],
                  summary['observed_case_count'], summary['observed_independence_family_count'],
                  llm['repeats_per_draft_case'], llm['draft_observation_count'],
                  llm['control_observation_count'], overall['planned'],
                  overall['valid_responses'], overall['all_planned_accuracy']['numerator'],
                  overall['confusion']['fn']):
        if type(value) is not int or value < 0:
            raise ValueError('invalid observation count')
    if not regression['tests'] or not overall['planned']:
        raise ValueError('empty observations')
    if regression['status'] == 'PASS':
        if (any(regression[k] for k in ('failures', 'errors', 'skipped', 'pytest_exit_code'))
                or not regression['collected_tests']
                or regression['tests'] != regression['collected_tests'] + regression['additional_subtests']):
            raise ValueError('conflicting regression observation')
    if llm['status'] == 'PASS':
        expected_drafts = {'QA-S01', 'QA-S02', 'QA-S03', 'QA-S04', 'QA-S14'}
        expected_controls = {f'QA-S{n:02}' for n in range(5, 13)}
        cases, controls = summary['cases'], llm['controls']
        if (summary['status'] != 'PASS' or llm['full_planned_matrix'] is not True
                or llm['collection_complete'] is not True
                or llm['source_files_unchanged_during_run'] is not True
                or llm['model_identity_stable'] is not True or llm['template_failures'] != []
                or type(cases) is not list or type(controls) is not list
                or not summary['planned_repeat_count'] or not summary['planned_case_count']
                or not summary['observed_independence_family_count']
                or not llm['repeats_per_draft_case']
                or summary['observed_repeat_count'] != summary['planned_repeat_count']
                or summary['missing_repeat_count'] != 0
                or summary['observed_case_count'] != summary['planned_case_count']
                or summary['planned_repeat_count'] != summary['planned_case_count'] * llm['repeats_per_draft_case']
                or llm['draft_observation_count'] != summary['observed_repeat_count']
                or summary['planned_case_count'] != len(expected_drafts)
                or len(cases) != len(expected_drafts)
                or len(controls) != len(expected_controls)
                or llm['control_observation_count'] != len(controls)
                or type(summary.get('fact_violation_count')) is not int or summary['fact_violation_count'] != 0
                or any(type(case) is not dict or case.get('status') != 'PASS'
                       or type(case.get('planned_repeats')) is not int
                       or type(case.get('observed_repeats')) is not int
                       or case.get('planned_repeats') != llm['repeats_per_draft_case']
                       or case.get('observed_repeats') != llm['repeats_per_draft_case']
                       or case.get('missing_repeat_indices') != []
                       or type(case.get('fact_violation_count')) is not int or case['fact_violation_count'] != 0
                       or type(case.get('status_counts')) is not dict
                       or case['status_counts'] != {'PASS': llm['repeats_per_draft_case'], 'FAIL': 0, 'INCOMPLETE': 0}
                       or not isinstance(case.get('independence_id'), str) or not case['independence_id']
                       for case in cases)
                or {case.get('case_id') for case in cases} != expected_drafts
                or len({case['independence_id'] for case in cases}) != summary['observed_independence_family_count']
                or any(type(control) is not dict or control.get('status') != 'PASS'
                       or control.get('issues') != [] for control in controls)
                or {control.get('case_id') for control in controls} != expected_controls):
            raise ValueError('incomplete or conflicting LLM observation')
        for metric, count in (('repeat_checks_pass_rate', summary['planned_repeat_count']),
                              ('case_checks_pass_rate', summary['planned_case_count'])):
            rate = summary[metric]
            if (type(rate) is not dict or type(rate.get('numerator')) is not int
                    or type(rate.get('denominator')) is not int
                    or rate['numerator'] != count or rate['denominator'] != count
                    or type(rate.get('value')) not in (int, float) or rate['value'] != 1):
                raise ValueError('conflicting LLM pass rate')
        raw = llm['raw_attempt_summary']
        if (type(raw) is not dict or type(raw.get('rows')) is not list
                or raw.get('observations') != summary['observed_repeat_count']
                or len(raw['rows']) != summary['observed_repeat_count']):
            raise ValueError('incomplete LLM attempt records')
        seen_attempts = set()
        for row in raw['rows']:
            if (type(row) is not dict or row.get('case_id') not in expected_drafts
                    or type(row.get('repeat_index')) is not int
                    or not 1 <= row['repeat_index'] <= llm['repeats_per_draft_case']
                    or type(row.get('http_attempts')) is not int or not 1 <= row['http_attempts'] <= 2
                    or type(row.get('first_attempt_valid')) is not bool
                    or type(row.get('repair_attempted')) is not bool
                    or row['repair_attempted'] != (row['http_attempts'] > 1)
                    or row.get('final_status') != 'draft'):
                raise ValueError('invalid LLM attempt record')
            key = (row['case_id'], row['repeat_index'])
            if key in seen_attempts:
                raise ValueError('duplicate LLM attempt record')
            seen_attempts.add(key)
        if (type(raw.get('first_attempt_valid_count')) is not int
                or type(raw.get('repair_attempted_count')) is not int
                or type(raw.get('total_http_attempts')) is not int
                or raw['first_attempt_valid_count'] != sum(r['first_attempt_valid'] for r in raw['rows'])
                or raw['repair_attempted_count'] != sum(r['repair_attempted'] for r in raw['rows'])
                or raw['total_http_attempts'] != sum(r['http_attempts'] for r in raw['rows'])):
            raise ValueError('conflicting LLM attempt counts')
    if overall['valid_responses'] > overall['planned'] or overall['all_planned_accuracy']['numerator'] > overall['planned']:
        raise ValueError('invalid judge denominator')
    judge_status = 'FAIL' if judge['status'] == 'FAIL' else 'INCOMPLETE'
    return {
        'deterministic_historical': {
            'status': regression['status'], 'tests_including_subtests': regression['tests'],
            'collected_tests': regression['collected_tests'], 'additional_subtests': regression['additional_subtests'],
            'scope': 'Historical isolated checks; not a current run or live model evaluation.'},
        'live_llm_historical': {
            'status': llm['status'], 'planned_repeats': llm['summary']['planned_repeat_count'],
            'observed_repeats': llm['summary']['observed_repeat_count'],
            'independent_families': llm['summary']['observed_independence_family_count'],
            'scope': 'Historical measured cases only; not proof of broad model utility.'},
        'retrieval_quality': {
            'status': 'INCOMPLETE', 'quality_acceptance': retrieval['quality_acceptance'],
            'primary_query_count': retrieval['primary_query_count'],
            'scope': 'Measurement status does not establish accepted ranking or domain validity.'},
        'judge': {
            'status': judge_status, 'observed_historical_status': judge['status'],
            'planned': overall['planned'], 'valid_responses': overall['valid_responses'],
            'correct_known_labels': overall['all_planned_accuracy']['numerator'],
            'false_passes': overall['confusion']['fn'],
            'scope': 'Advisory only; FAIL retained; no human calibration inferred.'},
        'human_calibration': {'status': 'INCOMPLETE', 'scope': 'No independent human assessment supplied to this snapshot.'},
        'product_acceptance': {'status': 'INCOMPLETE', 'scope': 'No product acceptance established by this snapshot.'},
    }


def capture(root: Path = ROOT) -> dict:
    root = Path(root).resolve(strict=True)
    documents, inventory, issues = {}, {}, []
    for name, relative in SOURCES.items():
        try:
            raw = _read_member(root, relative)
            if name != 'capture_program':
                document = _strict_json(raw)
                if not isinstance(document, dict):
                    raise ValueError('invalid object')
                documents[name] = document
            inventory[name] = {'path': relative, 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
        except (OSError, ValueError, UnicodeError, RecursionError):
            issues.append({'source': name, 'status': 'INCOMPLETE'})
    # Even a partial capture retains an explicitly observed failure; missing or
    # unvalidated source material must never turn any scope into a pass.
    partial_judge = documents.get('judge', {}).get('status')
    historical = {
        'deterministic_historical': {'status': 'INCOMPLETE'},
        'live_llm_historical': {'status': 'INCOMPLETE'},
        'retrieval_quality': {'status': 'INCOMPLETE'},
        'judge': {'status': 'FAIL' if partial_judge == 'FAIL' else 'INCOMPLETE'},
        'human_calibration': {'status': 'INCOMPLETE'},
        'product_acceptance': {'status': 'INCOMPLETE'},
    }
    if not issues:
        try:
            historical = _extract(documents)
        except (KeyError, TypeError, ValueError):
            issues.append({'source': 'observation_validation', 'status': 'INCOMPLETE'})
    return {
        'schema_version': VERSION,
        'captured_at_utc': datetime.now(timezone.utc).isoformat(),
        'capture_status': 'PASS' if not issues else 'INCOMPLETE',
        'source_count_expected': len(SOURCES), 'source_count_captured': len(inventory),
        'sources': inventory, 'issues': issues, 'historical_observations': historical,
        'current_product_status': 'INCOMPLETE',
        'scope': 'Fixed inventory of historical artifacts, not a new test run, integrity attestation, or product approval.',
    }


def write_new(result: dict, relative: str, root: Path = ROOT) -> Path:
    """Write one new artifact beneath var/qa/dark-factory; never overwrite."""
    root = Path(root).resolve(strict=True)
    if not re.fullmatch(r'var/qa/dark-factory/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\.json', relative):
        raise ValueError('output must be a named JSON under var/qa/dark-factory')
    parent = root
    for component in PurePosixPath(relative).parts[:-1]:
        parent = parent / component
        if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
            raise ValueError('output links forbidden')
        parent.mkdir(exist_ok=True)
        if not parent.resolve().is_relative_to(root):
            raise ValueError('output escapes root')
    output = parent / PurePosixPath(relative).name
    with output.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='New var/qa/dark-factory/<name>.json')
    args = parser.parse_args(argv)
    try:
        result = capture()
        output = write_new(result, args.output)
    except (OSError, ValueError) as exc:
        print('Capture failed: ' + type(exc).__name__, file=sys.stderr)
        return 2
    print(json.dumps({'capture_status': result['capture_status'],
                      'output': output.relative_to(ROOT).as_posix(),
                      'current_product_status': result['current_product_status']}))
    return 0 if result['capture_status'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
