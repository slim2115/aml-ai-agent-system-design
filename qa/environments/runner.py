"""Owned execution and evidence for the fixed stateful environment schedules."""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from qa.environments import models
from qa.execution.provenance import compare_snapshots, environment, snapshot
from qa.execution.runner import pin_file, verify_files
from qa.qualification.runner import exact, json_value
from qa.reporting import combine, strict_json
from qa.requirements.catalog import ROOT, canonical_digest
from src.qa_profile import checked_path, get_qa_profile
from src.workflow import store as workflow_store

CATALOG = 'qa/environments/catalog-v1.json'
CANARIES = ('drop_step', 'alter_state', 'break_invariant')
EXIT = {'PASS': 0, 'FAIL': 1, 'ERROR': 2, 'INCOMPLETE': 3}
TRACE_ACTION_CALLS = {
    'receive': {'db.enqueue', 'db.receipt', 'offset.attempt', 'offset.ack',
                'dlq.publish', 'dlq.ack'},
    'tick': {'db.read.submitted', 'db.read.queued', 'db.project', 'db.projection.receipt',
             'db.defer', 'http.submit', 'http.get', 'sqlite.admission.receipt', 'clock.expired'},
    'finish': {'sqlite.terminal.refused'}, 'restart': set(),
}
FAULT_PREDECESSORS = {
    'db_unavailable': {'db.enqueue'}, 'offset_ack_lost': {'offset.attempt'},
    'dlq_unavailable': {'dlq.publish'}, 'http_ack_lost': {'sqlite.admission.receipt'},
    'projection_unavailable': {'db.project'}, 'budget_after_admission': {'sqlite.admission.receipt'},
    'get_not_found': {'http.get'}, 'protocol_error': {'sqlite.admission.receipt', 'http.get'},
}


class EnvironmentRunError(ValueError):
    """Fixed diagnostic code, never raw dependency messages."""


def require(condition, code):
    if not condition:
        raise EnvironmentRunError(code)


def load_catalog(root=ROOT):
    path = checked_path(Path(root) / CATALOG, root)
    require(path.is_file() and path.stat().st_size <= 1024 * 1024, 'ENVIRONMENT_CATALOG_FILE')
    return models.validate_catalog(strict_json(path.read_text('utf-8-sig')))


def rate(numerator, denominator):
    return {'numerator': numerator, 'denominator': denominator,
            'value': numerator / denominator if denominator else None}


