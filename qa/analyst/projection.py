"""Explicit display projections: no executable/report HTML or raw model payloads."""
from qa.analyst.reader import require

MAX_ROWS = 1000
FIELDS = ('mode', 'execution_status', 'deterministic_status', 'plan_fulfillment', 'version_binding',
          'qualification_status', 'fault_injection_status', 'scenario_status', 'canary_status',
          'analysis_status', 'observed_failure', 'error_code', 'source_digest', 'environment_digest',
          'started_at_utc', 'finished_at_utc', 'new_model_calls', 'budget_enforcement',
          'human_calibration', 'historical_judge', 'remote_ci')


def scalar(value):
    require(value is None or type(value) in (str, int, float, bool), 'ANALYST_FIELD_INVALID')
    if type(value) is str:
        require(len(value) <= 4096, 'ANALYST_TEXT_LIMIT')
    return value


def record(value):
    require(type(value) is dict, 'ANALYST_RECORD_INVALID')
    return value


def rows(value):
    require(type(value) is list and len(value) <= MAX_ROWS, 'ANALYST_ROWS_INVALID')
    return value


def selected(value, fields):
    record(value)
    return {key: scalar(value[key]) for key in fields if key in value}


def codes(value):
    items = rows(value)
    require(all(type(item) is str and len(item) <= 256 for item in items), 'ANALYST_CODES_INVALID')
    return ', '.join(items)


def table(view, title, values, fields):
    result = []
    for value in rows(values):
        row = selected(value, fields)
        if 'issues' in value:
            row['issues'] = codes(value['issues'])
        result.append(row)
    view['tables'][title] = result


def _cycle(view, cycle, label):
    record(cycle)
    for case in rows(cycle.get('cases', [])):
        view['tables'].setdefault('Случаи и исправления', []).append(dict(
            control=label, **selected(case, ('case_id', 'status', 'first_attempt_status',
                'final_candidate_status', 'termination_reason'))))
        for attempt in rows(case.get('attempts', [])):
            data = dict(control=label, case_id=scalar(case.get('case_id')),
                        **selected(attempt, ('attempt_index', 'status', 'outcome', 'tokens', 'raw_sha256')))
            assessment = attempt.get('assessment')
            data['issues'] = codes(record(assessment).get('issues', [])) if assessment is not None else 'NOT_ASSESSED'
            view['tables'].setdefault('Попытки', []).append(data)
    ledger = record(cycle.get('ledger', {}))
    view['tables'].setdefault('Бюджет', []).append(dict(control=label, **selected(ledger,
        ('status', 'attempted_calls', 'known_tokens', 'charged_tokens', 'total_tokens',
         'unknown_usage_attempts', 'elapsed_seconds', 'stop_reason', 'error_code')),
        **selected(record(ledger.get('limits', {})), ('max_attempts', 'max_tokens', 'wall_seconds'))))
    for event in rows(cycle.get('events', [])):
        view['tables'].setdefault('Хронология', []).append(dict(control=label,
            **selected(event, ('sequence', 'event', 'case_id', 'attempt_index', 'status', 'reason', 'tokens'))))


def _statistics(view, stats):
    record(stats)
    view['summary'].update(selected(stats, ('planned_slots', 'observed_slots', 'missing_slots', 'not_run_slots')))
    arms = record(stats.get('arms', {}))
    require(len(arms) <= 2, 'ANALYST_ARMS_INVALID')
    for name, arm in arms.items():
        scalar(name)
        data = dict(arm=name, **selected(arm, ('status', 'planned_slots', 'observed_slots', 'missing_slots',
                                             'not_run_slots', 'planned_family_count', 'interval_status')))
        data.update({'pass_' + key: value for key, value in selected(record(arm.get('pass_rate', {})),
                                                                     ('numerator', 'denominator')).items()})
        data.update(selected(record(arm.get('latency', {})), ('p50_seconds', 'p95_seconds')))
        view['tables'].setdefault('Повторы и полнота', []).append(data)
        for kind in ('cases', 'families'):
            for value in rows(arm.get(kind, [])):
                view['tables'].setdefault('Случаи и семейства', []).append(dict(arm=name, kind=kind,
                    **selected(value, ('case_id', 'family_id', 'status', 'planned_slots', 'observed_slots',
                                       'missing_slots', 'not_run_slots', 'case_count', 'passed_all_slots'))))
        usage = record(arm.get('usage', {}))
        for name_usage in ('prompt_tokens', 'output_tokens'):
            view['tables'].setdefault('Расход исторических попыток', []).append(dict(arm=name, unit=name_usage,
                **selected(record(usage.get(name_usage, {})),
                           ('known_sum', 'known_attempts', 'unknown_attempts', 'recorded_total'))))


