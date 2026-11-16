"""Read-only, hash-pinned QA evidence aggregation. No services or model imports."""
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
import json
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
KINDS = {'unit', 'checks', 'browser', 'browser_ci', 'pipeline', 'retrieval', 'llm', 'judge', 'human'}
STATUSES = {'PASS', 'FAIL', 'ERROR', 'INCOMPLETE'}


def strict_json(text):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('DUPLICATE_JSON_KEY')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('NONFINITE_JSON')
    def floating(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError('NONFINITE_JSON')
        return value
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=floating)


def checked(root, name):
    root = Path(root).resolve()
    if not isinstance(name, str) or not name or '\\' in name or ':' in name:
        raise ValueError('REPORT_RELATIVE_PATH_REQUIRED')
    path = root / name
    if Path(name).is_absolute() or any(part in {'.', '..'} for part in name.split('/')):
        raise ValueError('REPORT_PATH_ESCAPE')
    path.resolve().relative_to(root)
    for item in (path, *path.parents):
        if item == root:
            break
        if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
            raise ValueError('REPORT_LINK_FORBIDDEN')
    return path


def pin(root, name):
    path = checked(root, name)
    return {'path': name, 'sha256': sha256(path.read_bytes()).hexdigest() if path.is_file() else None}


def combine(statuses):
    return next((state for state in ('ERROR', 'FAIL', 'INCOMPLETE') if state in statuses), 'PASS') if statuses else 'INCOMPLETE'


def number(value):
    return type(value) is int and value >= 0


def numeric_tree(value):
    if value is None or type(value) in (int, float):
        return value
    if isinstance(value, dict) and all(re.fullmatch(r'[a-z0-9_]+', key) for key in value):
        return {key: numeric_tree(item) for key, item in value.items()}
    raise ValueError('INVALID_NUMERIC_METRIC')


def junit(data):
    # Parse test cases rather than trusting an XML root's summary attributes.
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('REPORT_XML_ENTITIES_FORBIDDEN')
    tree = ET.fromstring(data)
    rows = list(tree.iter('testcase'))
    parsed = dict(tests=len(rows), **{kind: sum(any(child.tag == kind for child in row) for row in rows)
                                    for kind in ('failure', 'error', 'skipped')})
    suites = list(tree.iter('testsuite'))
    try:
        parsed['reported_counts'] = {key: sum(int(suite.attrib[attribute]) for suite in suites)
            for key, attribute in (('tests', 'tests'), ('failure', 'failures'), ('error', 'errors'), ('skipped', 'skipped'))}
    except (KeyError, ValueError):
        parsed['reported_counts'] = None
    return parsed


def load_files(profile, root):
    loaded, evidence, problems = {}, [], []
    for role, reference in profile['files'].items():
        if not re.fullmatch(r'[A-Za-z0-9_.:/-]+', role) or not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}:
            raise ValueError('REPORT_REFERENCE_INVALID')
        path = checked(root, reference['path'])
        if path.parts[len(Path(root).resolve().parts)] not in {'verification', 'var', 'qa'}:
            raise ValueError('REPORT_INPUT_SCOPE')
        item = dict(role=role, **reference, integrity='PASS')
        if not path.is_file() or reference['sha256'] is None:
            item['integrity'] = 'INCOMPLETE'
        else:
            raw = path.read_bytes()
            if len(raw) > 32 * 1024 * 1024:
                raise ValueError('REPORT_INPUT_TOO_LARGE')
            if sha256(raw).hexdigest() != reference['sha256']:
                item['integrity'] = 'ERROR'
            else:
                try:
                    if path.suffix == '.xml':
                        loaded[role] = junit(raw)
                    elif path.suffix == '.jsonl':
                        loaded[role] = [strict_json(line) for line in raw.decode('utf-8-sig').splitlines() if line.strip()]
                    elif path.suffix == '.zip':
                        loaded[role] = {'bytes': len(raw), 'sha256': reference['sha256']}
                    else:
                        loaded[role] = strict_json(raw.decode('utf-8-sig'))
                except (ValueError, ET.ParseError, UnicodeError):
                    item['integrity'] = 'ERROR'
        if item['integrity'] != 'PASS':
            problems.append(role + ':' + item['integrity'])
        evidence.append(item)
    return loaded, evidence, problems