def assess_trace(definition, value):
    """Check normalized evidence independently of the model's reported flags.

    This checks ordering and projection consistency, not authenticity of logs or
    raw idempotency keys/job IDs (which remain the model's SQLite-level check).
    """
    trace = value['trace']
    require(type(trace) is list and 0 < len(trace) <= 512, 'ENVIRONMENT_TRACE')
    require(all(type(item) is dict and set(item) == {'step', 'call'}
                and type(item['step']) is int and 0 <= item['step'] < len(definition['steps'])
                and type(item['call']) is str for item in trace), 'ENVIRONMENT_TRACE')
    indices = [item['step'] for item in trace]
    require(indices == sorted(indices) and set(indices) == set(range(len(definition['steps']))),
            'ENVIRONMENT_TRACE_STEP_ORDER')
    durable, bounded, stable, consistent = True, True, True, True
    totals = Counter()
    for number, step in enumerate(definition['steps']):
        calls = [item['call'] for item in trace if item['step'] == number]
        marker = 'action.' + step['action']
        allowed = TRACE_ACTION_CALLS[step['action']] | {marker}
        if step.get('fault'):
            allowed.add('fault.' + step['fault'])
        require(calls[0] == marker and calls.count(marker) == 1 and set(calls) <= allowed,
                'ENVIRONMENT_TRACE_CALL')
        fault = step.get('fault')
        require(sum(call.startswith('fault.') for call in calls) == int(fault is not None),
                'ENVIRONMENT_TRACE_FAULT')
        consistent &= len(calls) == len(set(calls))
        for index, call in enumerate(calls):
            prior = calls[:index]
            if call.startswith('fault.'):
                consistent &= index > 0 and calls[index - 1] in FAULT_PREDECESSORS[fault]
            if call == 'offset.attempt':
                durable &= ('dlq.ack' if step.get('invalid', False) else 'db.receipt') in prior
            if call == 'clock.expired':
                consistent &= 'fault.budget_after_admission' in prior
                bounded &= not any(later.startswith(('db.', 'http.', 'sqlite.admission'))
                                   for later in calls[index + 1:])
            predecessors = {
                'db.receipt': {'db.enqueue'}, 'offset.ack': {'offset.attempt'},
                'dlq.ack': {'dlq.publish'}, 'http.submit': {'db.read.queued'},
                'http.get': {'db.read.submitted'}, 'sqlite.admission.receipt': {'http.submit'},
                'db.project': {'http.submit', 'http.get'},
                'db.projection.receipt': {'db.project'}, 'db.defer': {'http.submit', 'http.get'},
                'db.read.queued': {'db.read.submitted'},
            }
            if call in predecessors:
                consistent &= bool(set(prior) & predecessors[call])
            if call == 'db.project' and 'http.submit' in prior:
                consistent &= 'sqlite.admission.receipt' in prior
        if step['action'] == 'receive':
            forbidden = {'db.enqueue', 'db.receipt'} if step.get('invalid', False) else {'dlq.publish', 'dlq.ack'}
            consistent &= not set(calls) & forbidden
        for failed, receipt in (('db_unavailable', 'db.receipt'), ('offset_ack_lost', 'offset.ack'),
                                ('dlq_unavailable', 'dlq.ack'), ('projection_unavailable', 'db.projection.receipt')):
            if fault == failed:
                consistent &= receipt not in calls
        if fault == 'budget_after_admission':
            consistent &= 'clock.expired' in calls
        if step['action'] == 'finish':
            consistent &= calls == [marker, 'sqlite.terminal.refused']
        totals.update(calls)
        # Missing or ill-typed projections are handled by exact step comparison;
        # never erase a missing step from the planned denominator here.
        actual = value['steps'][number] if number < len(value['steps']) else None
        if (type(actual) is dict and type(actual.get('outcome')) is list
                and all(type(outcome) is str for outcome in actual['outcome'])):
            # A terminal projection cannot be justified by final state alone:
            # the acknowledged persistence operations must exist in this step.
            required_calls = {
                'queued': {'db.enqueue', 'db.receipt', 'offset.attempt', 'offset.ack'},
                'duplicate': {'db.enqueue', 'db.receipt', 'offset.attempt', 'offset.ack'},
                'dead_lettered': {'dlq.publish', 'dlq.ack', 'offset.attempt', 'offset.ack'},
                'submitted': {'http.submit', 'sqlite.admission.receipt', 'db.project', 'db.projection.receipt'},
                'projected': {'http.get', 'db.project', 'db.projection.receipt'},
            }
            for outcome in actual['outcome']:
                consistent &= required_calls.get(outcome, set()) <= set(calls)
                if outcome.startswith('deferred:'):
                    consistent &= 'db.defer' in calls
        counters = ('job_count', 'key_count', 'submit_calls', 'offset_acks', 'dlq_acks')
        if type(actual) is dict and all(type(actual.get(key)) is int for key in counters):
            stable &= (0 <= actual['job_count'] <= 1 and 0 <= actual['key_count'] <= 1)
            consistent &= (actual['submit_calls'] == totals['http.submit']
                           and actual['offset_acks'] == totals['offset.ack']
                           and actual['dlq_acks'] == totals['dlq.ack']
                           and actual['key_count'] == int(totals['http.submit'] > 0)
                           and actual['job_count'] == int(totals['sqlite.admission.receipt'] > 0))
    return {'consistent': bool(consistent), 'invariants': [
        {'id': 'durable_receipt_before_offset', 'passed': bool(durable)},
        {'id': 'stable_idempotency', 'passed': bool(stable)},
        {'id': 'no_admission_after_budget', 'passed': bool(bounded)},
    ]}


