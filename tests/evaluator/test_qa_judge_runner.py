"""Collector qualification without Ollama, networking, or generator calls."""
from contextlib import nullcontext
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from qa.tools import run_judge as runner


def planned(case_id='J-001', split='calibration'):
    return {
        'id': case_id, 'split': split, 'family': 'synthetic-family',
        'expected_verdict': 'pass', 'expected_preference': 'not_applicable',
        'expected_kind': 'synthetic_rule', 'mutation': 'SECRET_MUTATION',
        'programmatic_gate': {'status': 'PASS', 'issues': []},
        'payload': {'case_id': case_id, 'criterion': 'grounding', 'mode': 'single',
                    'sources': [{'ref_id': 'source:amount', 'text': 'Total 12.30 USD'}],
                    'candidates': [{'label': 'A', 'report': {'status': 'draft'},
                                    'fragments': [{'ref_id': 'A:amount', 'text': '12.30 USD'}]}]},
    }


def reply(payload):
    return {'schema_version': '1.0', 'rubric_version': '1.0',
            'case_id': payload['case_id'], 'criterion': payload['criterion'],
            'verdict': 'pass', 'preference': 'not_applicable',
            'references': [{'ref_id': 'source:amount', 'quote': '12.30 USD'}],
            'rationale': 'The cited amount matches.'}


def summary(**overrides):
    value = {'known_challenge_status': 'PASS', 'execution_status': 'PASS',
             'human_calibration': {'status': 'EVIDENCE_COMPLETE'}}
    value.update(overrides)
    return value


@pytest.mark.parametrize('relative', ['verification/qa-step-08-unit/batch', 'var/qa/judge/batch'])
def test_new_output_must_be_in_explicit_judge_namespace(tmp_path, monkeypatch, relative):
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    destination = runner.output_path(relative)
    assert destination == tmp_path / relative
    assert not destination.exists()


@pytest.mark.parametrize('relative', ['src/new', 'qa/datasets/new', 'var/qa/judge',
                                      'verification/qa-step-08-unit',
                                      'verification/qa-step-07-unit/batch',
                                      'var/qa/judge/../../outside'])
def test_wrong_scope_and_traversal_are_rejected(tmp_path, monkeypatch, relative):
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    with pytest.raises(ValueError):
        runner.output_path(relative)


def test_output_outside_root_and_existing_evidence_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    with pytest.raises(ValueError):
        runner.output_path(tmp_path.parent / 'outside')
    existing = tmp_path / 'var/qa/judge/existing'
    existing.mkdir(parents=True)
    marker = existing / 'keep.json'
    marker.write_text('original evidence', encoding='utf-8')
    with pytest.raises(FileExistsError, match='QA_EVIDENCE_ALREADY_EXISTS'):
        runner.output_path(existing)
    assert marker.read_text(encoding='utf-8') == 'original evidence'


def test_cache_key_is_canonical_but_sensitive_to_every_evaluation_input():
    payload = planned()['payload']
    rubric = {'version': '1.0', 'criterion': 'match cited amount'}
    key = runner.evaluation_key(payload, rubric, 'digest-1', 'system-1')
    assert len(key) == 64
    assert runner.evaluation_key(dict(reversed(list(payload.items()))),
                                 dict(reversed(list(rubric.items()))), 'digest-1', 'system-1') == key
    different_payload = deepcopy(payload)
    different_payload['sources'][0]['text'] = 'Total 120.30 USD'
    assert runner.evaluation_key(different_payload, rubric, 'digest-1', 'system-1') != key
    assert runner.evaluation_key(payload, {**rubric, 'version': '2.0'}, 'digest-1', 'system-1') != key
    assert runner.evaluation_key(payload, rubric, 'digest-2', 'system-1') != key
    assert runner.evaluation_key(payload, rubric, 'digest-1', 'system-2') != key


def test_nonfinite_values_cannot_be_silently_canonicalized_into_cache_identity():
    with pytest.raises(ValueError):
        runner.content_hash({'value': float('nan')})