def assess(kind, files):
    if kind == 'human':
        deferred = files.get('decision', {}).get('human_calibration') == 'DEFERRED_BY_USER'
        return 'INCOMPLETE', {'reviews': 0, 'calibration': 'DEFERRED_BY_USER' if deferred else 'PENDING'}, ['human_ratings_absent']
    result = files.get('result')
    if not isinstance(result, dict):
        return 'INCOMPLETE', {}, ['result_missing']
    stated = result.get('status')
    if stated not in STATUSES:
        return 'ERROR', {}, ['invalid_result_status']
    facts, issues = {}, []
    state = stated
    if kind == 'checks':
        checks = result.get('checks')
        if not isinstance(checks, list) or not checks:
            return 'INCOMPLETE', {}, ['checks_empty']
        facts = {'checks': len(checks), 'passed': sum(row.get('passed') is True for row in checks)}
        if facts['checks'] != facts['passed']:
            state = combine([state, 'FAIL'])
    elif kind in {'unit', 'browser', 'pipeline', 'browser_ci'}:
        keys = ('tests', 'failures', 'errors', 'skipped') if kind == 'unit' else ('testcases', 'failure', 'error', 'skipped')
        if not all(number(result.get(key)) for key in keys):
            return 'ERROR', {}, ['invalid_test_counts']
        facts = {key: result[key] for key in keys}
        if result[keys[0]] == 0:
            state = combine([state, 'INCOMPLETE'])
        if any(result[key] for key in keys[1:]):
            state = combine([state, 'FAIL'])
        if result.get('pytest_exit_code') != 0:
            state = combine([state, 'FAIL' if result.get('pytest_exit_code') is not None else 'INCOMPLETE'])
        if kind == 'browser_ci':
            facts['cleanup_status'] = result.get('cleanup_status') if result.get('cleanup_status') in {'PASS', 'FAIL', 'NOT_STARTED'} else 'UNKNOWN'
            if facts['cleanup_status'] != 'PASS':
                state = combine([state, 'FAIL' if facts['cleanup_status'] == 'FAIL' else 'INCOMPLETE'])
        else:
            parsed = files.get('junit')
            expected = dict(zip(('tests', 'failure', 'error', 'skipped'), (result[k] for k in keys)))
            if parsed is None:
                state = combine([state, 'INCOMPLETE'])
                issues.append('junit_missing')
            else:
                nodes = {key: parsed[key] for key in expected}
                # Pytest 9.1 records successful subtests only in suite totals.
                # Accept that representation only with an agreeing collected-test
                # count, declared total and zero failure/skip/error evidence.
                aggregated = (kind == 'unit' and stated == 'PASS'
                    and number(result.get('additional_subtests')) and result['additional_subtests'] > 0
                    and nodes['tests'] == result.get('collected_tests')
                    and parsed.get('reported_counts') == expected
                    and not any(nodes[key] or expected[key] for key in ('failure', 'error', 'skipped')))
                if nodes != expected and not aggregated:
                    state = 'ERROR'
                    issues.append('junit_counts_disagree')
                if aggregated:
                    facts['subtests_accounting'] = 'pytest_suite_totals_consistent; successful subtests have no individual XML nodes'
        if kind == 'unit':
            for key in ('collected_tests', 'additional_subtests'):
                facts[key] = result.get(key)
            if not all(number(facts[key]) for key in ('collected_tests', 'additional_subtests')) or facts['collected_tests'] + facts['additional_subtests'] != facts['tests']:
                state = 'ERROR'
            contracts = result.get('contract_artifacts_match')
            if not isinstance(contracts, list) or not contracts or not all(row.get('passed') is True for row in contracts):
                state = combine([state, 'FAIL'])
        if kind == 'pipeline' and (result.get('collection_complete') is not True or result.get('expected_testcases') != result['testcases']):
            state = combine([state, 'INCOMPLETE'])
    elif kind == 'retrieval':
        for key in ('ranking_cases', 'primary_query_count', 'metamorphic_cases', 'metamorphic_family_count', 'embedding_calls'):
            if not number(result.get(key)) or result[key] == 0:
                return 'INCOMPLETE', {}, ['retrieval_counts_missing']
            facts[key] = result[key]
        facts['primary_macro'] = numeric_tree(result.get('primary_macro', {}))
        facts['ranking_quality_acceptance'] = 'NOT_ESTABLISHED'
        state = combine([state, *[result.get(key) if result.get(key) in STATUSES else 'INCOMPLETE'
                                 for key in ('ranking_status', 'metamorphic_status')]])
    elif kind == 'llm':
        summary = result.get('summary', {})
        for key in ('planned_repeat_count', 'observed_repeat_count', 'missing_repeat_count', 'fact_violation_count', 'observed_independence_family_count'):
            if not number(summary.get(key)):
                return 'INCOMPLETE', {}, ['llm_counts_missing']
            facts[key] = summary[key]
        if facts['planned_repeat_count'] == 0 or facts['planned_repeat_count'] != facts['observed_repeat_count'] or facts['missing_repeat_count']:
            state = combine([state, 'INCOMPLETE'])
        if facts['fact_violation_count']:
            state = combine([state, 'FAIL'])
        state = combine([state, summary.get('status') if summary.get('status') in STATUSES else 'INCOMPLETE'])
        stable = result.get('model_identity_stable')
        facts['model_identity_stable'] = stable if type(stable) is bool else None
        if stable is not True:
            state = combine([state, 'FAIL' if stable is False else 'INCOMPLETE'])
        if result.get('full_planned_matrix') is not True or result.get('collection_complete') is not True:
            state = combine([state, 'INCOMPLETE'])
        templates = result.get('template_failures')
        if not isinstance(templates, list):
            state = combine([state, 'INCOMPLETE'])
        elif templates:
            state = combine([state, 'FAIL'])
        controls = result.get('controls')
        count = result.get('control_observation_count')
        if not isinstance(controls, list) or not controls or not number(count) or len(controls) != count:
            state = combine([state, 'INCOMPLETE'])
        else:
            state = combine([state, *[item.get('status') if item.get('status') in STATUSES else 'INCOMPLETE' for item in controls]])
            if any(item.get('issues') for item in controls):
                state = combine([state, 'FAIL'])
        facts['control_observation_count'] = count if number(count) else None
        if result.get('draft_observation_count') != facts['observed_repeat_count'] or not number(result.get('repeats_per_draft_case')) or result['repeats_per_draft_case'] < 3:
            state = combine([state, 'INCOMPLETE'])
        facts['ordering_usefulness'] = 'NOT_ESTABLISHED'
    elif kind == 'judge':
        summary = result.get('summary', {})
        overall = summary.get('overall', {})
        for key in ('planned', 'valid_responses', 'missing', 'error', 'timeout', 'invalid_response'):
            if not number(overall.get(key)):
                return 'INCOMPLETE', {}, ['judge_counts_missing']
            facts[key] = overall[key]
        if facts['planned'] == 0 or sum(facts[k] for k in ('valid_responses', 'missing', 'error', 'timeout', 'invalid_response')) != facts['planned']:
            return 'ERROR', facts, ['judge_counts_disagree']
        for key in ('execution_status', 'known_challenge_status'):
            facts[key] = summary.get(key) if summary.get(key) in STATUSES else 'INCOMPLETE'
        state = combine([state, facts['execution_status'], facts['known_challenge_status']])
        stable = result.get('model_identity_stable')
        if stable is not None and type(stable) is not bool:
            return 'ERROR', facts, ['invalid_model_identity_flag']
        facts['model_identity_stable'] = stable
        human = summary.get('human_calibration', {})
        if not number(human.get('supplied_reviews')):
            return 'ERROR', facts, ['invalid_human_review_count']
        facts['human_reviews'] = human['supplied_reviews']
        if any(facts[k] for k in ('error', 'timeout', 'invalid_response')) or facts['model_identity_stable'] is False:
            state = combine([state, 'FAIL'])
        paired = human.get('paired_valid_observations')
        if facts['missing'] or stable is not True or result.get('full_planned_matrix') is not True or human.get('status') != 'EVIDENCE_COMPLETE' or facts['human_reviews'] != facts['planned'] or not number(paired) or paired != facts['planned']:
            state = combine([state, 'INCOMPLETE'])
        facts['suitability'] = 'NOT_ESTABLISHED'
        accuracy = overall.get('all_planned_accuracy', {})
        facts['all_planned_accuracy'] = numeric_tree({key: accuracy.get(key) for key in ('numerator', 'denominator', 'rate')})
    if result.get('source_files_unchanged_during_run') is False or result.get('source_files_unchanged') is False:
        state = combine([state, 'FAIL'])
        issues.append('measured_sources_changed')
    if stated == 'PASS' and state != 'PASS':
        issues.append('claimed_pass_not_supported')
    return state, facts, issues


