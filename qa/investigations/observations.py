"""Fixed learning controls built from real evaluators and explicitly modeled I/O.

Only the new training copy is deliberately changed. The projections contain
selected measurements, never generated report text or arbitrary file contents.
"""
from copy import deepcopy
import json
from pathlib import Path
import re

from qa.execution.runner import pin_file, verify_files
from src.qa_profile import checked_path


def _same(left, right):
    """JSON equality preserves bool/int distinctions and rejects non-finite values."""
    options = dict(sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    return json.dumps(left, **options) == json.dumps(right, **options)


def generate(root, directory, workspace):
    """Run bounded synthetic controls in new, caller-owned directories.

    ``root`` supplies frozen inputs. ``directory`` must be a pack under
    var/qa/investigations and ``workspace`` its corresponding isolated SQLite
    workspace. The caller records source/environment identities around this call.
    """
    root = Path(root).absolute()
    directory = checked_path(Path(directory).absolute(), root)
    workspace = checked_path(Path(workspace).absolute(), root)
    if (directory.parent != root / 'var/qa/investigations'
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', directory.name)
            or not directory.is_dir()
            or workspace != root / 'var/workflow/investigations' / directory.name / 'http-ack-replay'
            or not workspace.is_dir() or any(workspace.iterdir())):
        raise ValueError('INVESTIGATION_WORKSPACE_SCOPE')
    fixtures = checked_path(directory / 'fixtures', root)
    if fixtures.exists():
        raise ValueError('INVESTIGATION_FIXTURE_EXISTS')
    # Local imports keep the read-only UI free of model/control dependencies.
    from qa.autonomy.demo import run_demo as autonomy_demo
    from qa.stability.demo import run_demo as stability_demo
    from qa.environments import models
    from qa.environments.runner import load_catalog, assess_trace

    autonomy = autonomy_demo(root=root, emit=lambda event: None)
    stability = stability_demo()
    definition = next(row for row in load_catalog(root)['scenarios'] if row['id'] == 'http-ack-replay')
    observation = models.execute(definition, workspace)
    environment = {'scenario_id': definition['id'], 'definition': deepcopy(definition),
                   'observation': observation, 'assessment': assess_trace(definition, observation),
                   'expected_steps_match': _same(definition['expected'], observation['steps'])}
    fixtures.mkdir()
    path = checked_path(fixtures / 'checksum-control.json', root)
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write('{"synthetic_training_copy":true,"value":1}\n')
    before = pin_file(path, root)
    before_verification = verify_files([before], root)
    # The only intentional overwrite is this just-created training file.
    checked_path(path, root).write_text('{"synthetic_training_copy":true,"value":2}\n', encoding='utf-8', newline='\n')
    integrity = {'before_pin': before, 'after_pin': pin_file(path, root),
                 'before_verification': before_verification,
                 'after_verification': verify_files([before], root)}
    return {'autonomy': autonomy, 'stability': stability,
            'integrity': integrity, 'environment': environment}


def _fact(identity, label, value, source):
    name, _separator, pointer = source.partition('.json')
    reference = 'observations.json#/' + name + pointer.removeprefix('#')
    return {'id': identity, 'label': label, 'value': deepcopy(value), 'source': reference}


def _selected(value, *keys):
    return {key: value[key] for key in keys}


def _project(value):
    controls = value['autonomy']['controls']
    repair_index = next(index for index, row in enumerate(controls) if row['id'] == 'repair')
    unknown_index = next(index for index, row in enumerate(controls) if row['id'] == 'unknown_usage')
    repair = controls[repair_index]['cycle']
    unknown = controls[unknown_index]['cycle']
    repaired_case = repair['cases'][0]
    repair_source = f'autonomy.json#/controls/{repair_index}/cycle'
    unknown_source = f'autonomy.json#/controls/{unknown_index}/cycle'
    stability = value['stability']
    missing = stability['negative_statistics']['missing_repeat']
    stats = stability['statistics']
    arm = stats['arms']['candidate']
    integrity = value['integrity']
    env = value['environment']['observation']
    steps = env['steps']
    return [
        {'id': 'L01', 'facts': [
            _fact('F01', 'Первый ответ', repaired_case['first_attempt_status'], repair_source + '/cases/0/first_attempt_status'),
            _fact('F02', 'Итоговый кандидат и весь цикл',
                  {'final_candidate_status': repaired_case['final_candidate_status'], 'cycle_status': repair['status']}, repair_source),
            _fact('F03', 'Число вызовов учебного поставщика', repair['provider_calls'], repair_source + '/provider_calls'),
            _fact('F04', 'Новых вызовов LLM', value['autonomy']['new_model_calls'], 'autonomy.json#/new_model_calls')]},
        {'id': 'L02', 'facts': [
            _fact('F01', 'Полнота учёта расхода', _selected(unknown['ledger'], 'unknown_usage_attempts', 'total_tokens'), unknown_source + '/ledger'),
            _fact('F02', 'Остановка цикла', _selected(unknown, 'status', 'termination_reason'), unknown_source),
            _fact('F03', 'Известная часть расхода', unknown['ledger']['known_tokens'], unknown_source + '/ledger/known_tokens'),
            _fact('F04', 'Запланированные пределы', controls[unknown_index]['plan']['limits'], f'autonomy.json#/controls/{unknown_index}/plan/limits')]},
        {'id': 'L03', 'facts': [
            _fact('F01', 'План и наблюдения', _selected(missing, 'planned_slots', 'observed_slots'), 'stability.json#/negative_statistics/missing_repeat'),
            _fact('F02', 'Пропущенные записи и итог', _selected(missing, 'missing_slots', 'status'), 'stability.json#/negative_statistics/missing_repeat'),
            _fact('F03', 'Явно отмеченные невыполненные слоты', missing['not_run_slots'], 'stability.json#/negative_statistics/missing_repeat/not_run_slots'),
            _fact('F04', 'Область вывода о популяции', missing['population_inference'], 'stability.json#/negative_statistics/missing_repeat/population_inference')]},
        {'id': 'L04', 'facts': [
            _fact('F01', 'Размер варианта candidate',
                  {'cases': len(arm['cases']), 'families': arm['planned_family_count'], 'repeat_slots': arm['planned_slots']}, 'stability.json#/statistics/arms/candidate'),
            _fact('F02', 'Различия первого случая и область вывода',
                  {**_selected(arm['cases'][0]['variability'], 'distinct_semantic_reports', 'distinct_ordered_reports'),
                   'population_inference': stats['population_inference']}, 'stability.json#/statistics'),
            _fact('F03', 'Статус условного интервала', arm['interval_status'], 'stability.json#/statistics/arms/candidate/interval_status'),
            _fact('F04', 'Источник измерений', stats['evidence_kind'], 'stability.json#/statistics/evidence_kind')]},
        {'id': 'L05', 'facts': [
            _fact('F01', 'Исходный SHA-256 учебной копии', integrity['before_pin']['sha256'], 'integrity.json#/before_pin/sha256'),
            _fact('F02', 'SHA-256 после изменения и проверка исходной записи',
                  {'sha256': integrity['after_pin']['sha256'], 'status': integrity['after_verification']['status']}, 'integrity.json'),
            _fact('F03', 'Проверка до изменения', integrity['before_verification']['status'], 'integrity.json#/before_verification/status'),
            _fact('F04', 'Размер изменённой копии, байт', integrity['after_pin']['size'], 'integrity.json#/after_pin/size')]},
        {'id': 'L06', 'facts': [
            _fact('F01', 'После потери HTTP-подтверждения', _selected(steps[1], 'outcome', 'job_count', 'submit_calls'), 'environment.json#/observation/steps/1'),
            _fact('F02', 'После повторной отправки', _selected(steps[4], 'job_count', 'submit_calls', 'key_count'), 'environment.json#/observation/steps/4'),
            _fact('F03', 'Итоговая проекция учебного задания', _selected(steps[5], 'queue_status', 'job_status'), 'environment.json#/observation/steps/5'),
            _fact('F04', 'Моделируемые зависимости', env['dependencies']['modeled'], 'environment.json#/observation/dependencies/modeled')]},
    ]


def project(observations):
    """Select only the fixed lesson facts; reject malformed structures uniformly."""
    try:
        return _project(observations)
    except (KeyError, TypeError, IndexError, StopIteration, ValueError):
        raise ValueError('INVESTIGATION_OBSERVATIONS_INVALID') from None


def qualify(observations):
    """Check the declared controls, not human competence or product acceptance.

    Saved measurements can be rechecked without generation or filesystem writes.
    These local checks and hashes are not an independent signature of execution.
    """
    checks = []

    def check(identity, passed):
        checks.append({'id': identity, 'passed': passed is True})

    try:
        if type(observations) is not dict or set(observations) != {'autonomy', 'stability', 'integrity', 'environment'}:
            raise ValueError('SHAPE')
        facts = {row['id']: {fact['id']: fact['value'] for fact in row['facts']} for row in project(observations)}
        a, s, i, e = (observations[key] for key in ('autonomy', 'stability', 'integrity', 'environment'))
        from qa.autonomy.demo import EXPECTATIONS
        check('control_completion', a['status'] == s['qualification_status'] == 'PASS'
              and type(a['new_model_calls']) is int and a['new_model_calls'] == 0
              and a['product_acceptance'] == 'INCOMPLETE'
              and type(a['control_count']) is int and a['control_count'] == len(EXPECTATIONS)
              and [row['id'] for row in a['controls']] == list(EXPECTATIONS)
              and all(row['status'] == 'PASS' and _same(row['expected'], list(EXPECTATIONS[row['id']]))
                      and _same(row['expected'], row['observed']) for row in a['controls']))
        check('retained_failure', facts['L01']['F01'] == 'FAIL' and _same(facts['L01']['F02'],
              {'final_candidate_status': 'PASS', 'cycle_status': 'FAIL'}))
        check('unknown_usage', _same(facts['L02']['F01'], {'unknown_usage_attempts': 1, 'total_tokens': None})
              and _same(facts['L02']['F02'], {'status': 'INCOMPLETE', 'termination_reason': 'UNKNOWN_USAGE'}))
        check('missing_repeat', _same(facts['L03']['F01'], {'planned_slots': 12, 'observed_slots': 11})
              and _same(facts['L03']['F02'], {'missing_slots': 1, 'status': 'INCOMPLETE'}))
        from qa.stability.statistics import summarize
        check('statistics_recomputed', _same(s['statistics'], summarize(s['plan'], s['observations']))
              and _same(s['negative_statistics']['missing_repeat'],
                        summarize(s['plan'], s['negative_observations']['missing_repeat'])))
        check('dependent_measurements', _same(facts['L04']['F01'], {'cases': 3, 'families': 2, 'repeat_slots': 6})
              and _same(facts['L04']['F02'], {'distinct_semantic_reports': 1,
                  'distinct_ordered_reports': 2, 'population_inference': 'NOT_ESTABLISHED'}))
        # Context is displayed alongside the two decisive facts. Qualify it as
        # strictly as the verdict: a valid primary conclusion must not lend a
        # PASS label to malformed, unconstrained or contradictory context.
        context = {lesson: _selected(values, 'F03', 'F04') for lesson, values in facts.items()
                   if lesson in ('L01', 'L02', 'L03', 'L04')}
        check('context_facts', _same(context, {
            'L01': {'F03': 2, 'F04': 0},
            'L02': {'F03': 0, 'F04': {'max_attempts': 3, 'max_tokens': 30, 'wall_seconds': 5.0}},
            'L03': {'F03': 0, 'F04': 'NOT_ESTABLISHED'},
            'L04': {'F03': 'ASSUMPTION_DEPENDENT', 'F04': 'synthetic_control'},
        }))
        pins = (i['before_pin'], i['after_pin'])
        check('checksum_shape', all(type(pin) is dict and set(pin) == {'path', 'sha256', 'size'}
              and type(pin['path']) is str and re.fullmatch(
                  r'var/qa/investigations/[A-Za-z0-9][A-Za-z0-9_-]{0,63}/fixtures/checksum-control\.json', pin['path']) is not None
              and type(pin['sha256']) is str and re.fullmatch(r'[0-9a-f]{64}', pin['sha256']) is not None
              and type(pin['size']) is int and 0 < pin['size'] < 1024 for pin in pins))
        check('changed_checksum_detected', i['before_pin']['path'] == i['after_pin']['path']
              and i['before_pin']['sha256'] != i['after_pin']['sha256']
              and _same(i['before_verification'], {'status': 'PASS', 'changed_or_missing': []})
              and _same(i['after_verification'], {'status': 'ERROR', 'changed_or_missing': [i['before_pin']['path']]})
              and _same(facts['L05']['F04'], len(b'{"synthetic_training_copy":true,"value":2}\n')))
        check('ack_loss_and_replay', _same(facts['L06']['F01'],
              {'outcome': ['deferred:NOT_READY'], 'job_count': 1, 'submit_calls': 1})
              and _same(facts['L06']['F02'], {'job_count': 1, 'submit_calls': 2, 'key_count': 1})
              and _same(facts['L06']['F03'], {'queue_status': 'refused', 'job_status': 'refused'}))
        from qa.environments.runner import assess_trace
        from qa.environments.models import REAL_DEPENDENCIES, MODELED_DEPENDENCIES, REQUIRED_INVARIANTS
        assessment = assess_trace(e['definition'], e['observation'])
        invariants = [{'id': name, 'passed': True} for name in REQUIRED_INVARIANTS]
        check('environment_trace', e['scenario_id'] == e['definition']['id'] == 'http-ack-replay'
              and _same(e['definition']['steps'], [{'action': 'receive'}, {'action': 'tick', 'fault': 'http_ack_lost'},
                  {'action': 'finish'}, {'action': 'restart'}, {'action': 'tick'}, {'action': 'tick'}])
              and e['expected_steps_match'] is True
              and _same(e['definition']['expected'], e['observation']['steps'])
              and _same(assessment, {'consistent': True, 'invariants': invariants})
              and _same(assessment, e['assessment'])
              and _same(e['observation']['invariants'], invariants)
              and _same(e['observation']['dependencies'], {'real': list(REAL_DEPENDENCIES), 'modeled': list(MODELED_DEPENDENCIES)}))
    except (KeyError, TypeError, IndexError, StopIteration, ValueError, OverflowError):
        check('observations_readable', False)
    return {'status': 'PASS' if checks and all(row['passed'] for row in checks) else 'FAIL', 'checks': checks}
