"""Advisory local Judge challenge runner. Generator reports are never regenerated."""
from datetime import datetime, timezone
from hashlib import sha256
import argparse
import json
from pathlib import Path
import platform
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from qa.tools.run_retrieval import environment, sources, write
from qa.tools.run_llm import EXPECTED_DIGEST, identity, network_guard, ollama_json

RUBRIC = ROOT / 'qa/judge/rubric-v1.json'
CASE_SECONDS = 180
BATCH_SECONDS = 1800
PROFILES = ('legacy-v1', 'compact-v2')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def content_hash(value):
    return sha256(canonical(value).encode('utf-8')).hexdigest()


def output_path(value):
    from src.qa_profile import checked_path
    raw = Path(value)
    path = checked_path(raw if raw.is_absolute() else ROOT / raw, ROOT)
    parts = path.relative_to(ROOT).parts
    if not ((len(parts) >= 3 and parts[0] == 'verification'
             and parts[1].startswith(('qa-step-08-', 'qa-judge-v2-')))
            or (len(parts) >= 4 and parts[:3] == ('var', 'qa', 'judge'))):
        raise ValueError('QA_JUDGE_NEW_OUTPUT_REQUIRED')
    if path.exists():
        raise FileExistsError('QA_EVIDENCE_ALREADY_EXISTS')
    return path


def selected_cases(cases, phase='all', case_id=None):
    if phase not in ('all', 'calibration', 'heldout'):
        raise ValueError('QA_JUDGE_UNKNOWN_PHASE')
    if case_id is not None and case_id not in {case['id'] for case in cases}:
        raise ValueError('QA_JUDGE_UNKNOWN_CASE')
    result = [case for case in cases if (phase == 'all' or case['split'] == phase)
              and (case_id is None or case['id'] == case_id)]
    if not result:
        raise ValueError('QA_JUDGE_EMPTY_SELECTION')
    return result


def source_hashes():
    result = sources()
    for path in (ROOT / 'qa/datasets/judge-v1').rglob('*'):
        if path.is_file() and '__pycache__' not in path.parts:
            result[path.relative_to(ROOT).as_posix()] = sha256(path.read_bytes()).hexdigest()
    result['qa/judge/rubric-v1.json'] = sha256(RUBRIC.read_bytes()).hexdigest()
    return dict(sorted(result.items()))


def evaluation_key(payload, rubric, model_digest, system_prompt, *, profile='legacy-v1'):
    if profile not in PROFILES:
        raise ValueError('QA_JUDGE_UNKNOWN_PROFILE')
    if profile != 'legacy-v1':
        from qa.judge.transport import request_config
        configuration = request_config(payload, profile=profile)
        if system_prompt != configuration['system_prompt']:
            raise ValueError('QA_JUDGE_PROFILE_PROMPT_MISMATCH')
        return content_hash({'payload': payload, 'rubric': rubric, 'model_digest': model_digest,
                             'request_config': configuration, 'cache_schema': '2.0'})
    return content_hash({'payload': payload, 'rubric': rubric, 'model_digest': model_digest,
                         'system_prompt': system_prompt, 'parameters': {'temperature': 0,
                         'num_predict': 512, 'num_ctx': 32768, 'seed': 'not_explicitly_set'}, 'cache_schema': '1.0'})


