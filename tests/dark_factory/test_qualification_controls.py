"""The control adapters exercise real evaluators and have independent expectations."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from qa.qualification import controls


CATALOG = json.loads((Path(__file__).resolve().parents[2] / 'qa/qualification/catalog-v1.json').read_text('utf-8'))
EXPECTED = {row['id']: row['expected'] for row in CATALOG['controls']}


@pytest.mark.parametrize('control_id', controls.CONTROL_IDS)
def test_real_evaluator_observation_matches_hand_authored_control(control_id):
    assert controls.observe(control_id) == EXPECTED[control_id]


def test_every_registered_control_has_one_static_expectation_and_declared_evaluator():
    assert tuple(EXPECTED) == controls.CONTROL_IDS
    assert len(CATALOG['controls']) == len(EXPECTED) == 29
    evaluator_ids = {row['id'] for row in CATALOG['evaluators']}
    assert evaluator_ids == {'core', 'scenario', 'model', 'retrieval', 'judge', 'reporting'}
    assert {row['evaluator_id'] for row in CATALOG['controls']} == evaluator_ids
    assert all(row['limitations'] for row in CATALOG['evaluators'])
    assert {row['kind'] for row in CATALOG['controls']} == {'positive', 'negative', 'incomplete', 'invalid', 'invariant'}


def test_control_order_and_prior_mutations_do_not_change_later_observations():
    baseline = {name: controls.observe(name) for name in controls.CONTROL_IDS}
    reversed_run = {name: controls.observe(name) for name in reversed(controls.CONTROL_IDS)}
    assert reversed_run == baseline == EXPECTED
    # Objects returned to a caller are not shared with future runs.
    baseline['CORE-01']['schema']['numerator'] = 999
    baseline['SCENARIO-01']['codes'].append('tamper')
    assert controls.observe('CORE-01') == EXPECTED['CORE-01']
    assert controls.observe('SCENARIO-01') == EXPECTED['SCENARIO-01']


@pytest.mark.parametrize('control_id,module,entrypoint', [
    ('CORE-01', controls, 'evaluate_bundle'),
    ('SCENARIO-01', controls.scenario_checks, 'check_observation'),
    ('MODEL-01', controls.model_checks, 'check_repeat'),
    ('RETRIEVAL-01', controls.retrieval_checks, 'evaluate_ranking'),
    ('JUDGE-01', controls.judge_contracts, 'validate_response'),
    ('REPORTING-01', controls.reporting, 'assess'),
])
def test_unexpected_evaluator_exception_propagates_instead_of_becoming_qualification_success(
        monkeypatch, control_id, module, entrypoint):
    def fail(*args, **kwargs):
        raise RuntimeError('unexpected evaluator failure')
    monkeypatch.setattr(module, entrypoint, fail)
    with pytest.raises(RuntimeError, match='unexpected evaluator failure'):
        controls.observe(control_id)


@pytest.mark.parametrize('raised', [ValueError('OTHER_CODE'), RuntimeError('EXPECTED_CODE')])
def test_expected_rejection_does_not_swallow_wrong_code_or_exception_type(raised):
    def call():
        raise raised
    with pytest.raises(type(raised), match=str(raised)):
        controls._reject(call, ValueError, 'EXPECTED_CODE')


def test_expected_rejection_does_not_swallow_a_subclass_with_the_same_message():
    class UnexpectedSubclass(ValueError):
        pass
    def call():
        raise UnexpectedSubclass('EXPECTED_CODE')
    with pytest.raises(UnexpectedSubclass):
        controls._reject(call, ValueError, 'EXPECTED_CODE')


def test_invalid_input_control_detects_a_validator_that_accepts_everything(monkeypatch):
    monkeypatch.setattr(controls, 'evaluate_bundle', lambda value: {})
    observation = controls.observe('CORE-05')
    assert observation['decision'] == 'PASS'
    assert observation['code'] == 'INVALID_INPUT_ACCEPTED'
    assert observation != EXPECTED['CORE-05']


def test_version_control_detects_a_ranking_function_that_ignores_versions(monkeypatch):
    original = controls.retrieval_checks.evaluate_ranking
    def ignoring_version(observed, relevant, *, k):
        altered = deepcopy(observed)
        for row in altered:
            row['citation']['rule_version'] = relevant[0]['rule_version']
        return original(altered, relevant, k=k)
    monkeypatch.setattr(controls.retrieval_checks, 'evaluate_ranking', ignoring_version)
    observation = controls.observe('RETRIEVAL-02')
    assert observation['decision'] == 'PASS'
    assert observation != EXPECTED['RETRIEVAL-02']


def test_missing_repeat_control_detects_denominator_shrinking(monkeypatch):
    original = controls.model_checks.summarize_repeats
    def excluding_missing(*args, **kwargs):
        value = original(*args, **kwargs)
        metric = value['repeat_checks_pass_rate']
        metric['denominator'] = value['observed_repeat_count']
        metric['value'] = metric['numerator'] / metric['denominator']
        return value
    monkeypatch.setattr(controls.model_checks, 'summarize_repeats', excluding_missing)
    observation = controls.observe('MODEL-03')
    assert observation['decision'] == 'INCOMPLETE'
    assert observation['pass_rate']['value'] == 1.0
    assert observation != EXPECTED['MODEL-03']


def test_known_failure_control_detects_erasing_failure_when_junit_is_missing(monkeypatch):
    original = controls.reporting.assess
    def erase_failure(kind, files):
        status, metrics, issues = original(kind, files)
        return ('INCOMPLETE' if 'junit_missing' in issues else status), metrics, issues
    monkeypatch.setattr(controls.reporting, 'assess', erase_failure)
    observation = controls.observe('REPORTING-06')
    assert observation['decision'] == 'INCOMPLETE'
    assert observation != EXPECTED['REPORTING-06']


@pytest.mark.parametrize('value', ['CORE-99', '../CORE-01', None, 1])
def test_control_identity_is_fixed_and_cannot_name_arbitrary_code(value):
    with pytest.raises(ValueError, match='^UNKNOWN_QUALIFICATION_CONTROL$'):
        controls.observe(value)
