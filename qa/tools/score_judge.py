"""Compare independently supplied human ratings with saved Judge attempts offline."""
from datetime import datetime, timezone
from hashlib import sha256
import argparse
import base64
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from qa.tools.run_judge import assess_record, evaluation_key, final_status, output_path
from qa.tools.run_retrieval import write
from qa.judge.dataset import DEFAULT_FOLDER, load_dataset
from qa.judge.scoring import summarize, validate_human_reviews
from src.runtime.transport import strict_json


def read(path):
    return strict_json(path.read_text(encoding='utf-8-sig'))


def saved_path(value):
    from src.qa_profile import checked_path
    raw = Path(value)
    path = checked_path(raw if raw.is_absolute() else ROOT / raw, ROOT)
    parts = path.relative_to(ROOT).parts
    if not ((len(parts) >= 3 and parts[0] == 'verification'
             and parts[1].startswith(('qa-step-08-', 'qa-judge-v2-')))
            or (len(parts) >= 4 and parts[:3] == ('var', 'qa', 'judge'))):
        raise ValueError('QA_JUDGE_SAVED_RUN_SCOPE')
    if not path.is_dir():
        raise ValueError('QA_JUDGE_SAVED_RUN_MISSING')
    return path


def _compact_configurations(run, result, dataset):
    """Bind new observations to the versioned request, not editable run labels."""
    from qa.judge.transport import MODEL, request_config
    from qa.tools.run_judge import content_hash
    from qa.tools.run_llm import EXPECTED_DIGEST
    configurations = {case['id']: request_config(case['payload'], 'compact-v2')
                      for case in dataset['cases']}
    first = next(iter(configurations.values()))
    selected = run.get('selected_case_ids')
    if (result.get('profile') != 'compact-v2'
            or type(result.get('source_files_unchanged')) is not bool
            or type(result.get('full_planned_matrix')) is not bool
            or (result.get('model_identity_stable') is not None
                and type(result.get('model_identity_stable')) is not bool)
            or run.get('model') != MODEL
            or run.get('model_digest') != EXPECTED_DIGEST
            or run.get('system_prompt') != first['system_prompt']
            or any(run.get(key) != value for key, value in first['options'].items())
            or run.get('seed') != 'not_explicitly_set'
            or run.get('automatic_retries') != 0 or run.get('cache') != 'disabled'
            or run.get('planned_case_ids') != list(configurations)
            or type(selected) is not list or not selected
            or any(type(value) is not str or value not in configurations for value in selected)
            or len(selected) != len(set(selected))
            or run.get('response_schema_sha256_by_case') != {
                key: content_hash(value['response_schema']) for key, value in configurations.items()}):
        raise ValueError('QA_JUDGE_SAVED_PROFILE_METADATA')
    return configurations


