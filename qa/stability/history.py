"""Read-only re-evaluation of bounded saved run_llm evidence, never new inference.

Historical identities are self-reported local evidence, not signatures. The old
collector did not declare a total budget; enforcement cannot be inferred later.
"""
from collections import Counter
from dataclasses import fields
from hashlib import sha256
import base64
import math
from pathlib import Path
import re

from qa.execution.runner import pin_file, verify_files
from qa.model_eval.checks import check_repeat
from qa.reporting import combine, strict_json
from qa.scenario_checks import Observation, check_observation
from qa.scenarios import load_catalog
from src.qa_profile import ROOT, checked_path

DRAFT_IDS = ('QA-S01', 'QA-S02', 'QA-S03', 'QA-S04', 'QA-S14')
CONTROL_IDS = tuple(f'QA-S{number:02d}' for number in range(5, 13))
MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
_HEX = re.compile(r'[0-9a-f]{64}')
_TRANSPORT = {'completed': 'completed', 'timeout': 'timeout',
              'dependency_failure': 'dependency_failure', 'error': 'error',
              'DEADLINE_EXCEEDED': 'timeout', 'DEPENDENCY_UNAVAILABLE': 'dependency_failure',
              'NOT_READY': 'dependency_failure'}


class HistoryError(ValueError):
    """Fixed code without raw historical payloads or filesystem details."""


def _require(condition, code):
    if not condition:
        raise HistoryError(code)


def _exact(first, second):
    if type(first) is not type(second):
        return False
    if isinstance(first, dict):
        return set(first) == set(second) and all(_exact(value, second[key]) for key, value in first.items())
    if isinstance(first, list):
        return len(first) == len(second) and all(_exact(a, b) for a, b in zip(first, second))
    return first == second


def _number(value, *, nullable=False):
    return (value is None and nullable) or (type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e9)


def _path(value, root):
    root = Path(root).absolute()
    raw = Path(value)
    path = checked_path(raw if raw.is_absolute() else root / raw, root)
    parts = path.relative_to(root).parts
    allowed = (len(parts) == 4 and parts[:3] == ('var', 'qa', 'llm')) or (
        len(parts) == 3 and parts[0] == 'verification'
        and re.fullmatch(r'qa-step-07-\d{4}-\d{2}-\d{2}', parts[1]))
    _require(allowed and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', parts[-1])
             and path.is_dir(), 'HISTORY_SOURCE_SCOPE')
    return path


class _Reader:
    def __init__(self, source, root):
        self.source, self.root, self.files, self.total, self.missing = source, root, [], 0, []

    def read(self, name, *, required=False):
        path = checked_path(self.source / name, self.root)
        if not path.exists():
            _require(not required, 'HISTORY_REQUIRED_FILE_MISSING')
            self.missing.append(name)
            return None
        _require(path.is_file() and path.stat().st_size <= MAX_FILE, 'HISTORY_FILE_SIZE')
        before = pin_file(path, self.root)
        raw = path.read_bytes()
        self.total += len(raw)
        _require(len(raw) <= MAX_FILE and self.total <= MAX_TOTAL, 'HISTORY_FILE_SIZE')
        _require(sha256(raw).hexdigest() == before['sha256'] and len(raw) == before['size'], 'HISTORY_FILE_CHANGED')
        value = strict_json(raw.decode('utf-8-sig'))
        self.files.append(before)
        return value


def _observation(value):
    _require(type(value) is dict and set(value) == {field.name for field in fields(Observation)}, 'HISTORY_OBSERVATION_FIELDS')
    _require(type(value['result']) is dict and (value['context'] is None or type(value['context']) is dict), 'HISTORY_OBSERVATION')
    _require(type(value['selected_rule_ids']) is list and all(type(x) is str for x in value['selected_rule_ids']), 'HISTORY_OBSERVATION')
    _require(all(type(value[key]) is list and all(type(x) is dict for x in value[key])
                 for key in ('model_prompts', 'events')) and _number(value['latency_seconds']), 'HISTORY_OBSERVATION')
    return Observation(**value)