def allowed_scalar(value):
    # Public report carries identifiers/versions, never arbitrary source prose.
    return value if value is None or type(value) in (bool, int, float) or (isinstance(value, str) and len(value) <= 240 and re.fullmatch(r'[\w .,:/@+()\[\]%-]+', value)) else None


def metadata(files):
    output = {}
    fields = ('run_id', 'probe_id', 'model', 'model_id', 'model_digest', 'dataset_manifest_sha256',
              'dataset_sha256', 'rubric_sha256', 'system_prompt_sha256', 'model_manifest_sha256',
              'embedding_manifest_sha256', 'temperature', 'num_predict', 'num_ctx', 'seed', 'python',
              'platform', 'cpu', 'logical_cpus', 'device', 'engine', 'version', 'started_at_utc',
              'finished_at_utc', 'checked_at_utc', 'status')
    for role in ('result', 'run', 'runtime', 'browser', 'ollama_before', 'ollama_after'):
        data = files.get(role)
        if not isinstance(data, dict):
            continue
        row = {key: allowed_scalar(data[key]) for key in fields if key in data and key != 'status'}
        if isinstance(data.get('model'), dict):
            row['model_digest'] = allowed_scalar(data['model'].get('digest'))
        if isinstance(data.get('server'), dict):
            row['server_version'] = allowed_scalar(data['server'].get('version'))
        if isinstance(data.get('packages'), dict):
            row['packages'] = {key: allowed_scalar(value) for key, value in data['packages'].items() if re.fullmatch(r'[\w.-]+', key)}
        output[role] = row
    if isinstance(files.get('sources'), dict):
        output['measured_code'] = {'file_count': len(files['sources']),
            'comparison_to_current_checkout': 'NOT_ASSERTED; see pinned sources artifact'}
    return output