def _verify_compact_journal(run_dir, run, result, indexed, configurations, rows, input_hashes):
    """Keep interrupted starts visible and qualify claimed per-case identity."""
    from qa.tools.run_judge import content_hash, same_identity
    from src.qa_profile import checked_path

    def member(path):
        checked_path(path, ROOT)
        value = read(path)
        input_hashes[path.relative_to(ROOT).as_posix()] = sha256(path.read_bytes()).hexdigest()
        return value

    def timestamp(value):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError()
            return parsed
        except (ValueError, TypeError):
            raise ValueError('QA_JUDGE_SAVED_JOURNAL_TIMESTAMP') from None

    selected = set(run['selected_case_ids'])
    observed = {row['case_id']: row for row in rows}
    if not observed.keys() <= selected:
        raise ValueError('QA_JUDGE_SAVED_JOURNAL_INVENTORY')
    starts, completions, identities = {}, {}, {}
    for directory, suffixes in (('attempts', ('.started.json', '.completed.json')),
                                ('identities', ('.before.json',))):
        folder = run_dir / directory
        if not folder.is_dir():
            if run.get('prepare_only') is True and not rows:
                continue
            raise ValueError('QA_JUDGE_SAVED_JOURNAL_MISSING')
        checked_path(folder, ROOT)
        for path in folder.iterdir():
            suffix = next((suffix for suffix in suffixes if path.name.endswith(suffix)), None)
            case_id = path.name[:-len(suffix)] if suffix else None
            if suffix is None or case_id not in selected or not path.is_file():
                raise ValueError('QA_JUDGE_SAVED_JOURNAL_INVENTORY')
            value = member(path)
            destination = identities if directory == 'identities' else (
                starts if suffix == '.started.json' else completions)
            destination[case_id] = value
    if set(completions) != set(observed) or not observed.keys() <= starts.keys() <= identities.keys():
        raise ValueError('QA_JUDGE_SAVED_JOURNAL_INVENTORY')
    if result.get('full_planned_matrix') is True and (
            set(observed) != set(indexed) or set(starts) != set(observed)
            or set(identities) != set(observed)):
        raise ValueError('QA_JUDGE_SAVED_JOURNAL_INVENTORY')
    for case_id, entry in starts.items():
        key = evaluation_key(indexed[case_id]['payload'], read(run_dir / 'rubric.json'),
                             run['model_digest'], run['system_prompt'], profile='compact-v2')
        if (set(entry) != {'case_id', 'profile', 'evaluation_key', 'request_config_sha256',
                          'status', 'automatic_retries', 'recorded_at_utc'}
                or entry['case_id'] != case_id or entry['profile'] != 'compact-v2'
                or entry['status'] != 'REQUEST_MAY_BE_IN_FLIGHT'
                or entry['automatic_retries'] != 0 or entry['evaluation_key'] != key
                or entry['request_config_sha256'] != content_hash(configurations[case_id])):
            raise ValueError('QA_JUDGE_SAVED_JOURNAL_START')
        started = timestamp(entry['recorded_at_utc'])
        if case_id in completions:
            done = completions[case_id]
            case_path = run_dir / 'cases' / (case_id + '.json')
            if (set(done) != {'case_id', 'status', 'observation_sha256', 'recorded_at_utc'}
                    or done['case_id'] != case_id or done['status'] != observed[case_id]['status']
                    or done['observation_sha256'] != sha256(case_path.read_bytes()).hexdigest()
                    or timestamp(done['recorded_at_utc']) < started):
                raise ValueError('QA_JUDGE_SAVED_JOURNAL_COMPLETION')
    before_path, after_path = run_dir / 'ollama-before.json', run_dir / 'ollama-after.json'
    before = member(before_path) if before_path.is_file() else None
    after = member(after_path) if after_path.is_file() else None
    if identities and before is None:
        raise ValueError('QA_JUDGE_SAVED_MODEL_IDENTITY')
    for value in ([before] if before is not None else []) + list(identities.values()):
        if type(value) is not dict or value.get('model', {}).get('digest') != run['model_digest']:
            raise ValueError('QA_JUDGE_SAVED_MODEL_IDENTITY')
    if result.get('model_identity_stable') is True:
        try:
            stable = before is not None and after is not None and same_identity(before, after)
            stable = stable and all(same_identity(before, value) for value in identities.values())
        except (KeyError, TypeError):
            stable = False
        if not stable:
            raise ValueError('QA_JUDGE_SAVED_MODEL_IDENTITY')


