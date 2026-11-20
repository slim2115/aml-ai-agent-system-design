"""Declared controls for the real bounded controller and candidate oracle."""
from qa.autonomy.candidates import prepare_cases
from qa.autonomy.cycle import run_cycle
from qa.scenario_checks import canonical


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


EXPECTATIONS = {
    'valid': ('PASS', 1, ['PASS']),
    'repair': ('FAIL', 2, ['PASS']),
    'no_progress': ('FAIL', 2, ['FAIL']),
    'attempt_limit': ('FAIL', 2, ['FAIL']),
    'call_limit': ('INCOMPLETE', 1, ['PASS', 'NOT_RUN']),
    'token_limit': ('INCOMPLETE', 0, ['NOT_RUN']),
    'timeout': ('INCOMPLETE', 1, ['INCOMPLETE']),
    'unknown_usage': ('INCOMPLETE', 1, ['INCOMPLETE']),
    'reservation_breach': ('FAIL', 1, ['FAIL']),
    'deadline': ('FAIL', 1, ['FAIL']),
    'oracle_error': ('ERROR', 1, ['ERROR']),
}


def run_demo(*, root, emit, guard=lambda: None):
    controls = []
    for name, expected in EXPECTATIONS.items():
        guard()
        cases = prepare_cases(['QA-S01', 'QA-S03'] if name == 'call_limit' else ['QA-S01'], root)
        baseline = cases.baseline()
        if baseline['status'] != 'PASS':
            raise ValueError('AUTONOMY_BASELINE_REJECTED')
        clock = Clock()
        limits = {'max_attempts': 1 if name == 'call_limit' else 3,
                  'max_tokens': 9 if name == 'token_limit' else 30, 'wall_seconds': 5.0}
        plan = {'schema_version': '1.0', 'plan_id': name, 'limits': limits,
                'cases': cases.plan_cases(2, 10)}

        def propose(case_id, index, feedback, deadline):
            guard()
            clock.value += 6 if name == 'deadline' else .1
            payload = cases.payload(case_id)
            raw = canonical({'schema_version': '1.0',
                'fact_order': [row['fact_id'] for row in reversed(payload['facts'])],
                'rule_order': list(reversed(payload['rule_keys']))})
            if name == 'no_progress' or name == 'repair' and index == 1:
                raw = '{}'
            elif name == 'attempt_limit':
                raw = '{}' if index == 1 else '{'
            return {'outcome': 'timeout' if name == 'timeout' else 'completed',
                    'raw': None if name == 'timeout' else raw,
                    'tokens': None if name == 'unknown_usage' else 11 if name == 'reservation_breach' else 4}

        def assess(case_id, raw):
            guard()
            if name == 'oracle_error':
                return {'status': 'ERROR', 'issues': ['CONTROLLED_ORACLE_ERROR']}
            return cases.assess(case_id, raw)

        cycle = run_cycle(plan, propose, assess, clock=clock,
                          emit=lambda event: emit({'control_id': name, **event}))
        observed = (cycle['status'], cycle['provider_calls'],
                    [case['final_candidate_status'] for case in cycle['cases']])
        if name == 'repair':
            preserved = cycle['cases'][0]['first_attempt_status'] == 'FAIL' and len(cycle['observed_failed_attempts']) == 1
        elif name == 'no_progress':
            preserved = cycle['cases'][0]['termination_reason'] == 'NO_PROGRESS'
        else:
            preserved = True
        controls.append({'id': name, 'expected': list(expected), 'observed': list(observed),
                         'status': 'PASS' if observed == expected and preserved else 'FAIL',
                         'plan': plan, 'baseline': baseline, 'cycle': cycle, 'reports': cases.reports})
    return {'status': 'PASS' if all(row['status'] == 'PASS' for row in controls) else 'FAIL',
            'controls': controls, 'control_count': len(controls), 'new_model_calls': 0,
            'scope': 'Synthetic provider and virtual time; real product renderer, fixed oracle and bounded controller',
            'product_acceptance': 'INCOMPLETE'}