def project(view, result, artifact):
    view['summary'] = selected(result, FIELDS)
    view['summary']['recorded_product_acceptance'] = scalar(result.get('product_acceptance'))
    view['summary']['recorded_candidate_admission'] = scalar(result.get('candidate_admission'))
    if 'process' in result:
        view['summary'].update({'process_' + key: value for key, value in selected(record(result['process']),
            ('cleanup_status', 'timed_out', 'interrupted', 'elapsed_seconds')).items()})
    kind = view['kind']
    if kind == 'execution':
        profiles = rows(result.get('profiles', []))
        table(view, 'Профили проверок', profiles, ('id', 'status', 'observed_failure'))
        for row, original in zip(view['tables']['Профили проверок'], profiles):
            row.update(selected(record(original.get('metrics', {})),
                ('collected_tests', 'additional_subtests', 'failures', 'errors', 'skipped', 'checks', 'passed')))
        view['summary']['pending_profiles'] = codes(result.get('pending_profiles', []))
        plan = artifact('plan.json', optional=True)
        if plan is not None:
            table(view, 'Требования в плане', record(plan).get('requirements', []), ('id', 'priority', 'title'))
            impacts = []
            for row in rows(plan.get('profiles', [])):
                for reason in rows(row.get('reasons', [])):
                    detail = {'reason': scalar(reason)} if type(reason) is str else selected(reason, ('input', 'component', 'reason'))
                    impacts.append(dict(profile=scalar(row.get('id')), **detail))
            table(view, 'Почему выбраны проверки', impacts, ('profile', 'input', 'component', 'reason'))
    elif kind == 'evaluator-qualification':
        table(view, 'Контроли оценщиков', record(result.get('baseline', {})).get('matrix', []),
              ('id', 'evaluator_id', 'kind', 'status'))
        table(view, 'Внесённые дефекты оценщиков', record(result.get('fault_injections', {})).get('rows', []),
              ('id', 'status', 'control_id'))
    elif kind == 'environments':
        table(view, 'Сценарии окружения', record(result.get('assessment', {})).get('matrix', []),
              ('id', 'status', 'planned_steps', 'observed_steps'))
        table(view, 'Контрольные подмены', record(result.get('canaries', {})).get('rows', []), ('id', 'status'))
    elif kind == 'stability':
        _statistics(view, result.get('statistics', {}))
        table(view, 'Контроли статистики и бюджета', result.get('control_results', []), ('id', 'status'))
    elif kind == 'autonomy':
        require(result.get('mode') in ('demo', 'ollama'), 'ANALYST_MODE_INVALID')
        if result['mode'] == 'demo':
            demo = artifact('demo.json', optional=result['status'] not in ('PASS', 'FAIL'))
            if demo is not None:
                controls = rows(record(demo).get('controls', []))
                table(view, 'Учебные контроли', controls, ('id', 'status'))
                for control in controls:
                    _cycle(view, control['cycle'], scalar(control['id']))
        else:
            cycle = artifact('cycle.json', optional=result['status'] not in ('PASS', 'FAIL'))
            if cycle is not None:
                _cycle(view, cycle, 'ollama')
    # Bound combined tables as well as each individual nested list.
    require(sum(len(value) for value in view['tables'].values()) <= 5000, 'ANALYST_TABLE_LIMIT')
