"""Small, owned controls for real evaluator functions; no services or models.

The hand-built inputs are open synthetic engineering controls. Expected results
live in catalog-v1.json and are never read or calculated by observe(). Decision
describes the observed evaluator result, not qualification of the control or AML
acceptance. Only a named invalid-input control catches its exact expected error.
"""
from copy import deepcopy
from hashlib import sha256
import json

from qa import reporting, retrieval_checks, scenario_checks
from qa.judge import contracts as judge_contracts, scoring as judge_scoring
from qa.model_eval import checks as model_checks
from qa.scenarios import Scenario
from src.evaluation import evaluate_bundle
from src.evaluation.gates import SCHEMA, TRACEABILITY
from src.evaluation.models import BundleInputError


CONTROL_IDS = tuple(
    f'{prefix}-{number:02}'
    for prefix, size in (('CORE', 5), ('SCENARIO', 4), ('MODEL', 4),
                         ('RETRIEVAL', 5), ('JUDGE', 5), ('REPORTING', 6))
    for number in range(1, size + 1)
)


def _citation():
    return {'rule_id': 'QUAL-RULE', 'rule_version': 'v1', 'source_ref': 'Synthetic qualification rule',
            'chunk_id': 'QUAL-CHUNK', 'content_sha256': sha256(b'Synthetic qualification rule').hexdigest()}


def _sample():
    """One hand-calculated transaction and six explicit facts, not renderer output."""
    window = {'start_inclusive': '2026-01-01T00:00:00Z', 'end_exclusive': '2026-02-01T00:00:00Z',
              'as_of': '2026-02-01T00:00:00Z'}
    context = {
        'schema_version': '1.0', 'incident_id': 'INC-000901', 'client_id': 'QUAL-CLIENT',
        'snapshot': {'snapshot_id': 'QUAL-SNAPSHOT', 'captured_at': '2026-02-01T00:00:00Z',
                     'sources': [{'source': 'synthetic', 'version': 'v1', 'content_sha256': 'b' * 64}]},
        'window': window, 'policy_version': 'v1', 'data_completeness': 'full',
        'consistency': 'consistent', 'missing_fields': [],
        'client': {'client_id': 'QUAL-CLIENT', 'risk_category': 'low', 'kyc_status': 'verified',
                   'client_since': '2020-01-01'}, 'cards': [],
        'transactions': [{'tx_id': 'QUAL-TX', 'client_id': 'QUAL-CLIENT', 'card_id': None,
                          'amount': '10.00', 'currency': 'RUB', 'counterparty': 'Synthetic counterparty',
                          'purpose': 'Synthetic transfer', 'channel': 'transfer', 'status': 'completed',
                          'timestamp': '2026-01-10T00:00:00Z'}],
    }
    rule = {'citation': _citation(), 'rule_text': 'Synthetic qualification rule'}
    case = {'id': 'QUAL-CASE', 'independence_id': 'QUAL-FAMILY', 'category': 'A',
            'incident_id': 'INC-000901', 'expected_status': 'draft',
            'forbidden_outputs': ['qualification-forbidden-claim'],
            'expected_retrieval': {'namespace': 'rule_id', 'ids': ['QUAL-RULE']}, 'context': context}
    scenario = Scenario.model_validate({
        'case': case, 'title': 'Open evaluator control', 'rationale': 'One hand-calculated transaction',
        'split': 'calibration', 'fixture': 'fixtures/QUAL', 'mode': 'demo', 'engine': 'template',
        'dependency': 'healthy', 'expected_model_calls': 0, 'source_mutations': [],
        'independence_basis': 'One open synthetic family; never an independent holdout',
        'excluded_transaction_ids': [], 'forbidden_prompt_strings': [],
        'applicable_checks': ['behavior', 'facts', 'rule_selection'],
        'golden': {'fact_count': 6, 'transaction_ids': ['QUAL-TX'],
                   'sums': [{'currency': 'RUB', 'value': '10.00', 'tx_ids': ['QUAL-TX']}], 'rules': [rule]},
    })
    facts = []
    for field, text in (
        ('risk_category', 'Категория риска в профиле: low.'),
        ('kyc_status', 'Статус KYC в профиле: verified.'),
        ('client_since', 'Дата начала отношений в профиле: 2020-01-01.'),
    ):
        facts.append({'fact': text, 'sources': [], 'evidence': [{'kind': 'profile',
            'snapshot_id': 'QUAL-SNAPSHOT', 'client_id': 'QUAL-CLIENT', 'field': field}]})
    facts.append({'fact': 'Транзакция QUAL-TX: сумма 10.00 RUB; время 2026-01-10T00:00:00Z; статус completed; канал transfer.',
        'sources': [], 'evidence': [{'kind': 'transaction', 'snapshot_id': 'QUAL-SNAPSHOT',
            'tx_id': 'QUAL-TX', 'field': field} for field in ('amount', 'currency', 'timestamp', 'status', 'channel')]})
    facts.append({'fact': 'Количество всех транзакций в контексте за окно [2026-01-01T00:00:00Z, 2026-02-01T00:00:00Z), включая все статусы: 1.',
        'sources': [], 'evidence': [{'kind': 'count', 'snapshot_id': 'QUAL-SNAPSHOT',
            'tx_ids': ['QUAL-TX'], 'window': deepcopy(window), 'value': 1}]})
    facts.append({'fact': 'Сумма всех транзакций в контексте в валюте RUB за окно [2026-01-01T00:00:00Z, 2026-02-01T00:00:00Z), включая все статусы: 10.00 RUB.',
        'sources': [], 'evidence': [{'kind': 'sum', 'snapshot_id': 'QUAL-SNAPSHOT',
            'tx_ids': ['QUAL-TX'], 'field': 'amount', 'window': deepcopy(window), 'currency': 'RUB', 'value': '10.00'}]})
    result = {'schema_version': '1.0', 'status': 'draft', 'incident_id': 'INC-000901',
              'report_id': 'QUAL-REPORT', 'report_version': 1, 'generated_at': '2026-02-01T00:00:00Z',
              'snapshot_id': 'QUAL-SNAPSHOT', 'policy_version': 'v1', 'data_completeness': 'full',
              'found_facts': facts, 'applicable_rules': [deepcopy(rule)], 'suspicious_patterns': [], 'confidence': None}
    observation = scenario_checks.Observation(result, deepcopy(context), ['QUAL-RULE'], [], 1.0, [])
    return scenario, observation