def assess(catalog, observations):
    """Compare all planned actions without discarding missing or failed records."""
    models.validate_catalog(catalog)
    planned = {row['id']: row for row in catalog['scenarios']}
    require(type(observations) is list and len(observations) <= 128, 'ENVIRONMENT_OBSERVATIONS')
    indexed, issues = {}, []
    for item in observations:
        if type(item) is not dict or type(item.get('id')) is not str:
            issues.append('MALFORMED_SCENARIO_RECORD')
            continue
        if item['id'] not in planned:
            issues.append('UNREGISTERED_SCENARIO')
            continue
        indexed.setdefault(item['id'], []).append(item)
    matrix = []
    for identity, definition in planned.items():
        row = {'id': identity, 'status': 'INCOMPLETE', 'issues': [],
               'steps': [{'index': index + 1, 'status': 'INCOMPLETE', 'expected': deepcopy(expected),
                          'observed': None} for index, expected in enumerate(definition['expected'])],
               'planned_steps': len(definition['expected']), 'observed_steps': 0, 'matched_steps': 0}
        records = indexed.get(identity, [])
        if not records:
            row['issues'] = ['MISSING_SCENARIO']
        elif len(records) != 1:
            row.update(status='ERROR', issues=['DUPLICATE_SCENARIO'])
        elif set(records[0]) == {'id', 'error_code'} and records[0]['error_code'] == 'SCENARIO_EXECUTION_ERROR':
            row.update(status='ERROR', issues=['SCENARIO_EXECUTION_ERROR'])
        elif set(records[0]) != {'id', 'observation'}:
            row.update(status='ERROR', issues=['MALFORMED_SCENARIO_RECORD'])
        else:
            try:
                value = records[0]['observation']
                json_value(value)
                require(type(value) is dict and set(value) == {'steps', 'invariants', 'trace', 'dependencies'},
                        'ENVIRONMENT_OBSERVATION_FIELDS')
                steps, invariants = value['steps'], value['invariants']
                require(type(steps) is list and len(steps) <= 64, 'ENVIRONMENT_OBSERVED_STEPS')
                require(type(invariants) is list and len(invariants) == len(models.REQUIRED_INVARIANTS),
                        'ENVIRONMENT_INVARIANTS')
                require(all(type(item) is dict and set(item) == {'id', 'passed'}
                            and type(item['id']) is str and type(item['passed']) is bool for item in invariants),
                        'ENVIRONMENT_INVARIANTS')
                invariant_ids = [item['id'] for item in invariants]
                require(len(set(invariant_ids)) == len(invariant_ids)
                        and set(invariant_ids) == set(models.REQUIRED_INVARIANTS), 'ENVIRONMENT_INVARIANTS')
                require(exact(value['dependencies'], {'real': list(models.REAL_DEPENDENCIES),
                                                      'modeled': list(models.MODELED_DEPENDENCIES)}),
                        'ENVIRONMENT_DEPENDENCIES')
                trace_result = assess_trace(definition, value)
                row.update(observed_steps=len(steps), invariants=deepcopy(invariants),
                           trace_consistency=trace_result['consistent'],
                           trace_invariants=trace_result['invariants'])
                states = []
                for index, expected in enumerate(definition['expected']):
                    actual = steps[index] if index < len(steps) else None
                    state = 'INCOMPLETE' if index >= len(steps) else 'PASS' if exact(expected, actual) else 'FAIL'
                    row['steps'][index] = {'index': index + 1, 'status': state,
                                          'expected': deepcopy(expected), 'observed': deepcopy(actual)}
                    states.append(state)
                row['matched_steps'] = states.count('PASS')
                if len(steps) > len(definition['expected']):
                    states.append('ERROR')
                    row['issues'].append('UNEXPECTED_EXTRA_STEPS')
                if any(not item['passed'] for item in invariants):
                    states.append('FAIL')
                    row['issues'].append('INVARIANT_VIOLATION')
                if (not trace_result['consistent']
                        or any(not item['passed'] for item in trace_result['invariants'])):
                    states.append('FAIL')
                    row['issues'].append('TRACE_CONTRADICTION')
                row['status'] = combine(states)
            except (ValueError, OverflowError):
                row.update(status='ERROR', issues=['INVALID_SCENARIO_OBSERVATION'])
        matrix.append(row)
    counts = Counter(row['status'] for row in matrix)
    planned_steps = sum(row['planned_steps'] for row in matrix)
    matched_steps = sum(row['matched_steps'] for row in matrix)
    metrics = {'planned_scenarios': len(planned), 'matched_scenarios': counts['PASS'],
               'failed_scenarios': counts['FAIL'], 'incomplete_scenarios': counts['INCOMPLETE'],
               'error_scenarios': counts['ERROR'], 'planned_steps': planned_steps,
               'observed_steps': sum(row['observed_steps'] for row in matrix), 'matched_steps': matched_steps,
               'scenario_match_rate': rate(counts['PASS'], len(planned)),
               'step_match_rate': rate(matched_steps, planned_steps)}
    return {'status': combine([row['status'] for row in matrix] + (['ERROR'] if issues else [])),
            'metrics': metrics, 'matrix': matrix, 'integrity_issues': sorted(set(issues)),
            'received_records': len(observations)}