def correlation_rows(files, profile):
    output = []
    for role, data in files.items():
        if not role.startswith('case:') or not isinstance(data, dict):
            continue
        row = dict(profile_id=profile['id'], evidence=profile['files'][role]['path'],
                   qa_run_id=None, case_id=None, repetition=None, event_id=None, kafka=[],
                   job_id=None, report_id=None, report_version=None, report_sha256=None,
                   ui_opened_job_id=None, ui_opened_report_id=None, browser_trace=None,
                   judge_case_id=None, judge_source_case_id=None, correlation_id=None,
                   snapshot_id=None, policy_version=None)
        nested = {}
        if profile['kind'] == 'pipeline':
            row.update(qa_run_id=data.get('run_id'), case_id=data.get('case_id'))
            for observation in data.get('observations', []):
                if 'event_id' in observation:
                    row['event_id'] = observation['event_id']
                for source_key in ('source', 'first_source', 'second_source', 'original_source', 'rejected_source'):
                    if isinstance(observation.get(source_key), dict):
                        row['kafka'].append(dict(relation=source_key, **{key: allowed_scalar(observation[source_key].get(key)) for key in ('topic', 'partition', 'offset')}))
                for item in observation.get('dlq_records', []):
                    row['kafka'].append(dict(relation='dlq', **{key: allowed_scalar(item.get(key)) for key in ('topic', 'partition', 'offset', 'reason_code')}))
                consistency = observation.get('consistency', {})
                for key in ('job_id', 'report_id', 'report_version', 'event_id'):
                    if key in consistency:
                        row[key] = consistency[key]
                row['report_sha256'] = consistency.get('content_sha256', row['report_sha256'])
                ui = observation.get('ui', {})
                row['ui_opened_job_id'] = ui.get('opened_job_id', row['ui_opened_job_id'])
                row['ui_opened_report_id'] = ui.get('opened_report_id', row['ui_opened_report_id'])
        elif profile['kind'] == 'llm':
            evaluation = data.get('evaluation', {})
            row.update(case_id=evaluation.get('case_id'), repetition=evaluation.get('repeat_index'),
                       report_sha256=evaluation.get('draft', {}).get('ordered_fingerprint'))
            observed = data.get('observation', {})
            report = observed.get('result', {})
            for key in ('report_id', 'report_version', 'snapshot_id', 'policy_version'):
                row[key] = report.get(key)
            ids = {event.get('correlation_id') for event in observed.get('events', []) if event.get('correlation_id')}
            if len(ids) == 1:
                row['correlation_id'] = ids.pop()
        elif profile['kind'] == 'judge':
            row['judge_case_id'] = data.get('observation', {}).get('case_id')
            # Dataset relation is synthetic derivation, never an event/job join.
            cases = files.get('dataset', {}).get('cases', [])
            source = next((item for item in cases if item.get('id') == row['judge_case_id']), {})
            row['judge_source_case_id'] = source.get('source_case_id')
            row['judge_mutation'] = source.get('mutation')
            row['judge_criterion'] = source.get('payload', {}).get('criterion')
            suffix = '/' + str(row['judge_source_case_id']) + '/repeat-01.json'
            refs = files.get('dataset', {}).get('source_hashes', {})
            nested['source_llm'] = [{'path': path, 'sha256': value['sha256'], 'repeat_index': 1}
                                    for path, value in refs.items() if path.startswith('verification/') and path.endswith(suffix)]
            nested['candidate_labels'] = [allowed_scalar(candidate.get('label')) for candidate in data.get('payload', {}).get('candidates', [])]
        elif profile['kind'] == 'browser':
            row['case_id'] = data.get('node_id')
            row['qa_run_id'] = files.get('result', {}).get('run_id')
            observations = files.get('observations:' + role.split(':', 1)[1], [])
            jobs = {}
            for observation in observations:
                response = observation.get('response', {})
                if not isinstance(response, dict) or not isinstance(response.get('job_id'), str):
                    continue
                job = jobs.setdefault(response['job_id'], {'job_id': allowed_scalar(response['job_id']), 'report_id': None, 'report_version': None})
                report = response.get('result')
                if isinstance(report, dict):
                    job.update(report_id=allowed_scalar(report.get('report_id')), report_version=allowed_scalar(report.get('report_version')))
            nested['browser_jobs'] = list(jobs.values())
            trace_role = 'trace:' + role.split(':', 1)[1]
            if trace_role in files:
                row['browser_trace'] = profile['files'][trace_role]['path']
        for key in row.keys() - {'kafka', 'evidence'}:
            row[key] = allowed_scalar(row[key])
        row.update(nested)
        row['missing_links_are_not_inferred'] = True
        output.append(row)
    return output


