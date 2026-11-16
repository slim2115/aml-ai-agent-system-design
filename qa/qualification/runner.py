"""Qualify fixed evaluator controls and deliberately corrupted observations.

This process invokes owned Python functions only. Fault injections change copies
of their projections, never evaluator source, model outputs or historical data.
"""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re

from qa.execution.provenance import compare_snapshots, environment, snapshot
from qa.execution.runner import pin_file, verify_files
from qa.qualification import controls
from qa.reporting import combine, strict_json
from qa.requirements.catalog import ROOT, canonical_digest
from src.qa_profile import checked_path

CATALOG = 'qa/qualification/catalog-v1.json'
DECISIONS = {'PASS', 'FAIL', 'INCOMPLETE', 'REJECTED'}
KINDS = {'positive', 'negative', 'incomplete', 'invalid', 'invariant'}
INJECTIONS = ('force_pass', 'force_fail', 'drop_record', 'duplicate_record', 'foreign_record', 'alter_numeric')
EXIT = {'PASS': 0, 'FAIL': 1, 'ERROR': 2, 'INCOMPLETE': 3}


class QualificationError(ValueError):
    """Fixed, non-sensitive error codes."""


def require(condition, code):
    if not condition:
        raise QualificationError(code)


def text(value):
    return type(value) is str and 0 < len(value.strip()) <= 4000


def json_value(value, depth=0):
    require(depth <= 8, 'QUALIFICATION_JSON_DEPTH')
    if value is None or type(value) is bool:
        return
    if type(value) in (int, float):
        require(math.isfinite(value) and abs(value) <= 1e100, 'QUALIFICATION_JSON_NUMBER')
    elif type(value) is str:
        require(len(value) <= 4000, 'QUALIFICATION_JSON_TEXT')
        try:
            value.encode('utf-8')
        except UnicodeError:
            raise QualificationError('QUALIFICATION_JSON_TEXT') from None
    elif type(value) is list:
        require(len(value) <= 256, 'QUALIFICATION_JSON_LIST')
        for child in value:
            json_value(child, depth + 1)
    elif type(value) is dict:
        require(len(value) <= 64 and all(text(key) for key in value), 'QUALIFICATION_JSON_OBJECT')
        for child in value.values():
            json_value(child, depth + 1)
    else:
        raise QualificationError('QUALIFICATION_JSON_TYPE')


def exact(left, right):
    """JSON equality also distinguishes bool/int and int/float."""
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return left.keys() == right.keys() and all(exact(left[key], right[key]) for key in left)
    if type(left) is list:
        return len(left) == len(right) and all(exact(a, b) for a, b in zip(left, right))
    return left == right


