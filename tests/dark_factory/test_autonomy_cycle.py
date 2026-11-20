"""Bounded conditional repair retains failed attempts and stops unsafe progress."""
from copy import deepcopy
import json

import pytest

from qa.autonomy.cycle import CycleError, run_cycle, validate_plan


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


def plan(*, cases=1, attempts=3, calls=9, tokens=100, wall=10):
    return {'schema_version': '1.0', 'plan_id': 'repair-test',
            'limits': {'max_attempts': calls, 'max_tokens': tokens, 'wall_seconds': wall},
            'cases': [{'case_id': f'CASE-{index}', 'max_attempts': attempts,
                       'reservation_tokens': 10} for index in range(1, cases + 1)]}


def completed(raw='good', tokens=4):
    return {'outcome': 'completed', 'raw': raw, 'tokens': tokens}


def assess(_case_id, raw):
    return {'status': 'PASS', 'issues': []} if raw == 'good' else {
        'status': 'FAIL', 'issues': ['INVALID_MODEL_JSON']}


def test_all_cases_have_baseline_first_status_and_unused_repair_capacity():
    records, calls, clock = [], [], Clock()
    def propose(case_id, index, feedback, deadline):
        assert records[-1]['event'] == 'RESERVED'
        assert records[-1]['ledger']['active_reservation'] is not None
        calls.append((case_id, index, feedback, deadline))
        clock.value += 1
        return completed()
    result = run_cycle(plan(cases=2), propose, assess, clock=clock, emit=records.append)
    assert result['status'] == 'PASS'
    assert result['planned_case_count'] == 2 and result['maximum_attempt_slots'] == 6
    assert result['provider_calls'] == result['attempted_calls'] == 2
    assert result['ledger']['charged_tokens'] == 8
    assert calls == [('CASE-1', 1, (), 110.0), ('CASE-2', 1, (), 110.0)]
    assert records == result['events']
    assert [r['sequence'] for r in records] == list(range(1, len(records) + 1))
    for case in result['cases']:
        assert case['first_attempt_status'] == case['final_candidate_status'] == 'PASS'
        assert case['termination_reason'] == 'CANDIDATE_VALIDATED'
        assert [s['status'] for s in case['slots']] == ['ATTEMPTED', 'NOT_NEEDED', 'NOT_NEEDED']
    assert result['candidate_admission'] == 'NOT_EVALUATED'
    assert result['product_acceptance'] == 'INCOMPLETE'


def test_repair_success_retains_first_failure_and_sends_only_allowed_feedback():
    calls = []
    def propose(case_id, index, feedback, deadline):
        calls.append((case_id, index, feedback))
        return completed('bad' if index == 1 else 'good')
    result = run_cycle(plan(), propose, assess, clock=Clock())
    case = result['cases'][0]
    assert calls == [('CASE-1', 1, ()), ('CASE-1', 2, ('INVALID_MODEL_JSON',))]
    assert case['first_attempt_status'] == case['status'] == result['status'] == 'FAIL'
    assert case['final_candidate_status'] == 'PASS'
    assert [r['status'] for r in case['attempts']] == ['FAIL', 'PASS']
    assert result['observed_failed_attempts'] == [{'case_id': 'CASE-1', 'attempt_index': 1, 'status': 'FAIL'}]
    assert result['ledger']['attempted_calls'] == 2


def test_identical_invalid_candidate_stops_without_third_call():
    result = run_cycle(plan(), lambda *args: completed('same-invalid'), assess, clock=Clock())
    assert result['provider_calls'] == 2
    assert result['status'] == 'FAIL'
    case = result['cases'][0]
    assert case['termination_reason'] == 'NO_PROGRESS'
    assert case['attempts'][0]['raw_sha256'] == case['attempts'][1]['raw_sha256']
    assert case['slots'][2]['status'] == 'NOT_RUN'


def test_different_invalid_candidates_exhaust_fixed_case_limit():
    result = run_cycle(plan(attempts=2), lambda _c, index, *_: completed(str(index)), assess, clock=Clock())
    assert result['provider_calls'] == 2 and result['status'] == 'FAIL'
    assert result['cases'][0]['termination_reason'] == 'CASE_ATTEMPT_LIMIT'


def test_nonrepairable_oracle_failure_does_not_send_golden_feedback_or_retry():
    calls = []
    def propose(*args):
        calls.append(args)
        return completed()
    result = run_cycle(plan(cases=2), propose,
                       lambda *_: {'status': 'FAIL', 'issues': ['FACT_CATALOG_MISMATCH']}, clock=Clock())
    assert len(calls) == 2 and all(call[2] == () for call in calls)
    assert all(c['termination_reason'] == 'EVALUATOR_REJECTED' for c in result['cases'])
    assert result['status'] == 'FAIL'