def test_selection_retains_full_catalog_and_explicit_phase_intersection():
    cases = [planned('CAL'), planned('HOLD', 'heldout')]
    original = deepcopy(cases)
    assert runner.selected_cases(cases) == cases
    assert runner.selected_cases(cases, 'heldout') == [cases[1]]
    assert runner.selected_cases(cases, case_id='CAL') == [cases[0]]
    assert cases == original
    with pytest.raises(ValueError, match='EMPTY_SELECTION'):
        runner.selected_cases(cases, 'heldout', 'CAL')


@pytest.mark.parametrize('phase,case_id,code', [
    ('unknown', None, 'UNKNOWN_PHASE'), ('all', 'UNKNOWN', 'UNKNOWN_CASE'),
])
def test_invalid_selection_is_never_interpreted_as_successful_empty_run(phase, case_id, code):
    with pytest.raises(ValueError, match=code):
        runner.selected_cases([planned()], phase, case_id)


@pytest.mark.parametrize('summary_overrides,source_stable,model_stable,full_matrix,expected', [
    ({}, True, True, True, 'PASS'),
    ({'human_calibration': {'status': 'INCOMPLETE'}}, True, True, True, 'INCOMPLETE'),
    ({}, True, True, False, 'INCOMPLETE'),
    ({'execution_status': 'INCOMPLETE'}, True, True, True, 'INCOMPLETE'),
    ({'known_challenge_status': 'INCOMPLETE'}, True, True, True, 'INCOMPLETE'),
    ({'execution_status': 'FAIL', 'human_calibration': {'status': 'INCOMPLETE'}}, True, True, False, 'FAIL'),
    ({'known_challenge_status': 'FAIL', 'human_calibration': {'status': 'INCOMPLETE'}}, True, True, False, 'FAIL'),
    ({'human_calibration': {'status': 'INCOMPLETE'}}, False, True, False, 'FAIL'),
    ({'human_calibration': {'status': 'INCOMPLETE'}}, True, False, False, 'FAIL'),
    ({}, True, None, True, 'INCOMPLETE'),
])
def test_failures_take_precedence_over_missing_human_calibration(summary_overrides, source_stable,
                                                               model_stable, full_matrix, expected):
    assert runner.final_status(summary(**summary_overrides), source_stable=source_stable,
                               model_stable=model_stable, full_matrix=full_matrix) == expected


def test_human_packet_is_blind_and_the_form_contains_no_prefilled_judgements(tmp_path):
    item = planned()
    item['expected_verdict'] = 'SECRET_ORACLE_LABEL'
    item['programmatic_gate'] = {'status': 'SECRET_GATE_STATUS', 'issues': ['SECRET_METRIC']}
    item['judge_result'] = {'verdict': 'SECRET_MODEL_RATING'}
    runner.human_packet(tmp_path, [item], {'criteria': {'grounding': 'Use exact sources.'}}, 'a' * 64)
    packet = (tmp_path / 'human-review-packet.md').read_text(encoding='utf-8')
    form = json.loads((tmp_path / 'human-review-template.json').read_text(encoding='utf-8'))
    for secret in ('SECRET_ORACLE_LABEL', 'SECRET_GATE_STATUS', 'SECRET_METRIC',
                   'SECRET_MODEL_RATING', 'SECRET_MUTATION'):
        assert secret not in packet and secret not in json.dumps(form)
    assert 'source:amount' in packet and 'Total 12.30 USD' in packet
    assert 'A:amount' in packet
    assert form['reviewer_id'] == form['reviewed_at'] == ''
    assert form['ratings'] == [{'case_id': 'J-001', 'criterion': 'grounding', 'verdict': '',
                               'preference': 'not_applicable', 'references': [], 'rationale': ''}]
    original = (tmp_path / 'human-review-packet.md').read_bytes()
    with pytest.raises(FileExistsError):
        runner.human_packet(tmp_path, [item], {'criteria': {}}, 'a' * 64)
    assert (tmp_path / 'human-review-packet.md').read_bytes() == original


