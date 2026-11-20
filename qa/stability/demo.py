"""Small declared controls; no network, model, prices or performance claims."""
from copy import deepcopy

from qa.stability.budget import BudgetLedger, BudgetStop
from qa.stability.statistics import summarize


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


def panel():
    plan = {'schema_version': '1.0', 'plan_id': 'paired-demo-v1', 'evidence_kind': 'synthetic_control',
            'arms': ['baseline', 'candidate'], 'cases': [
                {'case_id': 'CASE-A', 'family_id': 'FAMILY-A', 'repeats': 2},
                {'case_id': 'CASE-B', 'family_id': 'FAMILY-A', 'repeats': 2},
                {'case_id': 'CASE-C', 'family_id': 'FAMILY-B', 'repeats': 2}]}
    rows = []
    for arm in plan['arms']:
        for case in plan['cases']:
            for repeat in (1, 2):
                rows.append({'arm': arm, 'case_id': case['case_id'], 'repeat_index': repeat,
                    'status': 'PASS', 'outcome': 'draft', 'semantic_fingerprint': 'a' * 64,
                    'ordered_fingerprint': ('b' if repeat == 2 else 'a') * 64,
                    'latency_seconds': 1.0, 'attempts': [{'status': 'completed',
                        'elapsed_seconds': 0.5, 'prompt_tokens': 6, 'output_tokens': 4}]})
    return plan, rows


def budget_control(kind):
    clock = Clock()
    limits = {'max_attempts': 2, 'max_tokens': 20, 'wall_seconds': 5.0}
    ledger = BudgetLedger(limits, clock=clock)
    actions = []
    first = ledger.reserve(10)
    actions.append({'action': 'reserve', 'id': first, 'tokens': 10})
    clock.value = 1.0
    usage = None if kind == 'unknown_usage' else 11 if kind == 'reservation_breach' else 10
    if kind == 'deadline':
        clock.value = 6.0
    outcome = 'error' if kind == 'failed_attempts' else 'completed'
    result = ledger.finish(first, outcome=outcome, tokens=usage)
    actions.append({'action': 'finish', 'outcome': outcome, 'tokens': usage})
    denied = False
    if kind in ('call_limit', 'failed_attempts'):
        second = ledger.reserve(10)
        clock.value = 2.0
        ledger.finish(second, outcome=outcome, tokens=10)
        actions += [{'action': 'reserve', 'id': second, 'tokens': 10},
                    {'action': 'finish', 'outcome': outcome, 'tokens': 10}]
    requested = 11 if kind == 'token_limit' else 10
    if kind != 'unknown_usage':
        try:
            ledger.reserve(requested)
        except BudgetStop:
            denied = True
        actions.append({'action': 'try_reserve', 'tokens': requested, 'denied': denied})
    result = ledger.snapshot()
    projection = {name: result[name] for name in ('status', 'attempted_calls', 'charged_tokens', 'unknown_usage_attempts')}
    projection['next_attempt_denied'] = denied
    expected = {
        'call_limit': ('PASS', 2, 20, 0, True),
        'failed_attempts': ('PASS', 2, 20, 0, True),
        'token_limit': ('PASS', 1, 10, 0, True),
        'unknown_usage': ('INCOMPLETE', 1, 10, 1, False),
        'reservation_breach': ('FAIL', 1, 11, 0, True),
        'deadline': ('FAIL', 1, 10, 0, True),
    }[kind]
    keys = ('status', 'attempted_calls', 'charged_tokens', 'unknown_usage_attempts', 'next_attempt_denied')
    return {'id': kind, 'expected': dict(zip(keys, expected)), 'observed': projection,
            'status': 'PASS' if projection == dict(zip(keys, expected)) else 'FAIL',
            'limits': limits, 'actions': actions, 'ledger': result}


def bounded_panel(plan, rows):
    """Fixed order, no retry-until-green; each remaining slot is explicit."""
    clock = Clock()
    ledger = BudgetLedger({'max_attempts': 8, 'max_tokens': 120, 'wall_seconds': 30.0}, clock=clock)
    observed, stopped = [], False
    for original in rows:
        row = deepcopy(original)
        if not stopped:
            try:
                reservation = ledger.reserve(10)
            except BudgetStop:
                stopped = True
        if stopped:
            row.update(status='NOT_RUN', outcome='not_run', semantic_fingerprint=None,
                       ordered_fingerprint=None, latency_seconds=None, attempts=[])
        else:
            clock.value += 1.0
            ledger.finish(reservation, outcome='completed', tokens=10)
        observed.append(row)
    return {'observations': observed, 'statistics': summarize(plan, observed), 'ledger': ledger.snapshot()}


def run_demo():
    plan, rows = panel()
    full = summarize(plan, rows)
    faulty = deepcopy(rows)
    faulty[-1]['status'] = 'FAIL'
    fault = summarize(plan, faulty)
    missing = summarize(plan, rows[:-1])
    controls = []
    for identity, value, status, observed, missing_slots in (
        ('complete_panel', full, 'PASS', 12, 0),
        ('known_failure', fault, 'FAIL', 12, 0),
        ('missing_repeat', missing, 'INCOMPLETE', 11, 1),
    ):
        expected = {'status': status, 'planned_slots': 12, 'observed_slots': observed, 'missing_slots': missing_slots}
        projection = {key: value[key] for key in expected}
        controls.append({'id': identity, 'expected': expected, 'observed': projection,
                         'status': 'PASS' if projection == expected else 'FAIL'})
    budgets = [budget_control(kind) for kind in ('call_limit', 'failed_attempts', 'token_limit',
                                               'unknown_usage', 'reservation_breach', 'deadline')]
    bounded = bounded_panel(plan, rows)
    observed = {key: bounded['statistics'][key] for key in ('status', 'planned_slots', 'not_run_slots')}
    expected = {'status': 'INCOMPLETE', 'planned_slots': 12, 'not_run_slots': 4}
    controls.append({'id': 'budget_stopped_panel', 'expected': expected, 'observed': observed,
                     'status': 'PASS' if observed == expected else 'FAIL'})
    return {'plan': plan, 'observations': rows, 'statistics': full,
            'qualification_status': 'PASS' if all(r['status'] == 'PASS' for r in controls + budgets) else 'FAIL',
            'controls': controls, 'budget_controls': budgets,
            'negative_observations': {'known_failure': faulty, 'missing_repeat': rows[:-1]},
            'negative_statistics': {'known_failure': fault, 'missing_repeat': missing},
            'budgeted_panel': bounded,
            'scope': 'Fixed synthetic controls and virtual clock; no model generation or live budget enforcement'}