def canaries(catalog, observations, baseline):
    """Corrupt copies of observations, not infrastructure or source code."""
    rows = []
    if baseline['status'] != 'PASS':
        return {'status': 'INCOMPLETE', 'planned': len(CANARIES), 'detected': 0,
                'rows': [{'id': name, 'status': 'NOT_RUN'} for name in CANARIES]}
    for name in CANARIES:
        altered = deepcopy(observations)
        target = altered[0]['id']
        value = altered[0]['observation']
        if name == 'drop_step':
            value['steps'].pop()
        elif name == 'alter_state':
            value['steps'][0]['job_count'] += 1
        else:
            value['invariants'][0]['passed'] = False
        result = assess(catalog, altered)
        detected = result['status'] != 'PASS' and any(row['id'] == target and row['status'] != 'PASS' for row in result['matrix'])
        rows.append({'id': name, 'status': 'PASS' if detected else 'FAIL', 'target_scenario': target,
                     'observed_status': result['status'], 'observations': altered, 'assessment': result})
    return {'status': combine([row['status'] for row in rows]), 'planned': len(CANARIES),
            'detected': sum(row['status'] == 'PASS' for row in rows), 'rows': rows,
            'scope': 'Observation fault injection, not live infrastructure chaos or source mutation coverage'}