def verify_saved(run_dir):
    """Recompute judgments from sealed raw attempts, not only claimed verdicts."""
    from qa.tools.run_judge import content_hash
    from src.qa_profile import checked_path
    from qa.judge.contracts import response_schema
    run = read(run_dir / 'run.json')
    result = read(run_dir / 'result.json')
    profile = run.get('profile', 'legacy-v1')
    if profile not in ('legacy-v1', 'compact-v2'):
        raise ValueError('QA_JUDGE_UNKNOWN_PROFILE')
    dataset = load_dataset()
    manifest_sha = sha256((DEFAULT_FOLDER / 'manifest.json').read_bytes()).hexdigest()
    if run['dataset_sha256'] != manifest_sha or read(run_dir / 'dataset.json') != dataset:
        raise ValueError('QA_JUDGE_SAVED_DATASET_MISMATCH')
    rubric = read(run_dir / 'rubric.json')
    if profile == 'compact-v2':
        from qa.tools.run_judge import RUBRIC
        if rubric != read(RUBRIC):
            raise ValueError('QA_JUDGE_SAVED_RUBRIC_MISMATCH')
    if content_hash(rubric) != run['rubric_sha256'] or content_hash(run['system_prompt']) != run['system_prompt_sha256']:
        raise ValueError('QA_JUDGE_SAVED_PROMPT_MISMATCH')
    if read(run_dir / 'source-hashes.json') != read(run_dir / 'source-hashes-after.json'):
        raise ValueError('QA_JUDGE_MEASURED_SOURCES_CHANGED')
    indexed = {case['id']: case for case in dataset['cases']}
    configurations = _compact_configurations(run, result, dataset) if profile == 'compact-v2' else None
    rows, input_hashes = [], {}
    paths = sorted((run_dir / 'cases').glob('*.json')) if (run_dir / 'cases').is_dir() else []
    seen = set()
    for path in paths:
        checked_path(path, ROOT)
        record = read(path)
        observed = record['observation']
        case_id = observed['case_id']
        if case_id not in indexed or case_id in seen or path.name != case_id + '.json':
            raise ValueError('QA_JUDGE_SAVED_CASE_ID')
        seen.add(case_id)
        case = indexed[case_id]
        if record['payload'] != case['payload']:
            raise ValueError('QA_JUDGE_SAVED_PAYLOAD_MISMATCH')
        raw = record['transport']
        if raw['status'] == 'completed' and not isinstance(raw.get('request_body'), str):
            raise ValueError('QA_JUDGE_SAVED_REQUEST_MISSING')
        body = base64.b64decode(raw['response_body_base64'], validate=True)
        if sha256(body).hexdigest() != raw['response_body_sha256'] or body.decode('utf-8', errors='replace') != raw['response_body']:
            raise ValueError('QA_JUDGE_SAVED_RESPONSE_HASH')
        if raw['request_body'] is not None:
            request = strict_json(raw['request_body'])
            expected_options = {key: run[key] for key in ('temperature', 'num_predict', 'num_ctx')}
            expected_schema = response_schema() if configurations is None else configurations[case_id]['response_schema']
            expected_user = {'rubric': rubric, 'task': case['payload']}
            if profile == 'compact-v2':
                expected_user['response_schema'] = expected_schema
            if (set(request) != {'model', 'stream', 'format', 'options', 'messages'}
                    or request['stream'] is not False or request['format'] != expected_schema
                    or len(request['messages']) != 2
                    or request['model'] != run['model'] or request['options'] != expected_options
                    or request['messages'][0] != {'role': 'system', 'content': run['system_prompt']}
                    or (profile == 'compact-v2' and request['messages'][1].get('role') != 'user')
                    or strict_json(request['messages'][1]['content']) != expected_user):
                raise ValueError('QA_JUDGE_SAVED_REQUEST_MISMATCH')
        if raw['status'] == 'completed':
            envelope = strict_json(body)
            if (raw['http_status'] != 200 or envelope.get('done') is not True or envelope.get('done_reason') != 'stop'
                    or envelope.get('model') != run['model'] or envelope.get('message', {}).get('role') != 'assistant'
                    or envelope['message'].get('tool_calls') or envelope['message']['content'] != raw['response_text']):
                raise ValueError('QA_JUDGE_SAVED_ENVELOPE')
        key = evaluation_key(case['payload'], rubric, run['model_digest'], run['system_prompt'], profile=profile)
        recalculated = assess_record(case, raw, key, profile=profile)
        if any(observed.get(key) != value for key, value in recalculated.items()):
            raise ValueError('QA_JUDGE_SAVED_ASSESSMENT_MISMATCH')
        rows.append(observed)
        input_hashes[path.relative_to(ROOT).as_posix()] = sha256(path.read_bytes()).hexdigest()
    if rows != sorted(result['observations'], key=lambda row: row['case_id']):
        raise ValueError('QA_JUDGE_SAVED_OBSERVATION_SET')
    if profile == 'compact-v2':
        _verify_compact_journal(run_dir, run, result, indexed, configurations, rows, input_hashes)
    if result['model_identity_stable'] is True:
        before_model, after_model = read(run_dir / 'ollama-before.json'), read(run_dir / 'ollama-after.json')
        if (before_model['model']['digest'] != run['model_digest']
                or after_model['model']['digest'] != run['model_digest']
                or before_model['server'] != after_model['server'] or before_model['show'] != after_model['show']):
            raise ValueError('QA_JUDGE_SAVED_MODEL_IDENTITY')
        for name in ('ollama-before.json', 'ollama-after.json'):
            path = run_dir / name
            input_hashes[path.relative_to(ROOT).as_posix()] = sha256(path.read_bytes()).hexdigest()
    for name in ('run.json', 'result.json', 'dataset.json', 'rubric.json', 'source-hashes.json', 'source-hashes-after.json'):
        path = run_dir / name
        input_hashes[path.relative_to(ROOT).as_posix()] = sha256(path.read_bytes()).hexdigest()
    return dataset, run, result, rows, manifest_sha, input_hashes


def run_comparison(run_dir, review_path, output):
    dataset, plan, original, rows, manifest_sha, input_hashes = verify_saved(run_dir)
    human = read(review_path)
    ratings = validate_human_reviews(human, dataset['cases'], dataset_sha256=manifest_sha)
    summary = summarize(dataset['cases'], rows, human_reviews=ratings)
    status = final_status(summary, source_stable=original['source_files_unchanged'],
                          model_stable=original['model_identity_stable'], full_matrix=original['full_planned_matrix'])
    output.mkdir(parents=True, exist_ok=False)
    write(output, 'human-reviews.json', human)
    write(output, 'result.json', dict(status=status, summary=summary, compared_at_utc=datetime.now(timezone.utc).isoformat(),
          profile=plan.get('profile', 'legacy-v1'),
          run=run_dir.relative_to(ROOT).as_posix(), input_hashes=input_hashes,
          human_reviews_sha256=sha256(review_path.read_bytes()).hexdigest(),
          comparison_code_sha256=sha256(Path(__file__).read_bytes()).hexdigest(), new_model_calls=0,
          rubric_version=plan.get('rubric_version', '1.0'), advisory_only=True, suitability='NOT_ESTABLISHED',
          product_acceptance='INCOMPLETE', sme_acceptance='pending'))
    print(json.dumps({'status': status, 'human_reviews': len(ratings), 'new_model_calls': 0}))
    return 0 if status == 'PASS' else 1 if status == 'FAIL' else 3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--human-reviews', required=True, type=Path)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    return run_comparison(saved_path(args.run_dir), args.human_reviews, output_path(args.output_dir))


if __name__ == '__main__':
    raise SystemExit(main())