def validate_catalog(catalog):
    require(type(catalog) is dict and set(catalog) == {'schema_version', 'catalog_id', 'evaluators', 'controls'},
            'QUALIFICATION_CATALOG_FIELDS')
    require(catalog['schema_version'] == '1.0' and catalog['catalog_id'] == 'evaluator-qualification-v1',
            'QUALIFICATION_CATALOG_VERSION')
    evaluators, rows = catalog['evaluators'], catalog['controls']
    require(type(evaluators) is list and 1 <= len(evaluators) <= 32, 'QUALIFICATION_EVALUATORS')
    ids = set()
    for evaluator in evaluators:
        require(type(evaluator) is dict and set(evaluator) == {'id', 'purpose', 'limitations', 'requirements'},
                'QUALIFICATION_EVALUATOR_FIELDS')
        name = evaluator['id']
        require(type(name) is str and re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', name)
                and name not in ids, 'QUALIFICATION_EVALUATOR_ID')
        ids.add(name)
        require(text(evaluator['purpose']), 'QUALIFICATION_EVALUATOR_PURPOSE')
        for field in ('limitations', 'requirements'):
            values = evaluator[field]
            require(type(values) is list and 1 <= len(values) <= 32 and all(text(x) for x in values)
                    and len(values) == len(set(values)), 'QUALIFICATION_EVALUATOR_METADATA')
        require(all(re.fullmatch(r'DFR-0(?:0[1-9]|1[0-2])', x) for x in evaluator['requirements']),
                'QUALIFICATION_REQUIREMENT_ID')
    require(type(rows) is list and 1 <= len(rows) <= 128, 'QUALIFICATION_CONTROLS')
    observed_ids, covered = set(), set()
    for row in rows:
        require(type(row) is dict and set(row) == {'id', 'evaluator_id', 'kind', 'description', 'expected'},
                'QUALIFICATION_CONTROL_FIELDS')
        require(type(row['id']) is str and row['id'] in controls.CONTROL_IDS and row['id'] not in observed_ids,
                'QUALIFICATION_CONTROL_ID')
        require(type(row['evaluator_id']) is str and row['evaluator_id'] in ids, 'QUALIFICATION_CONTROL_EVALUATOR')
        require(type(row['kind']) is str and row['kind'] in KINDS and text(row['description']),
                'QUALIFICATION_CONTROL_METADATA')
        expected = row['expected']
        json_value(expected)
        require(type(expected) is dict and type(expected.get('decision')) is str
                and expected['decision'] in DECISIONS, 'QUALIFICATION_EXPECTED_DECISION')
        observed_ids.add(row['id'])
        covered.add(row['evaluator_id'])
    require(observed_ids == set(controls.CONTROL_IDS) and covered == ids, 'QUALIFICATION_CATALOG_INCOMPLETE')
    return catalog


def load_catalog(root=ROOT):
    path = checked_path(Path(root) / CATALOG, root)
    require(path.is_file() and path.stat().st_size <= 1024 * 1024, 'QUALIFICATION_CATALOG_FILE')
    return validate_catalog(strict_json(path.read_text('utf-8-sig')))


def rate(numerator, denominator):
    return {'numerator': numerator, 'denominator': denominator,
            'value': numerator / denominator if denominator else None}


def metrics(rows):
    counts = Counter(row['status'] for row in rows)
    positive = [row for row in rows if row['expected']['decision'] == 'PASS']
    nonpositive = [row for row in rows if row['expected']['decision'] != 'PASS']
    false_pass = sum(row.get('observed', {}).get('decision') == 'PASS' for row in nonpositive)
    false_alarm = sum(row.get('observed', {}).get('decision') in {'FAIL', 'INCOMPLETE', 'REJECTED'} for row in positive)
    return {'planned': len(rows), 'matched': counts['PASS'], 'mismatched': counts['FAIL'],
            'missing': counts['INCOMPLETE'], 'errors': counts['ERROR'],
            'matched_rate': rate(counts['PASS'], len(rows)),
            'decision_false_approval_rate': rate(false_pass, len(nonpositive)),
            'decision_false_alarm_rate': rate(false_alarm, len(positive))}