def build_report(index, root=ROOT):
    if set(index) != {'schema_version', 'scope', 'profiles'} or index['schema_version'] != '1.0' or index['scope'] not in {'historical', 'ci-hermetic', 'ci-browser'}:
        raise ValueError('REPORT_INDEX_INVALID')
    if not isinstance(index['profiles'], list) or not index['profiles']:
        raise ValueError('REPORT_PROFILES_EMPTY')
    profiles, identities, links = [], set(), []
    for spec in index['profiles']:
        if set(spec) != {'id', 'kind', 'files'} or not re.fullmatch(r'[a-z0-9-]+', spec['id']) or spec['id'] in identities or spec['kind'] not in KINDS or not isinstance(spec['files'], dict):
            raise ValueError('REPORT_PROFILE_INVALID')
        identities.add(spec['id'])
        files, evidence, problems = load_files(spec, root)
        try:
            state, metrics, issues = assess(spec['kind'], files)
            versions = metadata(files)
            links.extend(correlation_rows(files, spec))
        except (TypeError, KeyError, AttributeError, ValueError):
            state, metrics, issues, versions = 'ERROR', {}, ['malformed_evidence'], {}
        state = combine([state, *[item['integrity'] for item in evidence]])
        profiles.append(dict(id=spec['id'], kind=spec['kind'], status=state, metrics=metrics,
            issues=problems + issues, evidence=evidence, versions=versions,
            product_acceptance='INCOMPLETE', historical=index['scope'] == 'historical'))
    available = {item['path']: item['sha256'] for row in profiles for item in row['evidence'] if item['integrity'] == 'PASS'}
    for row in links:
        for source in row.get('source_llm', []):
            source['hash_verified_in_report'] = available.get(source['path']) == source['sha256']
    return dict(schema_version='1.0', created_at_utc=datetime.now(timezone.utc).isoformat(),
        scope=index['scope'], status=combine([row['status'] for row in profiles]), profiles=profiles,
        evidence_integrity=combine([item['integrity'] for row in profiles for item in row['evidence']]),
        correlation_rows=links, product_acceptance='INCOMPLETE',
        remote_ci='NOT_VERIFIED_BY_THIS_REPORT',
        interpretation='Profile results describe their own measured snapshots. Historical evidence is not a new run or release acceptance. Null links are absent, never inferred across separate runs.')