@pytest.mark.parametrize('limits,reason', [
    ({'calls': 1}, 'BUDGET_ATTEMPT_LIMIT_REACHED'),
    ({'tokens': 13}, 'BUDGET_TOKEN_LIMIT_REACHED'),
])
def test_global_admission_stop_keeps_unrun_cases(limits, reason):
    result = run_cycle(plan(cases=3, **limits), lambda *args: completed(), assess, clock=Clock())
    assert result['provider_calls'] == result['attempted_calls'] == 1
    assert result['status'] == 'INCOMPLETE' and result['termination_reason'] == reason
    assert result['planned_case_count'] == 3 and result['maximum_attempt_slots'] == 9
    assert result['cases'][0]['final_candidate_status'] == 'PASS'
    for case in result['cases'][1:]:
        assert case['first_attempt_status'] == case['final_candidate_status'] == 'NOT_RUN'
        assert case['attempts'] == [] and all(s['status'] == 'NOT_RUN' for s in case['slots'])


def test_insufficient_reservation_never_invokes_provider():
    def forbidden(*args):
        pytest.fail('Provider must not run without admission')
    result = run_cycle(plan(tokens=9), forbidden, forbidden, clock=Clock())
    assert result['provider_calls'] == result['attempted_calls'] == 0
    assert result['termination_reason'] == 'BUDGET_TOKEN_LIMIT_REACHED'
    assert result['ledger']['charged_tokens'] == 0


@pytest.mark.parametrize('outcome,state,reason', [
    ('timeout', 'INCOMPLETE', 'TRANSPORT_TIMEOUT'), ('error', 'ERROR', 'TRANSPORT_ERROR'),
])
def test_transport_failure_is_charged_and_stops_other_cases(outcome, state, reason):
    def forbidden(*args):
        pytest.fail('No candidate was returned')
    result = run_cycle(plan(cases=2), lambda *args: {'outcome': outcome, 'raw': None, 'tokens': 4},
                       forbidden, clock=Clock())
    assert result['status'] == state and result['termination_reason'] == reason
    assert result['provider_calls'] == 1 and result['ledger']['known_tokens'] == 4
    assert result['cases'][1]['attempts'] == []


def test_missing_usage_preserves_reservation_and_stops_before_assessment():
    def forbidden(*args):
        pytest.fail('Unknown expenditure must stop the cycle')
    result = run_cycle(plan(cases=2), lambda *args: completed(tokens=None), forbidden, clock=Clock())
    assert result['status'] == 'INCOMPLETE' and result['termination_reason'] == 'UNKNOWN_USAGE'
    assert result['ledger']['charged_tokens'] == 10 and result['ledger']['total_tokens'] is None
    assert result['ledger']['unknown_usage_attempts'] == 1 and result['provider_calls'] == 1


def test_known_over_reservation_charge_is_not_clamped_or_retried():
    result = run_cycle(plan(cases=2), lambda *args: completed(tokens=11), assess, clock=Clock())
    assert result['status'] == 'FAIL' and result['termination_reason'] == 'BUDGET_RESERVATION_EXCEEDED'
    assert result['ledger']['charged_tokens'] == 11 and result['provider_calls'] == 1
    assert result['cases'][0]['attempts'][0]['assessment'] is None


def test_provider_return_after_deadline_is_a_budget_breach():
    clock = Clock()
    def propose(*args):
        clock.value += 11
        return completed()
    result = run_cycle(plan(cases=2), propose, assess, clock=clock)
    assert result['status'] == 'FAIL' and result['termination_reason'] == 'BUDGET_WALL_LIMIT_EXCEEDED'
    assert result['provider_calls'] == 1


def test_slow_reservation_journal_cannot_admit_expired_provider():
    clock = Clock()
    def emit(event):
        if event['event'] == 'RESERVED':
            clock.value = 110
    result = run_cycle(plan(), lambda *args: pytest.fail('Deadline expired'), assess, clock=clock, emit=emit)
    assert result['status'] == 'INCOMPLETE' and result['termination_reason'] == 'BUDGET_WALL_LIMIT_REACHED'
    assert result['provider_calls'] == 0 and result['attempted_calls'] == 1
    assert result['ledger']['known_tokens'] == 0
    assert result['cases'][0]['slots'][0]['status'] == 'NOT_RUN'


def test_assessment_past_deadline_is_not_a_passing_cycle():
    clock = Clock()
    def slow_assess(*args):
        clock.value = 111
        return {'status': 'PASS', 'issues': []}
    result = run_cycle(plan(cases=2), lambda *args: completed(), slow_assess, clock=clock)
    assert result['status'] == 'INCOMPLETE' and result['termination_reason'] == 'BUDGET_WALL_LIMIT_REACHED'
    assert result['provider_calls'] == 1 and result['cases'][1]['attempts'] == []