def _attempt(row, prompt, model, transport):
    _require(type(row) is dict and row.get('endpoint') == 'http://127.0.0.1:11434'
             and row.get('method') == 'POST' and row.get('path') == '/api/chat', 'HISTORY_ATTEMPT_ENDPOINT')
    request = strict_json(row['request_body'])
    _require(type(request) is dict and request.get('model') == model and request.get('stream') is False,
             'HISTORY_ATTEMPT_REQUEST')
    messages = request.get('messages')
    _require(type(messages) is list and len(messages) == 2 and messages[1].get('role') == 'user'
             and _exact(strict_json(messages[1]['content']), prompt), 'HISTORY_ATTEMPT_PROMPT')
    raw = base64.b64decode(row['response_body_base64'], validate=True)
    _require(len(raw) <= MAX_FILE and row.get('response_body_sha256') == sha256(raw).hexdigest()
             and type(row.get('response_body_bytes')) is int and row['response_body_bytes'] == len(raw),
             'HISTORY_ATTEMPT_RESPONSE_HASH')
    try:
        decoded = raw.decode('utf-8')
        reply = strict_json(decoded)
    except (UnicodeError, ValueError):
        _require('response_json' not in row, 'HISTORY_ATTEMPT_RESPONSE_JSON')
        reply = {}
    else:
        _require(row.get('response_body') == decoded and _exact(row.get('response_json'), reply),
                 'HISTORY_ATTEMPT_RESPONSE_JSON')
    _require(type(reply) is dict, 'HISTORY_ATTEMPT_RESPONSE_JSON')
    completed = row.get('http_status') == 200 and reply.get('done') is True
    if completed:
        _require(reply.get('model') == model, 'HISTORY_ATTEMPT_MODEL')
    tokens = [reply.get('prompt_eval_count'), reply.get('eval_count')]
    _require(all(value is None or type(value) is int and 0 <= value <= 10000000 for value in tokens), 'HISTORY_TOKEN_COUNT')
    elapsed = row.get('elapsed_seconds')
    _require(_number(elapsed, nullable=True), 'HISTORY_ATTEMPT_TIME')
    return {'status': 'timeout' if transport == 'timeout' else
            'completed' if completed and transport == 'completed' else 'error',
            'elapsed_seconds': elapsed, 'prompt_tokens': tokens[0], 'output_tokens': tokens[1]}


def _record(record, scenario, repeat, model, *, control):
    _require(type(record) is dict and set(record) == {'evaluation', 'observation', 'http_attempts', 'rankings',
        'loaded_before', 'model_load_state', 'transport_outcome', 'template_issues', 'template_report'}, 'HISTORY_RECORD_FIELDS')
    _require(record['transport_outcome'] in _TRANSPORT
             and record['model_load_state'] in ('cold', 'warm'), 'HISTORY_RECORD_STATE')
    transport = _TRANSPORT[record['transport_outcome']]
    observed = _observation(record['observation'])
    _require(transport == 'completed' or (observed.result.get('status') == 'error'
             and observed.model_prompts), 'HISTORY_RECORD_STATE')
    if record['transport_outcome'] in ('DEADLINE_EXCEEDED', 'DEPENDENCY_UNAVAILABLE', 'NOT_READY'):
        _require(observed.result.get('code') == record['transport_outcome'], 'HISTORY_RECORD_STATE')
    attempts = record['http_attempts']
    # The collector saves the prompt before entering LocalOllamaTransport.
    # A deadline/configuration failure may occur before HTTPConnection exists.
    allowed_counts = {len(observed.model_prompts)}
    if transport != 'completed':
        allowed_counts.add(len(observed.model_prompts) - 1)
    _require(type(attempts) is list and len(attempts) <= 2
             and len(attempts) in allowed_counts, 'HISTORY_ATTEMPT_COUNT')
    if control:
        issues = check_observation(scenario, observed)
        checked = {'case_id': scenario.case.id, 'repeat_index': 1, 'status': 'FAIL' if issues else 'PASS',
                   'issues': issues, 'model_calls': len(observed.model_prompts)}
    else:
        runtime_scenario = scenario.model_copy(update={'engine': 'local-model', 'expected_model_calls': 1})
        # run_llm persisted an Observation even when its transport timed out;
        # the runtime result carries the error code. Do not erase that result.
        checked = check_repeat(runtime_scenario, observed, repeat_index=repeat, transport_outcome='completed')
    _require(_exact(checked, record['evaluation']), 'HISTORY_EVALUATION_MISMATCH')
    normalized = [_attempt(row, prompt, model,
                          transport if index == len(observed.model_prompts) - 1 else 'completed')
                  for index, (row, prompt) in enumerate(zip(attempts, observed.model_prompts))]
    if len(attempts) < len(observed.model_prompts):
        # A recorded prompt proves a model-call attempt, but not a completed
        # HTTP exchange or measured usage. Keep unknowns in aggregate usage.
        normalized.append({'status': 'timeout' if transport == 'timeout' else 'error',
                           'elapsed_seconds': None, 'prompt_tokens': None, 'output_tokens': None})
    if control:
        return {'case_id': scenario.case.id, 'status': checked['status'], 'attempts': len(normalized)}
    draft = checked['draft']
    return {'arm': 'candidate', 'case_id': scenario.case.id, 'repeat_index': repeat,
            'status': checked['status'], 'outcome': checked['outcome'],
            'semantic_fingerprint': draft['semantic_fingerprint'] if draft else None,
            'ordered_fingerprint': draft['ordered_fingerprint'] if draft else None,
            'latency_seconds': checked['latency_seconds'], 'attempts': normalized}