def write_report(report, index, output, root=ROOT):
    name = Path(output).as_posix()
    path = checked(root, name)
    if name.split('/')[0] not in {'var', 'verification'} or len(name.split('/')) < 3:
        raise ValueError('REPORT_OUTPUT_SCOPE')
    if path.exists():
        raise FileExistsError('REPORT_OUTPUT_EXISTS')
    # Rendering uses only this allowlist; raw prompts/response bodies/config keys are absent.
    rows = ['# QA: ' + report['status'], '',
        'Область: ' + report['scope'] + '. Приёмка продукта: INCOMPLETE. Удалённый CI этим отчётом не подтверждён.', '',
        '| Профиль | Итог | Метрики |', '|---|---|---|']
    for profile in report['profiles']:
        metrics = json.dumps(profile['metrics'], ensure_ascii=False, sort_keys=True)
        rows.append('| ' + profile['id'] + ' | ' + profile['status'] + ' | ' + escape(metrics).replace('|', '&#124;').replace('\n', ' ') + ' |')
    rows += ['', 'Исторические результаты относятся к своим снимкам. Успешные тесты не отменяют ошибки модели или отсутствующие свидетельства.',
             '', 'Подробные версии, хеши входов и реальные связи: report.json. В HTML доступны таблицы и источники.', '']
    markdown = '\n'.join(rows)
    sections = []
    for profile in report['profiles']:
        evidence = ''.join('<li>' + escape(item['role'] + ': ' + item['path'] + ' [' + item['integrity'] + ']') + '</li>' for item in profile['evidence'])
        sections.append('<section><h2>' + escape(profile['id'] + ' — ' + profile['status']) + '</h2><pre>' + escape(json.dumps(profile['metrics'], ensure_ascii=False, indent=2)) + '</pre><details><summary>Версии и источники</summary><pre>' + escape(json.dumps(profile['versions'], ensure_ascii=False, indent=2)) + '</pre><ul>' + evidence + '</ul></details></section>')
    html = '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>QA report</title><style>body{font:16px system-ui;max-width:1100px;margin:32px auto;padding:0 20px;color:#172b4d;background:#f5f7fa}section{background:white;padding:20px;margin:16px 0;border:1px solid #cbd5e1;border-radius:8px}pre{white-space:pre-wrap;overflow-wrap:anywhere}li{overflow-wrap:anywhere}h1{color:#854d0e}</style><h1>QA — ' + escape(report['status']) + '</h1><p>Область: ' + escape(report['scope']) + '. Приёмка продукта: INCOMPLETE.</p><p>Каждый профиль относится к своему измерению. Пробелы не считаются успехом; исторический свод не подтверждает текущий удалённый CI.</p>' + ''.join(sections) + '<section><h2>Связи между событиями и результатами</h2><p>null означает отсутствие свидетельства. Разные запуски не объединяются по одному имени кейса.</p><pre>' + escape(json.dumps(report['correlation_rows'], ensure_ascii=False, indent=2)) + '</pre></section></html>'
    rendered = {'report.json': json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n',
                'index.json': json.dumps(index, ensure_ascii=False, indent=2, allow_nan=False) + '\n',
                'summary.md': markdown, 'report.html': html}
    # Detect credential-shaped material in the exact files about to be published.
    pattern = re.compile(r'(?i)(?:bearer\s+[A-Za-z0-9._~-]{12,}|(?:gh[pousr]_|github_pat_|sk-)[A-Za-z0-9_\-]{15,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)')
    if any(pattern.search(content) for content in rendered.values()):
        raise ValueError('REPORT_SECRET_PATTERN_DETECTED')
    path.mkdir(parents=True, exist_ok=False)
    for filename, content in rendered.items():
        (path / filename).write_text(content, encoding='utf-8')
    return {'PASS': 0, 'FAIL': 1, 'ERROR': 1, 'INCOMPLETE': 3}[report['status']]