def assess(catalog, observations):
    """Keep the planned denominator; unexpected/duplicate records invalidate it."""
    validate_catalog(catalog)
    planned = {row['id']: row for row in catalog['controls']}
    require(type(observations) is list and len(observations) <= 512, 'QUALIFICATION_OBSERVATIONS')
    by_id, issues = {}, []
    for observation in observations:
        if type(observation) is not dict or type(observation.get('id')) is not str:
            issues.append('MALFORMED_OBSERVATION_RECORD')
            continue
        identity = observation['id']
        if identity not in planned:
            issues.append('UNREGISTERED_OBSERVATION')
            continue
        by_id.setdefault(identity, []).append(observation)
    matrix = []
    for identity, definition in planned.items():
        row = {'id': identity, 'evaluator_id': definition['evaluator_id'], 'kind': definition['kind'],
               'expected': deepcopy(definition['expected']), 'status': 'INCOMPLETE', 'issues': []}
        values = by_id.get(identity, [])
        if not values:
            row['issues'] = ['MISSING_OBSERVATION']
        elif len(values) != 1:
            row.update(status='ERROR', issues=['DUPLICATE_OBSERVATION'])
        else:
            item = values[0]
            if set(item) == {'id', 'error_code'} and item['error_code'] == 'CONTROL_EXECUTION_ERROR':
                row.update(status='ERROR', issues=['CONTROL_EXECUTION_ERROR'])
            elif set(item) != {'id', 'observation'}:
                row.update(status='ERROR', issues=['MALFORMED_OBSERVATION_RECORD'])
            else:
                try:
                    actual = item['observation']
                    json_value(actual)
                    require(type(actual) is dict and type(actual.get('decision')) is str
                            and actual['decision'] in DECISIONS | {'ERROR'}, 'QUALIFICATION_OBSERVED_DECISION')
                    row['observed'] = deepcopy(actual)
                    row['status'] = 'PASS' if exact(definition['expected'], actual) else 'FAIL'
                    if row['status'] == 'FAIL':
                        row['issues'] = ['EXPECTED_OBSERVATION_MISMATCH']
                except (ValueError, OverflowError):
                    row.update(status='ERROR', issues=['INVALID_OBSERVATION_PROJECTION'])
        matrix.append(row)
    status = combine([row['status'] for row in matrix] + (['ERROR'] if issues else []))
    groups = []
    for evaluator in catalog['evaluators']:
        subset = [row for row in matrix if row['evaluator_id'] == evaluator['id']]
        groups.append({'id': evaluator['id'], 'status': combine([row['status'] for row in subset]),
                       'metrics': metrics(subset)})
    return {'status': status, 'metrics': metrics(matrix), 'matrix': matrix, 'evaluators': groups,
            'integrity_issues': sorted(set(issues)), 'received_records': len(observations)}


def numeric_path(value, prefix=()):
    if type(value) in (int, float):
        return prefix
    if type(value) is dict:
        for key in sorted(value):
            found = numeric_path(value[key], prefix + (key,))
            if found is not None:
                return found
    if type(value) is list:
        for index, child in enumerate(value):
            found = numeric_path(child, prefix + (index,))
            if found is not None:
                return found
    return None


def fault_injections(catalog, observations, baseline):
    """Demonstrate sensitivity to corrupt projections, not source mutations."""
    rows = []
    if baseline['status'] != 'PASS':
        return {'status': 'INCOMPLETE', 'planned': len(INJECTIONS), 'detected': 0,
                'rows': [{'id': name, 'status': 'NOT_RUN', 'reason': 'BASELINE_NOT_PASS'} for name in INJECTIONS],
                'scope': 'Controlled observation fault injection; no production source mutation coverage'}
    for name in INJECTIONS:
        altered, target = deepcopy(observations), None
        if name in ('force_pass', 'force_fail'):
            decision = 'PASS' if name == 'force_pass' else 'FAIL'
            for item in altered:
                if item['observation']['decision'] != decision:
                    target = target or item['id']
                item['observation']['decision'] = decision
        elif name == 'drop_record':
            target = altered.pop(0)['id']
        elif name == 'duplicate_record':
            target = altered[0]['id']
            altered.append(deepcopy(altered[0]))
        elif name == 'foreign_record':
            target = 'FOREIGN-CONTROL'
            altered.append({'id': target, 'observation': {'decision': 'PASS'}})
        elif name == 'alter_numeric':
            for item in altered:
                path = numeric_path(item['observation'])
                if path is not None:
                    target = item['id']
                    current = item['observation']
                    for key in path[:-1]:
                        current = current[key]
                    current[path[-1]] += 1
                    break
        if target is None:
            rows.append({'id': name, 'status': 'INCOMPLETE', 'reason': 'NO_APPLICABLE_CONTROL'})
            continue
        result = assess(catalog, altered)
        detected = (result['status'] != 'PASS' and
                    (('UNREGISTERED_OBSERVATION' in result['integrity_issues']) if name == 'foreign_record'
                     else any(row['id'] == target and row['status'] != 'PASS' for row in result['matrix'])))
        rows.append({'id': name, 'status': 'PASS' if detected else 'FAIL', 'target_control': target,
                     'observed_status': result['status'], 'observations': altered, 'assessment': result})
    return {'status': combine([row['status'] for row in rows]), 'planned': len(INJECTIONS),
            'detected': sum(row['status'] == 'PASS' for row in rows), 'rows': rows,
            'scope': 'Controlled observation fault injection; no production source mutation coverage'}