def _reject(call, exception_type, code):
    try:
        value = call()
    except exception_type as exc:
        if type(exc) is not exception_type or str(exc) != code:
            raise
        return {'decision': 'REJECTED', 'code': code}
    # Unexpected acceptance is an observed result, never the expected rejection.
    return {'decision': 'PASS', 'code': 'INVALID_INPUT_ACCEPTED', 'returned_type': type(value).__name__}


def _replay(number):
    scenario, observation = _sample()
    bundle = {'schema_version': '1.0', 'dataset': {'id': 'evaluator-qualification', 'kind': 'synthetic_control'},
              'cases': [scenario.case.model_dump(mode='json')],
              'outcomes': [{'case_id': 'QUAL-CASE', 'incident_id': 'INC-000901', 'result': observation.result,
                            'latency_seconds': 1.0, 'retrieval': {'namespace': 'rule_id', 'ids': ['QUAL-RULE']}}],
              'citation_catalog': [_citation()]}
    if number == 2:
        observation.result['applicable_rules'][0]['citation']['rule_version'] = 'v2'
    elif number == 3:
        bundle['outcomes'] = []
    elif number == 4:
        observation.result['found_facts'][3]['evidence'][0]['tx_id'] = 'ABSENT-TX'
    elif number == 5:
        bundle['unrecognized'] = True
        return _reject(lambda: evaluate_bundle(bundle), BundleInputError, 'INVALID_REPLAY_BUNDLE')
    evaluated = evaluate_bundle(bundle)
    return {'decision': evaluated['verdict'],
            'schema': {key: evaluated['metrics'][SCHEMA][key] for key in ('numerator', 'denominator', 'value')},
            'traceability': {key: evaluated['metrics'][TRACEABILITY][key] for key in ('numerator', 'denominator', 'value')},
            'missing_outcomes': evaluated['coverage']['missing_outcome_count'],
            'codes': sorted({code for row in evaluated['cases'] for code in row['issues']}),
            'synthetic_acceptance_blocked': next(gate['status'] for gate in evaluated['gates']
                                                if gate['name'] == 'Acceptance Evidence Kind') == 'INCOMPLETE'}