def _case_files(source, selected, repeats, root):
    cases = checked_path(source / 'cases', root)
    if not cases.exists():
        return
    _require(cases.is_dir(), 'HISTORY_CASES_DIRECTORY')
    entries = list(cases.iterdir())
    _require(len(entries) <= len(DRAFT_IDS) + len(CONTROL_IDS), 'HISTORY_UNPLANNED_CASE')
    for directory in entries:
        checked_path(directory, root)
        _require(directory.is_dir() and directory.name in selected, 'HISTORY_UNPLANNED_CASE')
        count = repeats if directory.name in DRAFT_IDS else 1
        allowed = {'template.json'} | {f'repeat-{index:02d}.json' for index in range(1, count + 1)}
        children = list(directory.iterdir())
        _require(len(children) <= len(allowed), 'HISTORY_UNPLANNED_REPEAT')
        for child in children:
            checked_path(child, root)
            _require(child.is_file() and child.name in allowed, 'HISTORY_UNPLANNED_REPEAT')


def _source_stability(before, after):
    for value in (before, after):
        if value is not None:
            _require(type(value) is dict and value and len(value) <= 10000
                     and all(type(k) is str and type(v) is str and _HEX.fullmatch(v) for k, v in value.items()),
                     'HISTORY_SOURCE_HASHES')
    return 'INCOMPLETE' if before is None or after is None else 'PASS' if _exact(before, after) else 'FAIL'


def load_history(source_dir: Path, root=ROOT):
    """Load a fixed historical panel, rechecking saved outcomes against frozen facts."""
    try:
        return _load(source_dir, Path(root).absolute())
    except HistoryError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise HistoryError('HISTORY_INVALID_EVIDENCE') from None


