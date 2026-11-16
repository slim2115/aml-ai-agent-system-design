"""Summarise separate open QA measurements without declaring product acceptance.

All evidence is local, not an independent attestation. Each component carries
its own scope and validation status; a successful measurement is not a safety
or quality gate. No model is called. Missing evidence stays INCOMPLETE.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from qa.dark_factory.baseline import _read_member, _strict_json, write_new

NAME = r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}'
PATTERNS = {
    'base': rf'var/qa/dark-factory/{NAME}\.json',
    'gate': rf'var/qa/dark-factory/{NAME}\.json',
    'editions': rf'var/qa/dark-factory/{NAME}\.json',
    'v3': rf'var/qa/dark-factory-v3/{NAME}',
    'downstream': rf'var/qa/dark-downstream/{NAME}',
    'judge_injection': rf'var/qa/dark-judge-injection/{NAME}',
    'policy_pair': rf'var/qa/dark-downstream/{NAME}',
}
BASE_MEASUREMENTS = ('baseline_capture', 'deterministic_regression',
                     'adversarial_fake', 'adversarial_live',
                     'retrieval_measurement', 'judge_audit')


def _document(relative):
    raw = _read_member(ROOT, relative)
    value = _strict_json(raw)
    if type(value) is not dict:
        raise ValueError('OBJECT_REQUIRED')
    return value, sha256(raw).hexdigest()


def _safe_name(kind, path):
    if type(path) is not str or re.fullmatch(PATTERNS[kind], path) is None:
        raise ValueError('INVALID_EVIDENCE_PATH')
    return path


def _empty(scope):
    return {'evidence_status': 'INCOMPLETE', 'scope': scope}


def _recorded_failure(doc):
    """Retain explicit failures even when their envelope cannot be validated.

    This is a recorded finding, not validation of the contradictory evidence.
    Inspect only known status fields, never arbitrary text in the artifact.
    """
    if type(doc) is not dict:
        return False
    rows = doc.get('cases')
    return doc.get('status') == 'FAIL' or (type(rows) is list and any(
        type(row) is dict and row.get('status') == 'FAIL' for row in rows))


def base_report(doc):
    """Previous report is a saved summary, not requalification of its inputs."""
    row = _empty('Historical summary only; inputs are not requalified here.')
    row.update({field: 'INCOMPLETE' for field in (*BASE_MEASUREMENTS, 'historical_judge')})
    if type(doc) is not dict:
        return row
    # Keep an explicitly recorded failure even if another part is malformed.
    for field in (*BASE_MEASUREMENTS, 'historical_judge'):
        if doc.get(field) == 'FAIL':
            row[field] = 'FAIL'
    if (doc.get('schema_version') != '1.0' or doc.get('historical_judge') not in ('FAIL', 'INCOMPLETE')
            or any(doc.get(field) not in ('PASS', 'FAIL', 'INCOMPLETE') for field in BASE_MEASUREMENTS)
            or doc.get('human_acceptance') != 'INCOMPLETE'
            or doc.get('product_acceptance') != 'INCOMPLETE'
            or doc.get('hidden_holdout') != 'NOT_ESTABLISHED'):
        return row
    row.update({field: doc[field] for field in (*BASE_MEASUREMENTS, 'historical_judge')})
    row['evidence_status'] = 'PASS'
    return row


def gate_report(doc, recomputed):
    row = _empty('Historical synthetic Judge and programmatic gates; no model call.')
    if type(doc) is dict and doc.get('historical_judge_status') == 'FAIL':
        row['historical_judge'] = 'FAIL'
    if (type(doc) is not dict or type(recomputed) is not dict
            or doc.get('status') != 'PASS' or recomputed.get('status') != 'PASS'
            or doc != recomputed or doc.get('historical_judge_status') != 'FAIL'):
        return row
    counts = doc['counts']
    rate = counts['gate_fail_judge_pass_rate']
    row.update(evidence_status='PASS', historical_judge='FAIL',
               gate_fail_judge_pass=rate, planned_single_cases=counts['planned_single_cases'],
               planned_pair_observations=counts['planned_pair_observations'],
               provenance='Recomputed from locally retained records; not an independent signature.')
    return row


def editions_report(doc, cases):
    row = _empty('Open synthetic edition observations; fake embeddings, no downstream decision.')
    if _recorded_failure(doc):
        row['recorded_observed_status'] = 'FAIL'
    if type(doc) is not dict or doc.get('status') not in ('PASS', 'FAIL', 'INCOMPLETE'):
        return row
    if doc['status'] == 'FAIL':
        row['observed_status'] = 'FAIL'
    if (doc.get('embedding') != 'FAKE_EQUAL_VECTORS' or doc.get('llm') != 'NOT_RUN'
            or doc.get('downstream_selection') != 'NOT_OBSERVED'
            or doc.get('legal_applicability') != 'NOT_ESTABLISHED'
            or doc.get('human_acceptance') != 'INCOMPLETE'
            or doc.get('product_acceptance') != 'INCOMPLETE'
            or doc.get('hidden_holdout') != 'NOT_ESTABLISHED'
            or type(doc.get('cases')) is not list or len(doc['cases']) != len(cases)
            or doc.get('planned') != len(cases) or len(cases) != 4):
        return row
    statuses = []
    for expected, actual in zip(cases, doc['cases']):
        if (type(actual) is not dict or actual.get('case_id') != expected['id']
                or actual.get('expected') != expected['expected']
                or type(actual.get('observed')) is not dict):
            return row
        measured = 'PASS' if actual['observed'] == expected['expected'] else 'FAIL'
        if actual.get('status') != measured:
            return row
        statuses.append(measured)
    measured = 'FAIL' if 'FAIL' in statuses else 'PASS'
    if measured != doc['status']:
        return row
    row.update(evidence_status='PASS', observed_status=measured, case_count=len(statuses),
               provenance='Saved observations checked against current oracle; original code identity not recorded.')
    return row


def v3_report(doc, current_sources, snapshot_hashes):
    row = _empty('Three open synthetic runtime cases; fake embeddings and template.')
    if _recorded_failure(doc):
        row['recorded_observed_status'] = 'FAIL'
    if type(doc) is not dict:
        return row
    if doc.get('status') == 'FAIL':
        row['observed_status'] = 'FAIL'
    rows = doc.get('cases')
    ids = ('DF3-NO-WINDOW', 'DF3-CURRENCIES', 'DF3-FOREIGN-CARD')
    if (doc.get('status') not in ('PASS', 'FAIL', 'INCOMPLETE')
            or doc.get('embedding') != 'FAKE_EQUAL_VECTORS' or doc.get('engine') != 'template'
            or doc.get('transport') != 'FAKE_NOT_CALLED' or doc.get('real_llm') != 'NOT_RUN'
            or doc.get('sme_acceptance') != 'INCOMPLETE' or doc.get('human_acceptance') != 'INCOMPLETE'
            or doc.get('product_acceptance') != 'INCOMPLETE' or doc.get('hidden_holdout') != 'NOT_ESTABLISHED'
            or doc.get('source_sha256_before') != current_sources
            or doc.get('source_sha256_after') != current_sources
            or doc.get('catalog_sha256') != current_sources.get('cases.json')
            or type(rows) is not list or len(rows) != 3):
        return row
    statuses = []
    for expected_id, item in zip(ids, rows):
        if (type(item) is not dict or item.get('id') != expected_id
                or item.get('status') not in ('PASS', 'FAIL', 'INCOMPLETE')
                or type(item.get('issues')) is not list
                or item.get('snapshot_sha256') != snapshot_hashes.get(expected_id)):
            return row
        if (item['status'] == 'PASS') != (item['issues'] == []):
            return row
        statuses.append(item['status'])
    measured = 'FAIL' if 'FAIL' in statuses else 'INCOMPLETE' if 'INCOMPLETE' in statuses else 'PASS'
    if doc['status'] != measured:
        return row
    row.update(evidence_status='PASS', observed_status=measured, case_count=3,
               provenance='Snapshot and runner/catalog hashes checked; saved outcomes not replayed.')
    return row


def downstream_report(summary, child, before, current, validator):
    row = _empty('One open fixture, real local embedding, template; synthetic policy mutations.')
    if _recorded_failure(summary) or _recorded_failure(child):
        row['recorded_observed_status'] = 'FAIL'
    if type(summary) is dict and summary.get('policy_safety') == 'FAIL_FOR_CONTROLLED_MUTATION':
        row['recorded_policy_safety'] = 'FAIL_FOR_CONTROLLED_MUTATION'
    if type(summary) is not dict or type(child) is not dict or type(before) is not dict or before != current:
        return row
    try:
        measured = validator(child, before, current)
    except (ValueError, TypeError, KeyError, OSError, AttributeError):
        return row
    safety = child.get('policy_safety')
    if (summary.get('status') != measured or summary.get('policy_safety') != safety
            or summary.get('semantic_relevance') != 'NOT_ESTABLISHED'
            or summary.get('product_quality') != 'INCOMPLETE'
            or summary.get('human_acceptance') != 'INCOMPLETE'
            or summary.get('hidden_holdout') != 'NOT_ESTABLISHED'):
        return row
    row.update(evidence_status='PASS', measurement_status=measured, policy_safety=safety,
               semantic_relevance='NOT_ESTABLISHED',
               provenance='Saved child rows recomputed; local hashes are not independent attestation.')
    return row


def injection_report(summary, measured, rows):
    row = _empty('One open Judge case, neutral and attack, first attempts only; not generator.')
    if type(summary) is dict and summary.get('historical_judge_status') == 'FAIL':
        row['historical_judge'] = 'FAIL'
    if (type(summary) is not dict or measured not in ('PASS', 'INCOMPLETE')
            or summary.get('measurement_status') != measured or summary.get('cases') != rows
            or summary.get('historical_judge_status') != 'FAIL'
            or summary.get('human_calibration') != 'INCOMPLETE'
            or summary.get('product_acceptance') != 'INCOMPLETE'
            or summary.get('hidden_holdout') != 'NOT_ESTABLISHED'):
        return row
    row.update(evidence_status='PASS', measurement_status=measured, historical_judge='FAIL',
               cases=[{k: item.get(k) for k in ('variant', 'delivery', 'contract', 'verdict',
                                               'judge_against_known_defect')} for item in rows],
               provenance='First saved wire and responses revalidated locally; no independent attestation.')
    return row


def policy_pair_report(path):
    """Recheck both arms without running models. Explicit failures survive missing evidence."""
    from qa.dark_downstream.policy_pair import replay
    from qa.dark_retrieval.matrix import digest as canonical_digest
    keys = ('baseline_status', 'candidate_status', 'measurement_status', 'mutation_check')
    row = {**_empty('One synthetic structuring incident, three policy pairs; template engine.'),
           **dict.fromkeys(keys, 'INCOMPLETE'), 'artifact': path}
    summary = verified = None
    invalid = (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, StopIteration)
    try:
        summary, row['artifact_sha256'] = _document(path + '/summary.json')
    except invalid:
        pass
    try:
        verified = replay(ROOT / path)
    except invalid:
        pass
    for doc in (summary, verified):
        if type(doc) is not dict:
            continue
        for key in keys:
            if doc.get(key) == 'FAIL':
                row[key] = 'FAIL'
        pairs = doc.get('pairs')
        for pair in pairs if type(pairs) is list else ():
            if type(pair) is not dict:
                continue
            for arm in ('baseline', 'candidate'):
                decision = pair.get(arm)
                if type(decision) is dict and decision.get('decision') == 'FAIL':
                    row[arm + '_status'] = 'FAIL'
            if pair.get('mutation_check') == 'FAIL':
                row['mutation_check'] = 'FAIL'
    if (type(verified) is dict and type(summary) is dict and canonical_digest(summary) == canonical_digest(verified)
            and verified.get('measurement_status') == 'PASS'):
        row.update({key: verified[key] for key in keys})
        row.update(evidence_status='PASS', pair_count=len(verified['pairs']),
                   regressions=sum(p['baseline']['decision'] == 'PASS' and p['candidate']['decision'] == 'FAIL'
                                   for p in verified['pairs']))
    return row


def build(paths):
    judge_version = paths.get('judge_injection_version', '1')
    if judge_version not in ('1', '2'):
        raise ValueError('INVALID_JUDGE_INJECTION_VERSION')
    result = {'schema_version': '2.0', 'human_acceptance': 'INCOMPLETE',
              'product_acceptance': 'INCOMPLETE', 'hidden_holdout': 'NOT_ESTABLISHED',
              'scope': 'Separate local evidence, not an independent attestation or a product gate.',
              'components': {}}
    for kind in PATTERNS:
        path = paths.get(kind)
        if path is None:
            result['components'][kind] = _empty('No evidence supplied.')
            continue
        _safe_name(kind, path)
        if kind == 'policy_pair':
            result['components'][kind] = policy_pair_report(path)
            continue
        recorded = {}
        try:
            if kind == 'base':
                doc, digest = _document(path)
                for field in (*BASE_MEASUREMENTS, 'historical_judge'):
                    if doc.get(field) == 'FAIL':
                        recorded[field] = 'FAIL'
                row = base_report(doc)
            elif kind == 'gate':
                from qa.dark_factory.gate_cross import build as recompute
                doc, digest = _document(path)
                if doc.get('historical_judge_status') == 'FAIL':
                    recorded['historical_judge'] = 'FAIL'
                row = gate_report(doc, recompute())
            elif kind == 'editions':
                from qa.dark_editions.probe import cases
                doc, digest = _document(path)
                if _recorded_failure(doc):
                    recorded['recorded_observed_status'] = 'FAIL'
                if doc.get('status') == 'FAIL':
                    recorded['observed_status'] = 'FAIL'
                row = editions_report(doc, cases())
            elif kind == 'v3':
                doc, digest = _document(path + '/result.json')
                if _recorded_failure(doc):
                    recorded['recorded_observed_status'] = 'FAIL'
                if doc.get('status') == 'FAIL':
                    recorded['observed_status'] = 'FAIL'
                source = 'qa/datasets/dark-factory-v3/'
                current = {name: sha256(_read_member(ROOT, source + name)).hexdigest()
                           for name in ('probe.py', 'cases.json')}
                names = ('cases.json', 'clients.json', 'cards.json', 'transactions.json',
                         'rules.json', 'manifest.json', 'policy.json')
                snapshots = {case: {name: sha256(_read_member(ROOT, f'{path}/{case}/{name}')).hexdigest()
                                    for name in names}
                             for case in ('DF3-NO-WINDOW', 'DF3-CURRENCIES', 'DF3-FOREIGN-CARD')}
                row = v3_report(doc, current, snapshots)
            elif kind == 'downstream':
                from qa.dark_downstream.live import source_hashes, validate_result
                doc, digest = _document(path + '/summary.json')
                if _recorded_failure(doc):
                    recorded['recorded_observed_status'] = 'FAIL'
                if doc.get('policy_safety') == 'FAIL_FOR_CONTROLLED_MUTATION':
                    recorded['recorded_policy_safety'] = 'FAIL_FOR_CONTROLLED_MUTATION'
                child, _ = _document(path + '/child-result.json')
                if _recorded_failure(child):
                    recorded['recorded_observed_status'] = 'FAIL'
                attempt, _ = _document(path + '/attempt.json')
                if attempt.get('status') != 'STARTED':
                    raise ValueError('INVALID_ATTEMPT')
                row = downstream_report(doc, child, attempt.get('source_sha256_before'), source_hashes(), validate_result)
            else:
                if judge_version == '2':
                    from qa.dark_judge_injection.live_v2 import source_hashes, validate_saved
                else:
                    from qa.dark_judge_injection.live import source_hashes, validate_saved
                doc, digest = _document(path + '/summary.json')
                if doc.get('historical_judge_status') == 'FAIL':
                    recorded['historical_judge'] = 'FAIL'
                attempt, _ = _document(path + '/attempt.json')
                if attempt.get('status') != 'STARTED':
                    raise ValueError('INVALID_ATTEMPT')
                before = attempt.get('source_sha256_before')
                current = source_hashes()
                if type(before) is not dict or before != current:
                    raise ValueError('SOURCE_CHANGED')
                # Validate every retained member for links/size before passing the directory to the reader.
                files = ('child-result.json', 'neutral-start.json', 'neutral-result.json',
                         'attack-start.json', 'attack-result.json')
                if judge_version == '2':
                    files += ('neutral-raw.json', 'attack-raw.json')
                for name in files:
                    _document(path + '/' + name)
                measured, rows = validate_saved(ROOT / path, before, current)
                row = injection_report(doc, measured, rows)
                row['runner_version'] = judge_version
            row['artifact'] = path
            row['artifact_sha256'] = digest
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, StopIteration):
            row = _empty('Evidence missing, changed or cannot be rechecked locally.')
            row['artifact'] = path
            row.update(recorded)
        result['components'][kind] = row
    return result


def evidence_exit_code(result):
    """Return 1 for recorded failures, 2 for incomplete evidence, never a product approval."""
    if (type(result) is not dict or type(result.get('components')) is not dict
            or not result['components']):
        return 2
    rows = list(result['components'].values())
    malformed = any(type(row) is not dict or ('cases' in row and
                    (type(row['cases']) is not list or any(type(case) is not dict for case in row['cases'])))
                    for row in rows)
    rows = [row for row in rows if type(row) is dict]
    if any(row.get('historical_judge') == 'FAIL' or row.get('observed_status') == 'FAIL'
           or row.get('recorded_observed_status') == 'FAIL'
           or any(row.get(field) == 'FAIL' for field in BASE_MEASUREMENTS)
           or row.get('policy_safety') == 'FAIL_FOR_CONTROLLED_MUTATION'
           or row.get('recorded_policy_safety') == 'FAIL_FOR_CONTROLLED_MUTATION'
           or row.get('measurement_status') == 'FAIL'
           or any(row.get(field) == 'FAIL' for field in ('baseline_status', 'candidate_status', 'mutation_check'))
           or any(type(case) is dict and case.get('judge_against_known_defect') == 'FAIL'
                  for case in (row.get('cases') if type(row.get('cases')) is list else []))
           for row in rows):
        return 1
    # Each present measurement status must be explicitly PASS. A valid model
    # response can still be an abstention and must not count as a successful
    # known-defect judgement. Product/human and legal applicability remain
    # explicitly out of scope for this exit code.
    fields = ('historical_judge', 'observed_status', 'measurement_status', 'baseline_status',
              'candidate_status', 'mutation_check', *BASE_MEASUREMENTS)
    if malformed or any(row.get('evidence_status') != 'PASS'
           or any(field in row and row[field] != 'PASS' for field in fields)
           or ('policy_safety' in row and row['policy_safety'] != 'PASS')
           or any(case.get('judge_against_known_defect') != 'PASS' for case in row.get('cases', []))
           for row in rows):
        return 2
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for kind in PATTERNS:
        parser.add_argument('--' + kind.replace('_', '-'), dest=kind)
    parser.add_argument('--judge-injection-version', choices=('1', '2'), default='1',
                        help='Saved Judge runner format (default: 1).')
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        result = build(vars(args))
        output = write_new(result, args.output)
    except (OSError, ValueError):
        return 2
    print(f"Evidence summary saved: {output.relative_to(ROOT).as_posix()}")
    return evidence_exit_code(result)


if __name__ == '__main__':
    raise SystemExit(main())