@pytest.mark.parametrize('mutation', ['unknown_reference', 'invented_quote', 'wrong_case', 'extra_field'])
def test_completed_transport_with_invalid_response_is_not_accepted(mutation):
    item = planned()
    value = reply(item['payload'])
    if mutation == 'unknown_reference':
        value['references'][0]['ref_id'] = 'unknown'
    elif mutation == 'invented_quote':
        value['references'][0]['quote'] = '120.30 USD'
    elif mutation == 'wrong_case':
        value['case_id'] = 'OTHER'
    else:
        value['confidence'] = 1
    result = runner.assess_record(item, {'status': 'completed', 'response_text': json.dumps(value),
                                        'elapsed_seconds': 1}, 'cache-key')
    assert result['status'] == 'invalid_response'
    assert result['response'] is None
    assert result['error_code'].startswith('QA_JUDGE_')
    assert result['operator_decision_written'] is False


@pytest.mark.parametrize('status', ['timeout', 'error', 'invalid_response'])
def test_transport_failure_status_is_preserved_without_attempting_to_accept_response(status):
    item = planned()
    result = runner.assess_record(item, {'status': status, 'response_text': json.dumps(reply(item['payload'])),
                                        'error_code': 'KNOWN_FAILURE', 'elapsed_seconds': 3}, 'key')
    assert result['status'] == status and result['response'] is None
    assert result['error_code'] == 'KNOWN_FAILURE' and result['latency_seconds'] == 3
    assert result['programmatic_gate'] == item['programmatic_gate']


@pytest.fixture
def isolated_run(tmp_path, monkeypatch):
    from qa.judge import dataset, transport

    cases = [planned('CAL'), planned('HOLD', 'heldout')]
    dataset_dir = tmp_path / 'dataset'
    dataset_dir.mkdir()
    manifest = b'{"synthetic_test_manifest":true}'
    (dataset_dir / 'manifest.json').write_bytes(manifest)
    rubric = tmp_path / 'rubric.json'
    rubric.write_text(json.dumps({'criteria': {'grounding': 'Use exact sources.'}}), encoding='utf-8')
    monkeypatch.setattr(dataset, 'DEFAULT_FOLDER', dataset_dir)
    monkeypatch.setattr(dataset, 'load_dataset', lambda: {'cases': deepcopy(cases)})
    monkeypatch.setattr(runner, 'RUBRIC', rubric)
    monkeypatch.setattr(runner, 'environment', lambda output: None)
    monkeypatch.setattr(runner, 'source_hashes', lambda: {'frozen': 'unchanged'})
    monkeypatch.setattr(runner, 'network_guard', nullcontext)
    calls = []

    def identity():
        calls.append('identity')
        return {'model': {'digest': runner.EXPECTED_DIGEST}, 'server': {'version': 'test'}, 'show': {}}

    class FakeTransport:
        def complete(self, payload, rubric, *, deadline):
            calls.append(payload['case_id'])
            return {'status': 'completed', 'response_text': json.dumps(reply(payload)), 'elapsed_seconds': 0.01}

    monkeypatch.setattr(runner, 'identity', identity)
    monkeypatch.setattr(runner, 'ollama_json', lambda path: {'models': []})
    monkeypatch.setattr(transport, 'JudgeTransport', FakeTransport)
    return {'output': tmp_path / 'new-run', 'cases': cases, 'calls': calls,
            'dataset_hash': sha256(manifest).hexdigest()}


def test_prepare_only_does_not_contact_models_or_claim_pass(isolated_run):
    state = isolated_run
    assert runner.run(state['output'], prepare_only=True) == 3
    assert state['calls'] == []
    result = json.loads((state['output'] / 'result.json').read_text(encoding='utf-8'))
    assert result['status'] == 'INCOMPLETE'
    assert result['generator_calls'] == result['observed_case_count'] == 0
    assert result['planned_case_count'] == result['summary']['overall']['missing'] == 2
    assert result['model_identity_stable'] is None
    assert result['summary']['human_calibration']['status'] == 'INCOMPLETE'
    assert (state['output'] / 'human-review-packet.md').exists()


def test_preflight_unavailable_stays_incomplete_with_all_planned_cases_missing(isolated_run, monkeypatch):
    state = isolated_run

    def unavailable():
        raise RuntimeError('QA_TEST_MODEL_UNAVAILABLE')

    monkeypatch.setattr(runner, 'identity', unavailable)
    assert runner.run(state['output']) == 3
    result = json.loads((state['output'] / 'result.json').read_text(encoding='utf-8'))
    assert result['status'] == 'INCOMPLETE'
    assert result['error_code'] == 'QA_TEST_MODEL_UNAVAILABLE'
    assert result['observations'] == []
    assert result['summary']['overall']['missing'] == result['planned_case_count'] == 2
    assert not result['full_planned_matrix']
    assert state['calls'] == []