def human_packet(output, cases, rubric, dataset_hash):
    """Create blind material before inference. Do not include oracle or Judge labels."""
    lines = ['# Независимая оценка оператора', '',
        'Заполните human-review-template.json самостоятельно до просмотра результатов Judge и oracle.',
        'Это учебная оценка представления отчёта; она не означает принятия AML-решения.',
        'Внесите reviewer_id и reviewed_at (ISO 8601 с часовым поясом), затем оценки всех показанных кейсов.',
        'Для каждого: pass/fail/abstain, краткое основание и точная цитата с ref_id. '
        'В single preference=not_applicable. В pair: verdict=pass с preference=A/B/tie, '
        'либо verdict=abstain и preference=abstain.', '', '## Рубрика', '']
    for criterion, description in rubric['criteria'].items():
        lines.extend([f'**{criterion}**: {description}', ''])
    lines.extend(['Имена авторов, автоматические оценки и ожидаемые ответы скрыты. '
                  'Сравнивайте только представленные фрагменты.', ''])
    ratings = []
    for case in cases:
        payload = case['payload']
        lines.extend([f"## {case['id']} — {payload['criterion']} ({payload['mode']})", '',
                      '### Источники', ''])
        for item in payload['sources']:
            lines.extend([f"`{item['ref_id']}`", '', '```text', item['text'], '```', ''])
        for candidate in payload['candidates']:
            lines.extend([f"### Вариант {candidate['label']}", '', '```json',
                          json.dumps(candidate['report'], ensure_ascii=False, indent=2), '```', ''])
            for item in candidate['fragments']:
                lines.extend([f"`{item['ref_id']}`", '', '```text', item['text'], '```', ''])
        ratings.append(dict(case_id=case['id'], criterion=payload['criterion'], verdict='',
                            preference='not_applicable' if payload['mode'] == 'single' else '',
                            references=[], rationale=''))
    with (output / 'human-review-packet.md').open('x', encoding='utf-8') as stream:
        stream.write('\n'.join(lines) + '\n')
    write(output, 'human-review-template.json', dict(schema_version='1.0', rubric_version='1.0',
          dataset_sha256=dataset_hash, reviewer_id='', reviewed_at='', ratings=ratings))


def assess_record(case, transport_record, key, *, profile='legacy-v1'):
    if profile not in PROFILES:
        raise ValueError('QA_JUDGE_UNKNOWN_PROFILE')
    if profile == 'compact-v2':
        from qa.judge.protocol_v2 import validate_response
    else:
        from qa.judge.contracts import validate_response
    status = transport_record['status']
    response, code = None, transport_record.get('error_code')
    if status == 'completed':
        try:
            response = validate_response(transport_record['response_text'], case['payload'])
        except (ValueError, TypeError, KeyError) as exc:
            status, code = 'invalid_response', str(exc) if isinstance(exc, ValueError) else 'QA_JUDGE_INVALID_RESPONSE'
    return dict(case_id=case['id'], status=status, response=response, error_code=code,
                latency_seconds=transport_record.get('elapsed_seconds'), cache_key=key,
                programmatic_gate=case['programmatic_gate'], operator_decision_written=False)


def final_status(summary, *, source_stable, model_stable, full_matrix):
    if not source_stable or model_stable is False:
        return 'FAIL'
    if summary['known_challenge_status'] == 'FAIL' or summary['execution_status'] == 'FAIL':
        return 'FAIL'
    if (model_stable is None or not full_matrix or summary['execution_status'] != 'PASS'
            or summary['known_challenge_status'] != 'PASS'
            or summary['human_calibration']['status'] != 'EVIDENCE_COMPLETE'):
        return 'INCOMPLETE'
    # Passing a measured exercise never promotes Judge to authority or grants product acceptance.
    return 'PASS'


def same_identity(initial, current):
    """Loaded/warm state may change; measured model/server/config identity may not."""
    return (initial['model']['digest'] == current['model']['digest']
            and initial['server'] == current['server'] and initial['show'] == current['show'])