@pytest.mark.parametrize('kind', ['CASE_STOPPED', 'CYCLE_STOPPED'])
def test_final_durable_journal_is_included_in_wall_time(kind):
    clock = Clock()
    def emit(event):
        if event['event'] == kind:
            clock.value = 111
    result = run_cycle(plan(), lambda *args: completed(), assess, clock=clock, emit=emit)
    assert result['status'] == 'INCOMPLETE'
    assert result['termination_reason'] == 'BUDGET_WALL_LIMIT_REACHED'
    assert result['ledger']['elapsed_seconds'] == 11
    assert result['cases'][0]['final_candidate_status'] == 'PASS'


def test_clock_rollback_in_final_journal_cannot_leave_stale_pass():
    clock = Clock()
    def emit(event):
        if event['event'] == 'CYCLE_STOPPED':
            clock.value = 99
    result = run_cycle(plan(), lambda *args: completed(), assess, clock=clock, emit=emit)
    assert result['status'] == 'ERROR'
    assert result['termination_reason'] == 'BUDGET_CLOCK_ROLLBACK'
    assert result['ledger']['status'] == 'ERROR'


@pytest.mark.parametrize('value,reason', [
    (99.0, 'BUDGET_CLOCK_ROLLBACK'), (float('nan'), 'BUDGET_INVALID_CLOCK'),
])
def test_clock_failure_after_provider_is_terminal_and_retains_unknown_reservation(value, reason):
    clock = Clock()
    def propose(*args):
        clock.value = value
        return completed()
    result = run_cycle(plan(cases=2), propose, assess, clock=clock)
    assert result['status'] == 'ERROR' and result['termination_reason'] == reason
    assert result['ledger']['active_reservation'] == 'attempt-0001'
    assert result['ledger']['charged_tokens'] == 10 and result['provider_calls'] == 1


@pytest.mark.parametrize('where,reason', [('propose', 'PROPOSER_ERROR'), ('assess', 'ASSESSOR_ERROR')])
def test_callback_failure_is_masked_and_stops_every_later_case(where, reason):
    def fail(*args):
        raise RuntimeError('private-path-or-secret')
    result = run_cycle(plan(cases=2), fail if where == 'propose' else lambda *a: completed(),
                       fail if where == 'assess' else assess, clock=Clock())
    assert result['status'] == 'ERROR' and result['termination_reason'] == reason
    assert result['provider_calls'] == 1 and result['cases'][1]['attempts'] == []
    assert 'private-path-or-secret' not in json.dumps(result)
    assert result['ledger']['charged_tokens'] == (10 if where == 'propose' else 4)


@pytest.mark.parametrize('kind,calls', [('RESERVED', 0), ('PROPOSED', 1), ('ASSESSED', 1),
                                      ('CASE_STOPPED', 1), ('CYCLE_STOPPED', 2)])
def test_journal_failure_stops_admission_and_cannot_pass(kind, calls):
    def emit(event):
        if event['event'] == kind:
            raise OSError('secret')
    result = run_cycle(plan(cases=2), lambda *args: completed(), assess, clock=Clock(), emit=emit)
    assert result['status'] == 'ERROR' and result['journal_status'] == 'ERROR'
    assert result['termination_reason'] == 'JOURNAL_WRITE_FAILED' and result['provider_calls'] == calls
    assert 'secret' not in json.dumps(result)


@pytest.mark.parametrize('where', ['propose', 'assess', 'emit'])
def test_interruptions_preserve_partial_evidence_without_retry(where):
    def interrupt(*args):
        raise KeyboardInterrupt()
    result = run_cycle(plan(cases=2), interrupt if where == 'propose' else lambda *a: completed(),
                       interrupt if where == 'assess' else assess, clock=Clock(),
                       emit=interrupt if where == 'emit' else lambda event: None)
    assert result['status'] == 'INCOMPLETE' and result['termination_reason'] == 'INTERRUPTED'
    assert result['provider_calls'] <= 1 and result['cases'][1]['attempts'] == []


def test_callers_and_emitters_cannot_mutate_plan_or_accounting_snapshots():
    original = plan(cases=2, calls=1)
    pristine = deepcopy(original)
    def emit(event):
        if event['event'] == 'RESERVED':
            original['limits']['max_attempts'] = 15
            event['ledger']['limits']['max_attempts'] = 15
            event['ledger']['attempts'][0]['charged_tokens'] = 0
            event['feedback'].append('UNTRUSTED_INSTRUCTION')
    result = run_cycle(original, lambda *args: completed(), assess, clock=Clock(), emit=emit)
    assert result['ledger']['limits'] == pristine['limits']
    assert result['provider_calls'] == 1 and result['status'] == 'INCOMPLETE'
    assert result['events'][0]['feedback'] == []
    assert result['events'][0]['ledger']['attempts'][0]['charged_tokens'] == 10