@pytest.mark.parametrize('prepare_only', [False, True])
def test_source_changes_are_failures_even_without_model_observations(isolated_run, monkeypatch, prepare_only):
    state = isolated_run
    snapshots = iter([{'frozen': 'before'}, {'frozen': 'after'}])
    monkeypatch.setattr(runner, 'source_hashes', lambda: next(snapshots))

    def unavailable():
        raise RuntimeError('QA_TEST_MODEL_UNAVAILABLE')

    monkeypatch.setattr(runner, 'identity', unavailable)
    assert runner.run(state['output'], prepare_only=prepare_only) == 1
    result = json.loads((state['output'] / 'result.json').read_text(encoding='utf-8'))
    assert result['status'] == 'FAIL'
    assert result['source_files_unchanged'] is False


def test_partial_selection_keeps_full_matrix_denominator_and_human_material(isolated_run):
    state = isolated_run
    assert runner.run(state['output'], phase='calibration') == 3
    result = json.loads((state['output'] / 'result.json').read_text(encoding='utf-8'))
    assert result['status'] == 'INCOMPLETE'
    assert result['selected_case_count'] == result['observed_case_count'] == 1
    assert result['planned_case_count'] == 2
    assert result['summary']['overall']['all_planned_accuracy']['rate'] == 0.5
    assert result['summary']['per_split']['heldout']['missing'] == 1
    assert result['full_planned_matrix'] is False
    assert state['calls'] == ['identity', 'CAL', 'identity']
    template = json.loads((state['output'] / 'human-review-template.json').read_text(encoding='utf-8'))
    assert [rating['case_id'] for rating in template['ratings']] == ['CAL', 'HOLD']


def test_all_synthetic_labels_correct_does_not_complete_human_calibration(isolated_run):
    state = isolated_run
    assert runner.run(state['output']) == 3
    result = json.loads((state['output'] / 'result.json').read_text(encoding='utf-8'))
    assert result['full_planned_matrix'] is True
    assert result['summary']['execution_status'] == result['summary']['known_challenge_status'] == 'PASS'
    assert result['summary']['human_calibration']['status'] == 'INCOMPLETE'
    assert result['status'] == result['product_acceptance'] == 'INCOMPLETE'


def test_complete_evidence_remains_advisory_and_does_not_authorize_the_product(isolated_run):
    state = isolated_run
    reviews = {'schema_version': '1.0', 'rubric_version': '1.0',
               'dataset_sha256': state['dataset_hash'], 'reviewer_id': 'synthetic-unit-test',
               'reviewed_at': '2026-10-21T12:00:00Z', 'ratings': [
                   {key: value for key, value in reply(item['payload']).items()
                    if key not in {'schema_version', 'rubric_version'}} for item in state['cases']]}
    review_path = state['output'].parent / 'reviews.json'
    review_path.write_text(json.dumps(reviews), encoding='utf-8')
    assert runner.run(state['output'], human_reviews=review_path) == 0
    result = json.loads((state['output'] / 'result.json').read_text(encoding='utf-8'))
    assert result['status'] == 'PASS' and result['advisory_only'] is True
    assert result['summary']['human_calibration']['status'] == 'EVIDENCE_COMPLETE'
    assert result['product_acceptance'] == 'INCOMPLETE'
    assert result['suitability'] == 'NOT_ESTABLISHED'
    assert result['generator_calls'] == 0
    assert all(row['operator_decision_written'] is False for row in result['observations'])


def test_run_refuses_to_overwrite_an_existing_artifact_directory(isolated_run):
    state = isolated_run
    state['output'].mkdir()
    marker = state['output'] / 'result.json'
    marker.write_text('preserve historical evidence', encoding='utf-8')
    with pytest.raises(FileExistsError):
        runner.run(state['output'], prepare_only=True)
    assert marker.read_text(encoding='utf-8') == 'preserve historical evidence'
    assert state['calls'] == []