def _scenario(number):
    scenario, observation = _sample()
    if number == 2:
        observation.result['found_facts'][-1]['fact'] = observation.result['found_facts'][-1]['fact'].replace('10.00', '99.00')
    elif number == 3:
        observation.result['applicable_rules'][0]['citation']['rule_version'] = 'v2'
    elif number == 4:
        observation.result['found_facts'].reverse()
    codes = sorted(scenario_checks.check_observation(scenario, observation))
    return {'decision': 'FAIL' if codes else 'PASS', 'codes': codes}


def _model(number):
    scenario, observation = _sample()
    first = model_checks.check_repeat(scenario, observation, repeat_index=1)
    if number == 1:
        observation.result['report_id'] = 'QUAL-REPORT-OTHER'
        observation.result['generated_at'] = '2026-02-02T00:00:00Z'
        second = model_checks.check_repeat(scenario, observation, repeat_index=2)
        return {'decision': reporting.combine([first['status'], second['status']]),
                'semantic_equal': first['draft']['semantic_fingerprint'] == second['draft']['semantic_fingerprint'],
                'ordered_equal': first['draft']['ordered_fingerprint'] == second['draft']['ordered_fingerprint']}
    if number in (2, 4):
        observation.result['found_facts'][0]['fact'] = 'Unsupported synthetic approval claim.'
        second = model_checks.check_repeat(scenario, observation, repeat_index=2)
        if number == 2:
            return {'decision': second['status'], 'fact_issues': second['fact_issues']}
    rows = [first] if number == 3 else [first, second]
    summary = model_checks.summarize_repeats(rows, planned_case_ids=['QUAL-CASE'], repeats_per_case=2)
    return {'decision': summary['status'], 'planned': summary['planned_repeat_count'],
            'observed': summary['observed_repeat_count'], 'missing': summary['missing_repeat_count'],
            'pass_rate': summary['repeat_checks_pass_rate'], 'fact_violations': summary['fact_violation_count'],
            'independence_families': summary['observed_independence_family_count']}


def _retrieval(number):
    relevant = [_citation()]
    other = dict(_citation(), rule_version='v2')
    observed = [{'citation': _citation(), 'distance': 0.1}, {'citation': other, 'distance': 0.2}]
    if number == 2:
        observed = [{'citation': other, 'distance': 0.1}]
    elif number == 3:
        observed = []
    elif number == 4:
        relevant = []
    elif number == 5:
        observed.append({'citation': _citation(), 'distance': 0.3})
        return _reject(lambda: retrieval_checks.evaluate_ranking(observed, relevant, k=2),
                       ValueError, 'DUPLICATE_OBSERVED_CITATION')
    result = retrieval_checks.evaluate_ranking(observed, relevant, k=2)
    decision = ('INCOMPLETE' if result['ranking_status'] == 'out_of_scope'
                else 'PASS' if result['recall_at_k'] == 1 else 'FAIL')
    return {'decision': decision, **result}


def _judge_case(case_id, expected='pass'):
    return {'id': case_id, 'expected_verdict': expected, 'expected_preference': 'not_applicable',
            'split': 'open-control', 'family': 'one-synthetic-family', 'expected_kind': 'engineering-control',
            'programmatic_gate': {'status': 'FAIL' if expected == 'fail' else 'PASS', 'issues': []},
            'payload': {'case_id': case_id, 'criterion': 'grounding', 'mode': 'single',
                        'sources': [{'ref_id': 'source', 'text': 'Synthetic total: 10.00 RUB.'}],
                        'candidates': [{'label': 'A', 'report': {},
                                        'fragments': [{'ref_id': 'candidate', 'text': 'Synthetic candidate'}]}]}}