def output_path(value, root=ROOT):
    root = Path(root).absolute()
    supplied = Path(value)
    path = checked_path(supplied if supplied.is_absolute() else root / supplied, root)
    parts = path.relative_to(root).parts
    require(len(parts) == 4 and parts[:3] == ('var', 'qa', 'evaluator-qualification')
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', parts[-1]), 'QUALIFICATION_OUTPUT_SCOPE')
    require(not path.exists(), 'QUALIFICATION_OUTPUT_EXISTS')
    return path


def write_new(directory, name, value, root):
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    require(len(encoded.encode('utf-8')) <= 8 * 1024 * 1024, 'QUALIFICATION_OUTPUT_SIZE')
    with checked_path(directory / name, root).open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(encoded)


def render(result):
    rows = ['# Квалификация программных оценщиков', '',
            f"Итог: **{result['status']}**. Программные контроли: **{result['qualification_status']}**. "
            f"Намеренные подмены: **{result['fault_injection_status']}**.", '',
            'Продуктовая приёмка: INCOMPLETE. Настоящий Judge и человеческая калибровка: NOT_MEASURED.', '',
            '| Оценщик | Статус | Совпало / запланировано | Пропущено | Ошибки |',
            '| --- | --- | ---: | ---: | ---: |']
    for group in result.get('baseline', {}).get('evaluators', []):
        metric = group['metrics']
        rows.append(f"| {group['id']} | {group['status']} | {metric['matched']} / {metric['planned']} | "
                    f"{metric['missing']} | {metric['errors']} |")
    rows += ['', '## Ожидаемое и наблюдавшееся', '',
             'Полные проекции и коды находятся в `baseline.json`; знаменатель включает все запланированные контроли.', '',
             '| Контроль | Вид | Ожидаемое решение | Наблюдавшееся решение | Совпадение |',
             '| --- | --- | --- | --- | --- |']
    for row in result.get('baseline', {}).get('matrix', []):
        rows.append(f"| {row['id']} | {row['kind']} | {row['expected']['decision']} | "
                    f"{row.get('observed', {}).get('decision', 'NOT_OBSERVED')} | {row['status']} |")
    rows += ['', '## Проверка чувствительности', '',
             'Подменяются копии наблюдений, а не код оценщика. Это не процент покрытия мутаций исходников.', '',
             '| Подмена | Обнаружение | Контроль | Наблюдавшийся результат |',
             '| --- | --- | --- | --- |']
    for row in result.get('fault_injections', {}).get('rows', []):
        rows.append(f"| {row['id']} | {row['status']} | {row.get('target_control', 'NOT_RUN')} | "
                    f"{row.get('observed_status', 'NOT_RUN')} |")
    rows += ['', f"Привязка к версии: **{result['version_binding']}**.", '',
             'Синтетические открытые контроли не доказывают полноту реальных дефектов, независимость ожиданий '
             'от автора или пригодность модели. Исторический FAIL Judge не переоценивается. '
             'Хэши — локальная связь файлов, не независимая подпись или аттестация ОС.', '']
    if result.get('error_code'):
        rows += [f"Ошибка запуска: `{result['error_code']}`.", '']
    return '\n'.join(rows)


def run(output, *, root=ROOT):
    root = Path(root).absolute()
    directory = output_path(output, root)
    directory.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    write_new(directory, 'started.json', {'status': 'INCOMPLETE', 'run_id': directory.name,
                                        'started_at_utc': started}, root)
    result = {'schema_version': '1.0', 'run_id': directory.name, 'status': 'ERROR',
              'qualification_status': 'INCOMPLETE', 'fault_injection_status': 'INCOMPLETE',
              'version_binding': 'INCOMPLETE', 'product_acceptance': 'INCOMPLETE',
              'candidate_admission': 'NOT_EVALUATED', 'model_judge': 'NOT_MEASURED',
              'human_calibration': 'NOT_MEASURED', 'historical_judge': 'NOT_REASSESSED',
              'remote_ci': 'NOT_RUN', 'scope': 'Fixed open synthetic controls of programmatic evaluators only'}
    before = env_before = None
    pins = []
    try:
        before, env_before = snapshot(root), environment()
        result.update(source_digest=before['digest'], environment_digest=env_before['digest'])
        catalog = load_catalog(root)
        result['catalog_digest'] = canonical_digest(catalog)
        for name, value in [('source-before.json', before), ('environment-before.json', env_before), ('catalog.json', catalog)]:
            write_new(directory, name, value, root)
            pins.append(pin_file(directory / name, root))
        require(compare_snapshots(before, snapshot(root))['status'] == 'PASS', 'QUALIFICATION_SOURCE_CHANGED')
        observations = []
        for row in catalog['controls']:
            try:
                observed = controls.observe(row['id'])
                json_value(observed)
                observations.append({'id': row['id'], 'observation': observed})
            except Exception:
                observations.append({'id': row['id'], 'error_code': 'CONTROL_EXECUTION_ERROR'})
        baseline = assess(catalog, observations)
        injected = fault_injections(catalog, observations, baseline)
        result.update(qualification_status=baseline['status'], fault_injection_status=injected['status'],
                      baseline=baseline,
                      fault_injections={key: value for key, value in injected.items() if key != 'rows'})
        result['fault_injections']['rows'] = [{key: value for key, value in row.items()
                                               if key not in {'observations', 'assessment'}} for row in injected['rows']]
        for name, value in [('observations.json', observations), ('baseline.json', baseline), ('fault-injections.json', injected)]:
            write_new(directory, name, value, root)
            pins.append(pin_file(directory / name, root))
    except KeyboardInterrupt:
        result['error_code'] = 'INTERRUPTED'
    except Exception as exc:
        result['error_code'] = str(exc) if type(exc) is QualificationError else 'QUALIFICATION_EXECUTION_ERROR'
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
            result.setdefault('error_code', 'QUALIFICATION_FINAL_IDENTITY_ERROR')
        integrity = verify_files(pins, root)
        result.update(source_stability=stability, environment_stability=env_stability, evidence_integrity=integrity,
                      version_binding=combine([stability['status'], env_stability, integrity['status']]))
        result['status'] = combine([result['qualification_status'], result['fault_injection_status'], result['version_binding']])
        if result.get('error_code'):
            result['status'] = 'ERROR'
        result.update(started_at_utc=started, finished_at_utc=datetime.now(timezone.utc).isoformat())
        write_new(directory, 'evidence.json', {'files': pins, 'integrity': integrity}, root)
        write_new(directory, 'result.json', result, root)
        with checked_path(directory / 'report.md', root).open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(render(result))
    print(json.dumps({key: result[key] for key in ('run_id', 'status', 'qualification_status', 'fault_injection_status', 'version_binding')}))
    return EXIT[result['status']]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    try:
        return run(args.output_dir)
    except Exception as exc:
        print(json.dumps({'status': 'ERROR', 'error_code': str(exc) if type(exc) is QualificationError
                          else 'QUALIFICATION_OUTPUT_ERROR', 'product_acceptance': 'INCOMPLETE'}))
        return 2
