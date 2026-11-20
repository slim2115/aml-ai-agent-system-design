"""Budget admission boundaries and retained cost of failed/unknown attempts."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from qa.stability.budget import BudgetError, BudgetLedger, BudgetStop, validate_limits


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


@pytest.fixture
def clock():
    return Clock()


def ledger(clock, *, calls=4, tokens=100, wall=10):
    return BudgetLedger({'max_attempts': calls, 'max_tokens': tokens, 'wall_seconds': wall}, clock)


@pytest.mark.parametrize('limits', [
    None, [], {}, {'max_attempts': 1, 'max_tokens': 1, 'wall_seconds': 1, 'extra': True},
    {'max_attempts': True, 'max_tokens': 1, 'wall_seconds': 1},
    {'max_attempts': 1.0, 'max_tokens': 1, 'wall_seconds': 1},
    {'max_attempts': 0, 'max_tokens': 1, 'wall_seconds': 1},
    {'max_attempts': 1001, 'max_tokens': 1, 'wall_seconds': 1},
    {'max_attempts': 1, 'max_tokens': False, 'wall_seconds': 1},
    {'max_attempts': 1, 'max_tokens': 0, 'wall_seconds': 1},
    {'max_attempts': 1, 'max_tokens': 10_000_001, 'wall_seconds': 1},
    {'max_attempts': 1, 'max_tokens': 1, 'wall_seconds': True},
    {'max_attempts': 1, 'max_tokens': 1, 'wall_seconds': 0},
    {'max_attempts': 1, 'max_tokens': 1, 'wall_seconds': float('nan')},
    {'max_attempts': 1, 'max_tokens': 1, 'wall_seconds': float('inf')},
    {'max_attempts': 1, 'max_tokens': 1, 'wall_seconds': 3601},
])
def test_limits_are_predeclared_finite_and_strictly_typed(limits):
    with pytest.raises(BudgetError, match='^BUDGET_INVALID_LIMITS$'):
        BudgetLedger(limits)


def test_limits_and_snapshots_cannot_be_used_to_reset_accounting(clock):
    limits = {'max_attempts': 1, 'max_tokens': 10, 'wall_seconds': 10}
    checked = validate_limits(limits)
    budget = BudgetLedger(limits, clock)
    checked['max_attempts'] = limits['max_attempts'] = 100
    identity = budget.reserve(7)
    snapshot = budget.snapshot()
    snapshot['limits']['max_attempts'] = 100
    snapshot['attempts'][0]['charged_tokens'] = 0
    budget.finish(identity, outcome='completed', tokens=7)
    with pytest.raises(BudgetStop, match='^BUDGET_ATTEMPT_LIMIT_REACHED$'):
        budget.reserve(1)
    assert budget.snapshot()['attempted_calls'] == 1
    assert budget.snapshot()['charged_tokens'] == 7


def test_reserve_charges_before_call_and_known_finish_releases_only_unused_tokens(clock):
    budget = ledger(clock, tokens=10)
    identity = budget.reserve(8)
    active = budget.snapshot()
    assert identity == 'attempt-0001'
    assert (active['status'], active['attempted_calls'], active['charged_tokens']) == ('INCOMPLETE', 1, 8)
    assert active['known_tokens'] == 0 and active['total_tokens'] is None
    clock.advance(2.5)
    result = budget.finish(identity, outcome='completed', tokens=3)
    assert result['status'] == 'PASS'
    assert (result['charged_tokens'], result['known_tokens'], result['remaining_tokens']) == (3, 3, 7)
    assert result['attempts'][0]['elapsed_seconds'] == 2.5
    assert result['attempts'][0]['reserved_tokens'] == 8
    assert result['total_tokens'] == 3


@pytest.mark.parametrize('outcome', ['completed', 'timeout', 'error'])
def test_unknown_usage_is_charged_conservatively_for_every_outcome(clock, outcome):
    budget = ledger(clock, tokens=10)
    first = budget.reserve(4)
    budget.finish(first, outcome='completed', tokens=3)
    second = budget.reserve(7)
    result = budget.finish(second, outcome=outcome, tokens=None)
    assert result['status'] == 'INCOMPLETE'
    assert (result['attempted_calls'], result['charged_tokens'], result['known_tokens']) == (2, 10, 3)
    assert result['unknown_usage_attempts'] == 1
    assert result['total_tokens'] is None
    with pytest.raises(BudgetStop, match='^BUDGET_TOKEN_LIMIT_REACHED$'):
        budget.reserve(1)
    assert budget.snapshot()['attempted_calls'] == 2
    assert budget.snapshot()['charged_tokens'] == 10


def test_timeout_and_error_calls_are_never_refunded_or_retried(clock):
    budget = ledger(clock, calls=2)
    budget.finish(budget.reserve(10), outcome='timeout', tokens=4)
    budget.finish(budget.reserve(10), outcome='error', tokens=2)
    result = budget.snapshot()
    assert result['status'] == 'PASS'  # Complete accounting is not response quality.
    assert [r['outcome'] for r in result['attempts']] == ['timeout', 'error']
    assert result['attempted_calls'] == 2 and result['known_tokens'] == 6
    with pytest.raises(BudgetStop, match='^BUDGET_ATTEMPT_LIMIT_REACHED$'):
        budget.reserve(1)
    assert budget.snapshot()['attempted_calls'] == 2


def test_wall_boundary_denies_new_call_without_mutating_attempt_count(clock):
    budget = ledger(clock, wall=2)
    clock.advance(2)
    with pytest.raises(BudgetStop) as stop:
        budget.reserve(1)
    assert stop.value.code == 'BUDGET_WALL_LIMIT_REACHED'
    result = budget.snapshot()
    assert result['attempted_calls'] == 0 and result['charged_tokens'] == 0
    assert result['admission_status'] == 'STOPPED'
    assert result['remaining_wall_seconds'] == 0
    assert result['status'] == 'PASS'  # The experiment still has unexecuted slots.


def test_declined_reservation_is_sticky_and_cannot_be_replaced_by_smaller_cost(clock):
    budget = ledger(clock, tokens=5)
    with pytest.raises(BudgetStop, match='^BUDGET_TOKEN_LIMIT_REACHED$'):
        budget.reserve(6)
    with pytest.raises(BudgetStop, match='^BUDGET_TOKEN_LIMIT_REACHED$'):
        budget.reserve(1)
    assert budget.snapshot()['attempted_calls'] == 0


def test_reservation_larger_than_any_allowed_budget_is_still_an_admission_stop(clock):
    budget = ledger(clock)
    with pytest.raises(BudgetStop, match='^BUDGET_TOKEN_LIMIT_REACHED$'):
        budget.reserve(10_000_001)
    with pytest.raises(BudgetStop, match='^BUDGET_TOKEN_LIMIT_REACHED$'):
        budget.reserve(1)
    assert budget.snapshot()['attempted_calls'] == 0


@pytest.mark.parametrize('delay,expected', [(2, 'PASS'), (2.1, 'FAIL')])
def test_completion_at_deadline_is_allowed_but_late_completion_is_a_breach(clock, delay, expected):
    budget = ledger(clock, wall=2)
    identity = budget.reserve(4)
    clock.advance(delay)
    result = budget.finish(identity, outcome='completed', tokens=2)
    assert result['status'] == expected
    assert result['charged_tokens'] == 2
    assert result['attempted_calls'] == 1
    with pytest.raises(BudgetStop):
        budget.reserve(1)
    if expected == 'FAIL':
        assert result['breaches'] == ['BUDGET_WALL_LIMIT_EXCEEDED']
        assert result['stop_reason'] == 'BUDGET_WALL_LIMIT_EXCEEDED'


@pytest.mark.parametrize('used,breaches', [
    (5, ['BUDGET_RESERVATION_EXCEEDED']),
    (12, ['BUDGET_RESERVATION_EXCEEDED', 'BUDGET_TOKEN_LIMIT_EXCEEDED']),
    (10_000_001, ['BUDGET_RESERVATION_EXCEEDED', 'BUDGET_TOKEN_LIMIT_EXCEEDED']),
    (10 ** 30, ['BUDGET_RESERVATION_EXCEEDED', 'BUDGET_TOKEN_LIMIT_EXCEEDED']),
])
def test_provider_overrun_is_recorded_as_actual_cost_and_closes_admission(clock, used, breaches):
    budget = ledger(clock, tokens=10)
    result = budget.finish(budget.reserve(4), outcome='completed', tokens=used)
    assert result['status'] == 'FAIL'
    assert result['known_tokens'] == result['charged_tokens'] == used
    assert result['breaches'] == breaches
    with pytest.raises(BudgetStop, match='^BUDGET_RESERVATION_EXCEEDED$'):
        budget.reserve(1)
    assert budget.snapshot()['attempted_calls'] == 1


@pytest.mark.parametrize('reservation', [0, -1, True, 1.0, '1', None])
def test_invalid_reservation_never_creates_an_attempt(clock, reservation):
    budget = ledger(clock)
    with pytest.raises(BudgetError, match='^BUDGET_INVALID_RESERVATION$'):
        budget.reserve(reservation)
    assert budget.snapshot()['attempted_calls'] == 0


@pytest.mark.parametrize('usage', [-1, True, 1.0, '1', float('inf')])
def test_invalid_usage_cannot_release_or_settle_reservation(clock, usage):
    budget = ledger(clock)
    identity = budget.reserve(5)
    with pytest.raises(BudgetError, match='^BUDGET_INVALID_USAGE$'):
        budget.finish(identity, outcome='completed', tokens=usage)
    assert budget.snapshot()['active_reservation'] == identity
    assert budget.snapshot()['charged_tokens'] == 5


def test_only_one_active_reservation_and_exactly_one_settlement(clock):
    budget = ledger(clock)
    identity = budget.reserve(5)
    with pytest.raises(BudgetError, match='^BUDGET_ACTIVE_RESERVATION$'):
        budget.reserve(1)
    with pytest.raises(BudgetError, match='^BUDGET_UNKNOWN_OR_SETTLED_RESERVATION$'):
        budget.finish('attempt-9999', outcome='completed', tokens=1)
    with pytest.raises(BudgetError, match='^BUDGET_INVALID_OUTCOME$'):
        budget.finish(identity, outcome='success', tokens=1)
    budget.finish(identity, outcome='error', tokens=0)
    with pytest.raises(BudgetError, match='^BUDGET_UNKNOWN_OR_SETTLED_RESERVATION$'):
        budget.finish(identity, outcome='completed', tokens=0)
    assert budget.snapshot()['attempted_calls'] == 1
    assert budget.snapshot()['known_tokens'] == 0


def test_concurrent_admission_cannot_double_reserve(clock):
    budget = ledger(clock)
    barrier = Barrier(2)
    def reserve():
        barrier.wait(timeout=5)
        try:
            return budget.reserve(5)
        except BudgetError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: reserve(), range(2)))
    assert sorted(results) == ['BUDGET_ACTIVE_RESERVATION', 'attempt-0001']
    assert budget.snapshot()['attempted_calls'] == 1


@pytest.mark.parametrize('invalid', [True, None, '100', float('nan'), float('inf'), 10 ** 1000])
def test_invalid_clock_cannot_initialize_ledger(invalid):
    with pytest.raises(BudgetError, match='^BUDGET_INVALID_CLOCK$'):
        BudgetLedger({'max_attempts': 1, 'max_tokens': 1, 'wall_seconds': 1}, clock=lambda: invalid)


@pytest.mark.parametrize('mode,code', [('rollback', 'BUDGET_CLOCK_ROLLBACK'),
                                     ('nonfinite', 'BUDGET_INVALID_CLOCK'),
                                     ('exception', 'BUDGET_CLOCK_ERROR')])
def test_clock_failure_is_terminal_and_preserves_charged_active_attempt(mode, code):
    state = {'time': 100.0, 'raises': False}
    def clock():
        if state['raises']:
            raise RuntimeError('provider-secret-is-not-a-diagnostic')
        return state['time']
    budget = ledger(clock)
    identity = budget.reserve(7)
    if mode == 'rollback':
        state['time'] = 99
    elif mode == 'nonfinite':
        state['time'] = float('inf')
    else:
        state['raises'] = True
    result = budget.snapshot()
    assert result['status'] == 'ERROR' and result['error_code'] == code
    assert result['active_reservation'] == identity and result['charged_tokens'] == 7
    state.update(time=101, raises=False)
    with pytest.raises(BudgetError, match='^' + code + '$'):
        budget.reserve(1)
    with pytest.raises(BudgetError, match='^' + code + '$'):
        budget.finish(identity, outcome='completed', tokens=1)
    assert budget.snapshot()['error_code'] == code
    assert 'provider-secret' not in str(budget.snapshot())


def test_unsettled_attempt_is_never_a_complete_zero_cost_run(clock):
    budget = ledger(clock)
    budget.reserve(10)
    clock.advance(30)
    result = budget.snapshot()
    assert result['status'] == 'INCOMPLETE'
    assert result['active_reservation'] is not None
    assert result['charged_tokens'] == 10 and result['total_tokens'] is None
    assert result['attempts'][0]['elapsed_seconds'] is None
    assert result['remaining_wall_seconds'] == 0
    assert 'no hard interruption' in result['scope']