def run(output, *, phase='all', case_id=None, human_reviews=None, prepare_only=False,
        profile='legacy-v1'):
    from qa.judge.dataset import DEFAULT_FOLDER, load_dataset
    from qa.judge.contracts import validate_payload
    from qa.judge.scoring import summarize, validate_human_reviews
    from qa.judge.transport import JudgeTransport, MODEL, SYSTEM_PROMPT
    from src.runtime.transport import strict_json
    if profile not in PROFILES:
        raise ValueError('QA_JUDGE_UNKNOWN_PROFILE')
    dataset = load_dataset()
    rubric = strict_json(RUBRIC.read_text(encoding='utf-8-sig'))
    cases = dataset['cases']
    selected = selected_cases(cases, phase, case_id)
    for case in cases:
        validate_payload(case['payload'])
    configurations = None
    system_prompt = SYSTEM_PROMPT
    options = dict(temperature=0, num_predict=512, num_ctx=32768)
    if profile == 'compact-v2':
        from qa.judge.transport import request_config
        configurations = {case['id']: request_config(case['payload'], profile=profile) for case in cases}
        system_prompt = configurations[cases[0]['id']]['system_prompt']
        options = configurations[cases[0]['id']]['options']
    dataset_hash = sha256((DEFAULT_FOLDER / 'manifest.json').read_bytes()).hexdigest()
    reviews = None
    if human_reviews is not None:
        raw_reviews = strict_json(Path(human_reviews).read_text(encoding='utf-8-sig'))
        reviews = validate_human_reviews(raw_reviews, cases, dataset_sha256=dataset_hash)
    output.mkdir(parents=True, exist_ok=False)
    environment(output)
    before = source_hashes()
    write(output, 'source-hashes.json', before)
    write(output, 'dataset.json', dataset)
    write(output, 'rubric.json', rubric)
    human_packet(output, cases, rubric, dataset_hash)
    if human_reviews is not None:
        write(output, 'human-reviews.json', raw_reviews)
    write(output, 'run.json', dict(status='INCOMPLETE', started_at_utc=datetime.now(timezone.utc).isoformat(),
          selected_case_ids=[case['id'] for case in selected], planned_case_ids=[case['id'] for case in cases],
          phase=phase, prepare_only=prepare_only, dataset_sha256=dataset_hash, rubric_sha256=content_hash(rubric),
          profile=profile, model=MODEL, model_digest=EXPECTED_DIGEST, **options,
          seed=options.get('seed', 'not_explicitly_set'), system_prompt=system_prompt,
          system_prompt_sha256=content_hash(system_prompt),
          response_schema_sha256_by_case=None if configurations is None else {
              key: content_hash(value['response_schema']) for key, value in configurations.items()},
          cache='disabled', case_deadline_seconds=CASE_SECONDS, batch_deadline_seconds=BATCH_SECONDS,
          max_http_calls=len(selected), automatic_retries=0, purpose='known_defect_challenge_not_product_gate',
          same_model_as_generator=True, product_acceptance='INCOMPLETE', sme_acceptance='pending'))
    write(output, 'runtime.json', dict(python=sys.version, platform=platform.platform(),
          cpu=platform.processor(), embedding_loaded=False, generator_calls=0))
    observations = []
    result = dict(status='INCOMPLETE', profile=profile, advisory_only=True, suitability='NOT_ESTABLISHED',
                  product_acceptance='INCOMPLETE', sme_acceptance='pending')
    started = time.monotonic()
    model_stable = None
    try:
        if not prepare_only:
            (output / 'cases').mkdir()
            if profile == 'compact-v2':
                (output / 'attempts').mkdir()
                (output / 'identities').mkdir()
            with network_guard():
                initial = identity()
                write(output, 'ollama-before.json', initial)
                transport = JudgeTransport() if profile == 'legacy-v1' else JudgeTransport(profile=profile)
                try:
                    for case in selected:
                        remaining = BATCH_SECONDS - (time.monotonic() - started)
                        if remaining <= 0:
                            result['stop_reason'] = 'BATCH_DEADLINE_EXCEEDED'
                            break
                        if profile == 'compact-v2':
                            current = identity()
                            write(output / 'identities', case['id'] + '.before.json', current)
                            if not same_identity(initial, current):
                                model_stable = False
                                raise RuntimeError('QA_JUDGE_MODEL_IDENTITY_CHANGED')
                        try:
                            loaded = ollama_json('/api/ps')
                        except Exception:
                            loaded = None
                        load_state = 'unknown' if loaded is None else ('warm' if any(
                            row.get('digest') == EXPECTED_DIGEST for row in loaded.get('models', [])) else 'cold')
                        key = evaluation_key(case['payload'], rubric, EXPECTED_DIGEST, system_prompt, profile=profile)
                        if profile == 'compact-v2':
                            write(output / 'attempts', case['id'] + '.started.json', dict(
                                case_id=case['id'], profile=profile, evaluation_key=key,
                                request_config_sha256=content_hash(configurations[case['id']]),
                                status='REQUEST_MAY_BE_IN_FLIGHT', automatic_retries=0,
                                recorded_at_utc=datetime.now(timezone.utc).isoformat()))
                        raw = transport.complete(case['payload'], rubric,
                                deadline=min(started + BATCH_SECONDS, time.monotonic() + CASE_SECONDS))
                        observed = assess_record(case, raw, key, profile=profile)
                        observed.update(model_load_state=load_state, loaded_before=loaded)
                        observations.append(observed)
                        write(output / 'cases', case['id'] + '.json', dict(observation=observed,
                              transport=raw, payload=case['payload'], expected_labels_hidden_from_model=True))
                        if profile == 'compact-v2':
                            write(output / 'attempts', case['id'] + '.completed.json', dict(
                                case_id=case['id'], status=observed['status'],
                                observation_sha256=sha256((output / 'cases' / (case['id'] + '.json')).read_bytes()).hexdigest(),
                                recorded_at_utc=datetime.now(timezone.utc).isoformat()))
                        print(json.dumps(dict(case_id=case['id'], split=case['split'], status=observed['status'],
                              verdict=None if observed['response'] is None else observed['response']['verdict'],
                              seconds=observed['latency_seconds']), ensure_ascii=False), flush=True)
                finally:
                    final = identity()
                    write(output, 'ollama-after.json', final)
                    model_stable = model_stable is not False and same_identity(initial, final)
    except Exception as exc:
        result.update(error_type=type(exc).__name__, error_code=str(exc) if type(exc) in (ValueError, RuntimeError)
                      else 'QA_JUDGE_COLLECTION_INTERRUPTED')
    finally:
        after = source_hashes()
        write(output, 'source-hashes-after.json', after)
        summary = summarize(cases, observations, human_reviews=reviews)
        source_stable = before == after
        full = not prepare_only and len(selected) == len(cases) and len(observations) == len(cases)
        status = final_status(summary, source_stable=source_stable, model_stable=model_stable, full_matrix=full)
        result.update(status=status, summary=summary, observations=observations,
                      source_files_unchanged=source_stable, model_identity_stable=model_stable,
                      full_planned_matrix=full, selected_case_count=len(selected), planned_case_count=len(cases),
                      observed_case_count=len(observations), generator_calls=0,
                      elapsed_seconds=time.monotonic() - started, finished_at_utc=datetime.now(timezone.utc).isoformat())
        write(output, 'result.json', result)
    print(json.dumps({'status': result['status'], 'observed': len(observations),
                      'planned': len(cases), 'suitability': 'NOT_ESTABLISHED'}), flush=True)
    return 0 if result['status'] == 'PASS' else 1 if result['status'] == 'FAIL' else 3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--phase', choices=('all', 'calibration', 'heldout'), default='all')
    parser.add_argument('--case-id')
    parser.add_argument('--human-reviews', type=Path)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--profile', choices=PROFILES, default='legacy-v1')
    args = parser.parse_args()
    return run(output_path(args.output_dir), phase=args.phase, case_id=args.case_id,
               human_reviews=args.human_reviews, prepare_only=args.prepare_only, profile=args.profile)


if __name__ == '__main__':
    raise SystemExit(main())