def _judge_response(case):
    return {'schema_version': '1.0', 'rubric_version': '1.0', 'case_id': case['id'],
            'criterion': 'grounding', 'verdict': 'pass', 'preference': 'not_applicable',
            'references': [{'ref_id': 'source', 'quote': '10.00 RUB'}], 'rationale': 'Synthetic engineering control'}


def _judge(number):
    case = _judge_case('QUAL-J1')
    response = _judge_response(case)
    if number <= 3:
        if number == 2:
            response['references'][0]['quote'] = 'Invented quotation'
            return _reject(lambda: judge_contracts.validate_response(json.dumps(response), case['payload']),
                           ValueError, 'QA_JUDGE_REFERENCE_QUOTE')
        if number == 3:
            del response['rationale']
            return _reject(lambda: judge_contracts.validate_response(json.dumps(response), case['payload']),
                           ValueError, 'QA_JUDGE_RESPONSE_FIELDS')
        validated = judge_contracts.validate_response(json.dumps(response), case['payload'])
        return {'decision': 'PASS', 'verdict': validated['verdict'], 'references': len(validated['references'])}
    cases = [case, _judge_case('QUAL-J2', 'fail')]
    observations = [{'case_id': case['id'], 'status': 'completed', 'response': response}]
    if number == 4:
        cases.append(_judge_case('QUAL-J3', 'fail'))
        observations.extend([
            {'case_id': 'QUAL-J2', 'status': 'completed', 'response': _judge_response(cases[1])},
            {'case_id': 'QUAL-J3', 'status': 'timeout'},
        ])
    result = judge_scoring.summarize(cases, observations)
    overall = result['overall']
    return {'decision': reporting.combine([result['execution_status'], result['known_challenge_status']]),
            'planned': overall['planned'], 'valid': overall['valid_responses'], 'missing': overall['missing'],
            'timeout': overall['timeout'], 'confusion': overall['confusion'],
            'all_planned_accuracy': overall['all_planned_accuracy'],
            'false_pass_rate': overall['false_pass_rate'],
            'human_calibration': result['human_calibration']['status'],
            'product_acceptance': result['product_acceptance'],
            'gate_overridden': result['programmatic_gates']['overridden_by_judge']}


def _reporting(number):
    test_count = 0 if number == 2 else 1
    failures = 1 if number in (4, 6) else 0
    skipped = 1 if number == 3 else 0
    child = '<failure/>' if failures else '<skipped/>' if skipped else ''
    rows = f'<testcase name="owned-control">{child}</testcase>' if test_count else ''
    xml = f'<testsuite tests="{test_count}" failures="{failures}" errors="0" skipped="{skipped}">{rows}</testsuite>'
    result = {'status': 'PASS', 'tests': test_count, 'collected_tests': test_count, 'additional_subtests': 0,
              'failures': failures, 'errors': 0, 'skipped': skipped, 'pytest_exit_code': 0,
              'contract_artifacts_match': [{'passed': True}]}
    files = {'result': result}
    if number not in (5, 6):
        files['junit'] = reporting.junit(xml.encode('utf-8'))
    state, metrics, issues = reporting.assess('unit', files)
    return {'decision': state, 'tests': metrics['tests'], 'failures': metrics['failures'],
            'skipped': metrics['skipped'], 'codes': sorted(issues)}


def observe(control_id):
    """Return a fresh JSON projection; unexpected exceptions propagate to runner."""
    if type(control_id) is not str or control_id not in CONTROL_IDS:
        raise ValueError('UNKNOWN_QUALIFICATION_CONTROL')
    prefix, number = control_id.split('-')
    return {'CORE': _replay, 'SCENARIO': _scenario, 'MODEL': _model, 'RETRIEVAL': _retrieval,
            'JUDGE': _judge, 'REPORTING': _reporting}[prefix](int(number))