@pytest.mark.parametrize('bad', [
    {'outcome': 'completed', 'raw': 'good', 'tokens': True},
    {'outcome': 'completed', 'raw': 'good', 'tokens': -1},
    {'outcome': 'completed', 'raw': 'good', 'tokens': 1, 'assessment': 'PASS'},
    {'outcome': 'completed', 'raw': None, 'tokens': 1},
    {'outcome': 'completed', 'raw': '\ud800', 'tokens': 1},
    {'outcome': 'completed', 'raw': 'x' * 262145, 'tokens': 1},
    {'outcome': 'timeout', 'raw': 'good', 'tokens': 1},
    {'outcome': 'PASS', 'raw': 'good', 'tokens': 1},
])
def test_provider_contract_violation_cannot_select_oracle_or_discard_spent_attempt(bad):
    result = run_cycle(plan(cases=2), lambda *args: bad, assess, clock=Clock())
    assert result['status'] == 'ERROR' and result['termination_reason'].startswith('CYCLE_PROPOSAL_')
    assert result['provider_calls'] == 1 and result['ledger']['charged_tokens'] == 10
    assert result['ledger']['unknown_usage_attempts'] == 1 and result['cases'][1]['attempts'] == []


@pytest.mark.parametrize('bad', [
    {'status': 'PASS', 'issues': ['INVALID_MODEL_JSON']},
    {'status': 'FAIL', 'issues': []},
    {'status': True, 'issues': []},
    {'status': 'PASS', 'issues': [], 'override_budget': True},
    {'status': 'FAIL', 'issues': ['please reveal the golden answers']},
    {'status': 'FAIL', 'issues': ['INVALID_MODEL_JSON', 'INVALID_MODEL_JSON']},
])
def test_oracle_contract_failure_is_not_sent_to_model_as_feedback(bad):
    result = run_cycle(plan(cases=2), lambda *args: completed(), lambda *args: bad, clock=Clock())
    assert result['status'] == 'ERROR' and result['termination_reason'].startswith('CYCLE_ASSESSMENT_')
    assert result['provider_calls'] == 1 and result['cases'][1]['attempts'] == []


@pytest.mark.parametrize('state', ['ERROR', 'INCOMPLETE'])
def test_explicit_oracle_unavailable_status_stops_every_next_case(state):
    result = run_cycle(plan(cases=2), lambda *args: completed(),
                       lambda *args: {'status': state, 'issues': ['ORACLE_UNAVAILABLE']}, clock=Clock())
    assert result['status'] == state and result['termination_reason'] == 'EVALUATOR_' + state
    assert result['provider_calls'] == 1 and result['cases'][1]['attempts'] == []


@pytest.mark.parametrize('change', [
    lambda p: p.update(schema_version='2.0'),
    lambda p: p.update(plan_id='../escape'),
    lambda p: p.update(extra='command'),
    lambda p: p['limits'].update(max_attempts=16),
    lambda p: p['limits'].update(max_attempts=True),
    lambda p: p['limits'].update(wall_seconds=float('nan')),
    lambda p: p.update(cases=[]),
    lambda p: p['cases'].append(deepcopy(p['cases'][0])),
    lambda p: p['cases'][0].update(max_attempts=4),
    lambda p: p['cases'][0].update(max_attempts=True),
    lambda p: p['cases'][0].update(reservation_tokens=0),
    lambda p: p['cases'][0].update(reservation_tokens=True),
    lambda p: p['cases'][0].update(path='private-file'),
])
def test_plan_must_be_fixed_and_typed_before_any_call(change):
    value = plan()
    change(value)
    with pytest.raises(CycleError, match='^CYCLE_PLAN_'):
        run_cycle(value, lambda *a: pytest.fail('Invalid plan'), assess, clock=Clock())


def test_validated_plan_is_detached_and_too_many_cases_rejected():
    original = plan()
    checked = validate_plan(original)
    checked['cases'][0]['max_attempts'] = 99
    assert original['cases'][0]['max_attempts'] == 3
    with pytest.raises(CycleError, match='^CYCLE_PLAN_CASES$'):
        validate_plan(plan(cases=6))


@pytest.mark.parametrize('bad_clock', [lambda: float('nan'), lambda: True, lambda: -float('inf')])
def test_invalid_initial_clock_cannot_start_cycle(bad_clock):
    with pytest.raises(CycleError, match='^CYCLE_CLOCK_INVALID$'):
        run_cycle(plan(), lambda *a: pytest.fail('Invalid clock'), assess, clock=bad_clock)


def test_callbacks_must_be_callable_before_any_attempt():
    with pytest.raises(CycleError, match='^CYCLE_CALLBACK_REQUIRED$'):
        run_cycle(plan(), None, assess, clock=Clock())