def output_path(value, root=ROOT):
    root = Path(root).absolute()
    path = checked_path(Path(value) if Path(value).is_absolute() else root / value, root)
    parts = path.relative_to(root).parts
    require(len(parts) == 4 and parts[:3] == ('var', 'qa', 'environments')
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', parts[-1]), 'ENVIRONMENT_OUTPUT_SCOPE')
    require(not path.exists(), 'ENVIRONMENT_OUTPUT_EXISTS')
    return path


def workspace_path(run_id, root):
    # Fail instead of silently opening a user's existing QA/workflow database.
    require(get_qa_profile() is None, 'ENVIRONMENT_NESTED_PROFILE_UNSUPPORTED')
    base = Path(workflow_store.WORKFLOW_ROOT).absolute()
    require(base == Path(root).absolute() / 'var/workflow', 'ENVIRONMENT_WORKFLOW_ROOT')
    path = checked_path(base / 'environment-models' / run_id, root)
    require(not path.exists(), 'ENVIRONMENT_WORKSPACE_EXISTS')
    return path


def write_new(directory, name, value, root):
    raw = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    require(len(raw.encode('utf-8')) <= 8 * 1024 * 1024, 'ENVIRONMENT_OUTPUT_SIZE')
    with checked_path(directory / name, root).open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(raw)


def workspace_files(workspace, root):
    pending, files, visited = [workspace], [], 0
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            visited += 1
            require(visited <= 128, 'ENVIRONMENT_WORKSPACE_SIZE')
            checked_path(path, root)
            if path.is_dir():
                pending.append(path)
            else:
                files.append(pin_file(path, root))
    require(files, 'ENVIRONMENT_WORKSPACE_EMPTY')
    return sorted(files, key=lambda row: row['path'])


def render(result):
    lines = ['# Сценарные модели окружения', '',
             f"Итог: **{result['status']}**. Сценарии: **{result['scenario_status']}**. "
             f"Контрольные подмены наблюдений: **{result['canary_status']}**.", '',
             'Реальные функции планировщика, WorkflowClient и SQLite работают с моделями Kafka, PostgreSQL-очереди и транспорта. '
             'Виртуальное время не измеряет SLA; синтетическое завершение задания не является AML-анализом.', '',
             '| Сценарий | Статус | Совпало / запланировано шагов |', '| --- | --- | ---: |']
    for row in result.get('assessment', {}).get('matrix', []):
        lines.append(f"| {row['id']} | {row['status']} | {row['matched_steps']} / {row['planned_steps']} |")
    lines += ['', 'Полные expected/observed и коды находятся в `assessment.json`, '
              'порядок операций — в `observations.json`. Рабочие SQLite сохранены в указанном `workspace`.', '']
    for row in result.get('assessment', {}).get('matrix', []):
        details = result.get('scenario_definitions', {}).get(row['id'], {})
        lines += [f"## {row['id']}", '', details.get('purpose', ''), '',
                  'Ограничения: ' + '; '.join(details.get('limitations', [])), '',
                  '| Шаг | Совпадение | Ожидание | Наблюдение |', '| --- | --- | --- | --- |']
        for step in row['steps']:
            expected = json.dumps(step['expected'], ensure_ascii=False).replace('|', '&#124;').replace('\n', ' ')
            actual = json.dumps(step['observed'], ensure_ascii=False).replace('|', '&#124;').replace('\n', ' ')
            lines.append(f"| {step['index']} | {step['status']} | `{expected}` | `{actual}` |")
        lines.append('')
    lines += [f"Привязка к версии: **{result['version_binding']}**.", '',
              'Локальные хэши не являются независимой подписью. Живые Kafka/PostgreSQL/HTTP, модель, '
              'перезапуск процессов и человеческая приёмка здесь не проверяются. Product acceptance: INCOMPLETE.', '']
    if result.get('error_code'):
        lines.append('Ошибка запуска: `' + result['error_code'] + '`.')
    return '\n'.join(lines)


def run(output, *, root=ROOT):
    root = Path(root).absolute()
    directory = output_path(output, root)
    workspace = workspace_path(directory.name, root)
    # Both destinations are preflighted before either is created.
    directory.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    write_new(directory, 'started.json', {'status': 'INCOMPLETE', 'run_id': directory.name,
                                        'started_at_utc': started}, root)
    result = {'schema_version': '1.0', 'run_id': directory.name, 'status': 'ERROR',
              'scenario_status': 'INCOMPLETE', 'execution_status': 'INCOMPLETE', 'canary_status': 'INCOMPLETE',
              'version_binding': 'INCOMPLETE', 'product_acceptance': 'INCOMPLETE',
              'candidate_admission': 'NOT_EVALUATED',
              'human_calibration': 'NOT_MEASURED', 'model_quality': 'NOT_MEASURED',
              'live_integration': 'NOT_RUN', 'remote_ci': 'NOT_RUN', 'historical_judge': 'NOT_REASSESSED',
              'workspace': workspace.relative_to(root).as_posix(),
              'scope': 'Fixed synthetic stateful dependency models with real scheduler, client and SQLite'}
    before = env_before = None
    pins = []
    try:
        before, env_before = snapshot(root), environment()
        result.update(source_digest=before['digest'], environment_digest=env_before['digest'])
        catalog = load_catalog(root)
        result['catalog_digest'] = canonical_digest(catalog)
        result['scenario_definitions'] = {row['id']: {key: row[key] for key in
            ('title', 'purpose', 'limitations', 'requirements')} for row in catalog['scenarios']}
        for name, value in [('source-before.json', before), ('environment-before.json', env_before), ('catalog.json', catalog)]:
            write_new(directory, name, value, root)
            pins.append(pin_file(directory / name, root))
        require(compare_snapshots(before, snapshot(root))['status'] == 'PASS', 'ENVIRONMENT_SOURCE_CHANGED')
        workspace.mkdir(parents=True, exist_ok=False)
        observations = []
        for scenario in catalog['scenarios']:
            case_path = checked_path(workspace / scenario['id'], root)
            case_path.mkdir(exist_ok=False)
            try:
                value = models.execute(scenario, case_path)
                json_value(value)
                observations.append({'id': scenario['id'], 'observation': value})
            except Exception:
                observations.append({'id': scenario['id'], 'error_code': 'SCENARIO_EXECUTION_ERROR'})
        assessed = assess(catalog, observations)
        injected = canaries(catalog, observations, assessed)
        complete = not assessed['integrity_issues'] and all(
            row['status'] != 'ERROR' and row['observed_steps'] == row['planned_steps']
            for row in assessed['matrix'])
        result.update(scenario_status=assessed['status'], canary_status=injected['status'], assessment=assessed,
                      execution_status='PASS' if complete else 'INCOMPLETE',
                      canaries={key: value for key, value in injected.items() if key != 'rows'})
        result['canaries']['rows'] = [{key: value for key, value in row.items()
                                      if key not in {'observations', 'assessment'}} for row in injected['rows']]
        for name, value in [('observations.json', observations), ('assessment.json', assessed), ('canaries.json', injected)]:
            write_new(directory, name, value, root)
            pins.append(pin_file(directory / name, root))
        pins += workspace_files(workspace, root)
    except KeyboardInterrupt:
        result['error_code'] = 'INTERRUPTED'
    except Exception as exc:
        result['error_code'] = str(exc) if type(exc) is EnvironmentRunError else 'ENVIRONMENT_EXECUTION_ERROR'
    finally:
        stability = {'status': 'INCOMPLETE', 'added': [], 'removed': [], 'changed': []}
        env_stability = 'INCOMPLETE'
        try:
            if before is not None:
                after = snapshot(root)
                write_new(directory, 'source-after.json', after, root)
                pins.append(pin_file(directory / 'source-after.json', root))
                stability = compare_snapshots(before, after)
            if env_before is not None:
                after_env = environment()
                write_new(directory, 'environment-after.json', after_env, root)
                pins.append(pin_file(directory / 'environment-after.json', root))
                env_stability = 'PASS' if exact(env_before, after_env) else 'ERROR'
        except Exception:
            stability['status'] = 'ERROR'
            result.setdefault('error_code', 'ENVIRONMENT_FINAL_IDENTITY_ERROR')
        integrity = verify_files(pins, root)
        result.update(source_stability=stability, environment_stability=env_stability, evidence_integrity=integrity,
                      version_binding=combine([stability['status'], env_stability, integrity['status']]))
        result['status'] = combine([result['scenario_status'], result['canary_status'], result['version_binding']])
        if result.get('error_code'):
            result['status'] = 'ERROR'
        result.update(started_at_utc=started, finished_at_utc=datetime.now(timezone.utc).isoformat())
        write_new(directory, 'evidence.json', {'files': pins, 'integrity': integrity}, root)
        write_new(directory, 'result.json', result, root)
        with checked_path(directory / 'report.md', root).open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(render(result))
    print(json.dumps({key: result[key] for key in ('run_id', 'status', 'scenario_status', 'canary_status', 'version_binding')}))
    return EXIT[result['status']]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    try:
        return run(args.output_dir)
    except Exception as exc:
        print(json.dumps({'status': 'ERROR', 'error_code': str(exc) if type(exc) is EnvironmentRunError
                          else 'ENVIRONMENT_OUTPUT_ERROR', 'product_acceptance': 'INCOMPLETE'}))
        return 2
