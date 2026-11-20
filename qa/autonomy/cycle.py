"""A bounded response-repair controller around trusted proposal/oracle adapters.

This module has no filesystem, network or execution capabilities. Its adapters
are reviewed application code, not instructions supplied by model responses.
Conditional repair capacity is not a statistical repeat plan. Every failed
attempt remains visible even when a later candidate passes its fixed oracle.
"""
from copy import deepcopy
from hashlib import sha256
import math
import re
import time

from qa.reporting import combine
from qa.stability.budget import BudgetError, BudgetLedger, BudgetStop, validate_limits


MAX_BYTES = 262144
REPAIRABLE = frozenset({'INVALID_MODEL_JSON', 'INVALID_MODEL_RESPONSE',
                       'INCOMPLETE_OR_UNKNOWN_SELECTION'})
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
_CODE = re.compile(r'[A-Z][A-Z0-9_]{0,95}')


class CycleError(ValueError):
    """A fixed diagnostic without raw input, exception or private-path content."""


def _require(value, code):
    if not value:
        raise CycleError(code)


def validate_plan(plan):
    _require(type(plan) is dict and set(plan) == {
        'schema_version', 'plan_id', 'limits', 'cases'}, 'CYCLE_PLAN_FIELDS')
    _require(plan['schema_version'] == '1.0' and type(plan['plan_id']) is str
             and _ID.fullmatch(plan['plan_id']), 'CYCLE_PLAN_ID')
    try:
        limits = validate_limits(plan['limits'])
    except BudgetError:
        raise CycleError('CYCLE_PLAN_LIMITS') from None
    _require(limits['max_attempts'] <= 15, 'CYCLE_PLAN_LIMITS')
    _require(type(plan['cases']) is list and 1 <= len(plan['cases']) <= 5,
             'CYCLE_PLAN_CASES')
    names = set()
    for row in plan['cases']:
        _require(type(row) is dict and set(row) == {
            'case_id', 'max_attempts', 'reservation_tokens'}, 'CYCLE_PLAN_CASE_FIELDS')
        _require(type(row['case_id']) is str and _ID.fullmatch(row['case_id'])
                 and row['case_id'] not in names, 'CYCLE_PLAN_CASE_ID')
        names.add(row['case_id'])
        _require(type(row['max_attempts']) is int and 1 <= row['max_attempts'] <= 3,
                 'CYCLE_PLAN_CASE_ATTEMPTS')
        _require(type(row['reservation_tokens']) is int
                 and 1 <= row['reservation_tokens'] <= 10_000_000,
                 'CYCLE_PLAN_RESERVATION')
    return deepcopy(plan)


def _proposal(value):
    _require(type(value) is dict and set(value) == {'outcome', 'raw', 'tokens'},
             'CYCLE_PROPOSAL_FIELDS')
    _require(type(value['outcome']) is str and value['outcome'] in ('completed', 'error', 'timeout'),
             'CYCLE_PROPOSAL_OUTCOME')
    _require(value['tokens'] is None or type(value['tokens']) is int and value['tokens'] >= 0,
             'CYCLE_PROPOSAL_USAGE')
    if value['outcome'] == 'completed':
        _require(type(value['raw']) is str, 'CYCLE_PROPOSAL_CONTENT')
        try:
            size = len(value['raw'].encode('utf-8'))
        except UnicodeError:
            raise CycleError('CYCLE_PROPOSAL_CONTENT') from None
        _require(size <= MAX_BYTES, 'CYCLE_PROPOSAL_CONTENT')
    else:
        _require(value['raw'] is None, 'CYCLE_PROPOSAL_CONTENT')
    return deepcopy(value)


def _assessment(value):
    _require(type(value) is dict and set(value) == {'status', 'issues'}, 'CYCLE_ASSESSMENT_FIELDS')
    _require(type(value['status']) is str and value['status'] in ('PASS', 'FAIL', 'ERROR', 'INCOMPLETE'),
             'CYCLE_ASSESSMENT_STATUS')
    issues = value['issues']
    _require(type(issues) is list and len(issues) <= 64
             and all(type(code) is str and _CODE.fullmatch(code) for code in issues)
             and len(issues) == len(set(issues)), 'CYCLE_ASSESSMENT_ISSUES')
    _require((value['status'] == 'PASS') == (not issues), 'CYCLE_ASSESSMENT_COHERENCE')
    return deepcopy(value)


class _Stop(Exception):
    def __init__(self, reason, status):
        self.reason, self.status = reason, status