def _load(source_dir, root):
    source = _path(source_dir, root)
    reader = _Reader(source, root)
    run = reader.read('run.json', required=True)
    _require(type(run) is dict and type(run.get('repeats')) is int and 1 <= run['repeats'] <= 5, 'HISTORY_RUN_PLAN')
    selected = run.get('selected_case_ids')
    _require(type(selected) is list and 1 <= len(selected) <= 13 and all(type(x) is str for x in selected)
             and len(set(selected)) == len(selected) and set(selected) <= set(DRAFT_IDS + CONTROL_IDS), 'HISTORY_RUN_PLAN')
    drafts = [name for name in selected if name in DRAFT_IDS]
    _require(drafts and type(run.get('model')) is str and 0 < len(run['model']) <= 100
             and type(run.get('model_digest')) is str and _HEX.fullmatch(run['model_digest']), 'HISTORY_RUN_PLAN')
    _case_files(source, selected, run['repeats'], root)
    catalog = load_catalog(root / 'qa/datasets/aml-v1')
    scenarios = {scenario.case.id: scenario for scenario in catalog.cases}
    _require(set(selected) <= set(scenarios), 'HISTORY_CATALOG_CASE')
    plan = {'schema_version': '1.0', 'plan_id': 'historical-llm-panel', 'evidence_kind': 'historical_llm',
            'arms': ['candidate'], 'cases': [{'case_id': name, 'family_id': scenarios[name].case.independence_id,
                'repeats': run['repeats']} for name in drafts]}
    before, after = [reader.read(name) for name in ('source-hashes.json', 'source-hashes-after.json')]
    stability = _source_stability(before, after)
    manifest = pin_file(root / 'qa/datasets/aml-v1/manifest.json', root)
    reader.files.append(manifest)
    _require(before is None or before.get('qa/datasets/aml-v1/manifest.json') == manifest['sha256'], 'HISTORY_FROZEN_DATASET_MISMATCH')
    identities = [reader.read(name) for name in ('ollama-before.json', 'ollama-after.json')]
    identity_status = 'INCOMPLETE' if any(value is None for value in identities) else 'PASS'
    for value in identities:
        if value is not None:
            _require(type(value) is dict and type(value.get('model')) is dict and type(value.get('server')) is dict
                     and type(value.get('show')) is dict, 'HISTORY_MODEL_IDENTITY')
            _require(value['model'].get('digest') == run['model_digest'] and value['model'].get('name') == run['model'],
                     'HISTORY_MODEL_IDENTITY')
    if identity_status == 'PASS' and any(not _exact(identities[0][key], identities[1][key]) for key in ('server', 'show')):
        identity_status = 'FAIL'
    result, runtime = reader.read('result.json'), reader.read('runtime.json')
    _require(result is None or type(result) is dict and result.get('status') in ('PASS', 'FAIL', 'ERROR', 'INCOMPLETE'), 'HISTORY_RESULT')
    _require(runtime is None or type(runtime) is dict, 'HISTORY_RUNTIME')
    header_status = 'INCOMPLETE' if reader.missing else 'PASS'
    rows, controls, load_states, missing_http = [], [], Counter(), 0
    for name in selected:
        count = run['repeats'] if name in DRAFT_IDS else 1
        for repeat in range(1, count + 1):
            record = reader.read(f'cases/{name}/repeat-{repeat:02d}.json')
            if record is None:
                continue
            row = _record(record, scenarios[name], repeat, run['model'], control=name in CONTROL_IDS)
            missing_http += len(record['observation']['model_prompts']) - len(record['http_attempts'])
            (controls if name in CONTROL_IDS else rows).append(row)
            if name in DRAFT_IDS:
                load_states[record['model_load_state']] += 1
    planned_controls = sum(name in CONTROL_IDS for name in selected)
    collection = 'PASS' if len(rows) == len(drafts) * run['repeats'] and len(controls) == planned_controls else 'INCOMPLETE'
    control_status = combine([r['status'] for r in controls] + ([] if len(controls) == planned_controls else ['INCOMPLETE'])) if planned_controls else 'NOT_APPLICABLE'
    prior_status = result['status'] if result else 'INCOMPLETE'
    telemetry_status = 'INCOMPLETE' if missing_http else 'PASS'
    metadata = {'status': combine([stability, identity_status, collection, prior_status,
                                  header_status, telemetry_status]
                                  + ([] if control_status == 'NOT_APPLICABLE' else [control_status])),
                'source': source.relative_to(root).as_posix(), 'historical_source_stability': stability,
                'model_identity_stability': identity_status, 'collection_status': collection,
                'header_status': header_status,
                'http_telemetry': {'status': telemetry_status, 'unrecorded_prompt_attempts': missing_http,
                    'scope': 'Recorded prompts without HTTP telemetry remain attempts with unknown time and usage'},
                'reevaluation_status': 'PASS', 'prior_report_status': prior_status,
                'prior_report_scope': 'Historical collector status; no new live model call or current product acceptance',
                'model': run['model'], 'model_digest': run['model_digest'],
                'temperature': run.get('temperature'), 'seed': run.get('seed'),
                'sampling': 'convenience_synthetic_families', 'population_inference': 'NOT_ESTABLISHED',
                'budget_enforcement': 'NOT_EVALUATED', 'predeclared_total_budget': 'ABSENT',
                'queue_wait': 'NOT_MEASURED', 'money_cost': 'NOT_MEASURED', 'new_model_calls': 0,
                'load_state_counts': dict(load_states), 'controls': {'planned': planned_controls,
                    'observed': len(controls), 'status': control_status, 'rows': controls},
                'missing_files': reader.missing,
                'historical_batch_elapsed_seconds': result.get('elapsed_seconds') if result else None,
                'provenance_scope': 'Locally pinned saved records and self-reported before/after identities; not signatures or present-machine model identity'}
    _require(_number(metadata['historical_batch_elapsed_seconds'], nullable=True), 'HISTORY_BATCH_TIME')
    _require(verify_files(reader.files, root)['status'] == 'PASS', 'HISTORY_FILE_CHANGED')
    return {'plan': plan, 'observations': rows, 'source_files': reader.files, 'metadata': metadata}