def run_cycle(plan, propose, assess, *, clock=time.monotonic, emit=lambda event: None):
    """Run fixed case order with cooperative admission and immutable expectations.

    ``propose(case_id, index, feedback_tuple, absolute_deadline)`` returns raw
    candidate text and usage. ``assess(case_id, raw)`` returns only fixed-oracle
    status and codes; providers never supply their own assessment. ``emit`` must
    durably store each detached event before returning. An emission failure stops
    admission immediately. Process-level deadlines belong to the outer runner.
    """
    plan = validate_plan(plan)
    _require(all(callable(fn) for fn in (propose, assess, clock, emit)), 'CYCLE_CALLBACK_REQUIRED')
    first_clock = []

    def observed_clock():
        value = clock()
        if not first_clock:
            first_clock.append(value)
        return value

    try:
        ledger = BudgetLedger(plan['limits'], clock=observed_clock)
        deadline = first_clock[0] + plan['limits']['wall_seconds']
        _require(math.isfinite(deadline), 'CYCLE_CLOCK_INVALID')
    except (BudgetError, OverflowError):
        raise CycleError('CYCLE_CLOCK_INVALID') from None
    events, states, cases = [], [], []
    provider_calls, global_reason, emission_failed = 0, None, False

    def publish(kind, **data):
        nonlocal emission_failed
        event = {'sequence': len(events) + 1, 'event': kind, **deepcopy(data)}
        events.append(event)
        try:
            emit(deepcopy(event))
        except KeyboardInterrupt:
            emission_failed = True
            raise _Stop('INTERRUPTED', 'INCOMPLETE') from None
        except Exception:
            emission_failed = True
            raise _Stop('JOURNAL_WRITE_FAILED', 'ERROR') from None

    def budget_state():
        value = ledger.snapshot()
        if value['status'] == 'ERROR':
            raise _Stop(value['error_code'], 'ERROR')
        return value

    for definition in plan['cases']:
        case = {'case_id': definition['case_id'], 'max_attempts': definition['max_attempts'],
                'first_attempt_status': 'NOT_RUN', 'final_candidate_status': 'NOT_RUN',
                'termination_reason': global_reason or 'NOT_RUN', 'attempts': [], 'slots': []}
        cases.append(case)
        if global_reason:
            continue
        feedback, rejected = (), set()
        try:
            for index in range(1, definition['max_attempts'] + 1):
                try:
                    reservation = ledger.reserve(definition['reservation_tokens'])
                except BudgetStop as exc:
                    raise _Stop(exc.code, 'INCOMPLETE') from None
                except BudgetError as exc:
                    raise _Stop(exc.code, 'ERROR') from None
                attempt = {'attempt_index': index, 'reservation_id': reservation,
                           'feedback': list(feedback), 'provider_called': False,
                           'outcome': 'not_called', 'raw': None, 'raw_sha256': None,
                           'tokens': None, 'assessment': None, 'status': 'INCOMPLETE'}
                case['attempts'].append(attempt)
                try:
                    publish('RESERVED', case_id=case['case_id'], attempt_index=index,
                            reservation_id=reservation, reservation_tokens=definition['reservation_tokens'],
                            feedback=list(feedback), ledger=budget_state())
                    if budget_state()['remaining_wall_seconds'] <= 0:
                        raise _Stop('BUDGET_WALL_LIMIT_REACHED', 'INCOMPLETE')
                    attempt['provider_called'] = True
                    provider_calls += 1
                    try:
                        proposal = _proposal(propose(case['case_id'], index, feedback, deadline))
                    except KeyboardInterrupt:
                        raise _Stop('INTERRUPTED', 'INCOMPLETE') from None
                    except CycleError as exc:
                        raise _Stop(str(exc), 'ERROR') from None
                    except Exception:
                        raise _Stop('PROPOSER_ERROR', 'ERROR') from None
                    attempt.update(proposal)
                    if proposal['raw'] is not None:
                        attempt['raw_sha256'] = sha256(proposal['raw'].encode('utf-8')).hexdigest()
                    try:
                        accounted = ledger.finish(reservation, outcome=proposal['outcome'], tokens=proposal['tokens'])
                    except BudgetError as exc:
                        raise _Stop(exc.code, 'ERROR') from None
                    attempt['ledger'] = accounted
                    publish('PROPOSED', case_id=case['case_id'], **attempt)
                    if accounted['status'] == 'FAIL':
                        raise _Stop(accounted['stop_reason'], 'FAIL')
                    if proposal['outcome'] != 'completed':
                        raise _Stop('TRANSPORT_' + proposal['outcome'].upper(),
                                    'INCOMPLETE' if proposal['outcome'] == 'timeout' else 'ERROR')
                    if proposal['tokens'] is None:
                        raise _Stop('UNKNOWN_USAGE', 'INCOMPLETE')
                    try:
                        assessment = _assessment(assess(case['case_id'], proposal['raw']))
                    except KeyboardInterrupt:
                        raise _Stop('INTERRUPTED', 'INCOMPLETE') from None
                    except CycleError as exc:
                        raise _Stop(str(exc), 'ERROR') from None
                    except Exception:
                        raise _Stop('ASSESSOR_ERROR', 'ERROR') from None
                    attempt.update(assessment=assessment, status=assessment['status'])
                    case['final_candidate_status'] = assessment['status']
                    publish('ASSESSED', case_id=case['case_id'], **attempt)
                    after = budget_state()
                    if after['elapsed_seconds'] > plan['limits']['wall_seconds']:
                        raise _Stop('BUDGET_WALL_LIMIT_REACHED', 'INCOMPLETE')
                    if assessment['status'] in ('ERROR', 'INCOMPLETE'):
                        raise _Stop('EVALUATOR_' + assessment['status'], assessment['status'])
                    if assessment['status'] == 'PASS':
                        case['termination_reason'] = 'CANDIDATE_VALIDATED'
                        break
                    if attempt['raw_sha256'] in rejected:
                        case['termination_reason'] = 'NO_PROGRESS'
                        break
                    rejected.add(attempt['raw_sha256'])
                    if not set(assessment['issues']) <= REPAIRABLE:
                        case['termination_reason'] = 'EVALUATOR_REJECTED'
                        break
                    feedback = tuple(assessment['issues'])
                    case['termination_reason'] = 'CASE_ATTEMPT_LIMIT'
                except _Stop as stop:
                    attempt['status'] = combine([attempt['status'], stop.status])
                    attempt['stop_reason'] = stop.reason
                    if ledger.snapshot()['active_reservation'] is not None:
                        try:
                            ledger.finish(reservation, outcome='error',
                                          tokens=None if attempt['provider_called'] else 0)
                        except BudgetError:
                            pass  # The active conservative reservation remains visible.
                    attempt['ledger'] = ledger.snapshot()
                    raise
            publish('CASE_STOPPED', case_id=case['case_id'],
                    termination_reason=case['termination_reason'],
                    final_candidate_status=case['final_candidate_status'])
        except _Stop as stop:
            global_reason = case['termination_reason'] = stop.reason
            states.append(stop.status)
            if case['final_candidate_status'] == 'NOT_RUN' and case['attempts']:
                case['final_candidate_status'] = stop.status
    final_ledger = ledger.snapshot()
    if final_ledger['status'] == 'ERROR':
        global_reason = final_ledger['error_code']
    if not emission_failed:
        try:
            publish('CYCLE_STOPPED', termination_reason=global_reason or 'ALL_CASES_FINISHED',
                    ledger=final_ledger)
        except _Stop as stop:
            global_reason = stop.reason
            states.append(stop.status)
    # A durable journal callback is part of the owned cycle. It can consume
    # time even after the last provider reservation has been settled, when a
    # plain ledger snapshot would otherwise leave accounting status PASS.
    final_ledger = ledger.snapshot()
    if final_ledger['status'] == 'ERROR':
        global_reason = global_reason or final_ledger['error_code']
        states.append('ERROR')
    elif final_ledger['elapsed_seconds'] > plan['limits']['wall_seconds']:
        global_reason = global_reason or 'BUDGET_WALL_LIMIT_REACHED'
        states.append('INCOMPLETE')
    for case in cases:
        attempts = case['attempts']
        case['first_attempt_status'] = attempts[0]['status'] if attempts else 'NOT_RUN'
        for index in range(1, case['max_attempts'] + 1):
            attempted = attempts[index - 1] if index <= len(attempts) else None
            case['slots'].append({'attempt_index': index,
                'status': 'ATTEMPTED' if attempted and attempted['provider_called'] else
                          'NOT_NEEDED' if not attempted and case['termination_reason'] == 'CANDIDATE_VALIDATED' else 'NOT_RUN',
                'reason': 'PROVIDER_CALLED' if attempted and attempted['provider_called'] else case['termination_reason']})
        case['status'] = combine([row['status'] for row in attempts]) if attempts else 'INCOMPLETE'
        states.append(case['status'])
    states.append(final_ledger['status'])
    return {'schema_version': '1.0', 'plan_id': plan['plan_id'], 'status': combine(states),
            'planned_case_count': len(cases),
            'maximum_attempt_slots': sum(row['max_attempts'] for row in cases),
            'attempted_calls': final_ledger['attempted_calls'], 'provider_calls': provider_calls,
            'cases': cases, 'ledger': final_ledger,
            'termination_reason': global_reason or 'ALL_CASES_FINISHED',
            'observed_failed_attempts': [{'case_id': case['case_id'], 'attempt_index': row['attempt_index'],
                                          'status': row['status']} for case in cases for row in case['attempts']
                                         if row['status'] in ('FAIL', 'ERROR')],
            'events': events, 'journal_status': 'ERROR' if emission_failed else 'PASS',
            'candidate_admission': 'NOT_EVALUATED', 'product_acceptance': 'INCOMPLETE',
            'scope': 'Bounded conditional response repair; all attempts retained, no source edits or release approval'}
