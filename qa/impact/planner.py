"""Conservative, explained planning over a curated component graph.

The input is a declaration, not an observed Git diff. No tests are executed and
no reduced suite is authorized: full regression remains mandatory.
"""
from collections import deque
from copy import deepcopy
import re

from qa.requirements.catalog import canonical_digest
from scripts.release.package_release import valid_path


PROFILES = {
    'full_regression': ('Полная регрессия и сверка контрактов', 'scripts/rebuild/run_checks.py'),
    'requirements': ('Выполнение каталога требований', 'qa/tools/check_requirements.py'),
    'ui_cli': ('Интерфейс AppTest и контракты CLI', 'scripts/diagnostics/qualify_ci_ui.py'),
    'browser': ('Браузерный smoke с изолированным стендом', 'qa/tools/run_ci_browser.py'),
    'retrieval': ('Измерения настоящего embedding-поиска', 'qa/tools/run_retrieval.py'),
    'llm': ('Настоящая LLM: факты, отказы и вариативность', 'qa/tools/run_llm.py'),
    'judge': ('Проверки настоящего консультативного Judge', 'qa/tools/run_judge.py'),
    'live_integration': ('Живая цепочка SQL/API/Kafka → AML → UI', None),
    'documentation': ('Проверка ссылок, команд и смысла изменённых инструкций', None),
    'mapping_review': ('Уточнение требований и недоказанных связей компонентов', None),
}
MAP_KEYS = {'schema_version', 'map_id', 'version', 'catalog_sha256', 'components'}
COMPONENT_KEYS = {'id', 'paths', 'depends_on', 'requirements', 'profiles', 'reason'}
MODEL_COMPONENTS = {'llm': 'model_llm', 'embedding': 'model_embedding'}


class ImpactError(ValueError):
    """Fixed error code for a rejected declaration or map."""


def require(condition, code):
    if not condition:
        raise ImpactError(code)


def relative_name(value):
    require(type(value) is str and len(value) <= 512, 'INVALID_CHANGE_PATH')
    name = value.replace('\\', '/')
    require(valid_path(name) and name not in ('.', '..'), 'INVALID_CHANGE_PATH')
    return name


def _strings(value, code, *, empty=True):
    require(type(value) is list and len(value) <= 256 and (empty or bool(value))
            and all(type(item) is str and item.strip() for item in value), code)
    require(len(value) == len(set(value)), code)


def normalize_changes(document):
    require(type(document) is dict and set(document) == {'schema_version', 'changes', 'model_changes'},
            'INVALID_CHANGE_FIELDS')
    require(document['schema_version'] == '1.0', 'UNSUPPORTED_CHANGE_VERSION')
    require(type(document['changes']) is list and len(document['changes']) <= 4096, 'INVALID_CHANGES')
    _strings(document['model_changes'], 'INVALID_MODEL_CHANGES')
    require(all(item in MODEL_COMPONENTS for item in document['model_changes']), 'INVALID_MODEL_CHANGES')
    rows, seen = [], set()
    for row in document['changes']:
        require(type(row) is dict and row.get('status') in ('A', 'M', 'D', 'R'), 'INVALID_CHANGE_STATUS')
        require(set(row) == ({'status', 'path', 'previous_path'} if row['status'] == 'R'
                             else {'status', 'path'}), 'INVALID_CHANGE_FIELDS')
        item = {'status': row['status'], 'path': relative_name(row['path'])}
        names = [item['path']]
        if row['status'] == 'R':
            item['previous_path'] = relative_name(row['previous_path'])
            require(item['path'] != item['previous_path'], 'INVALID_RENAME')
            names.append(item['previous_path'])
        folded = {name.casefold() for name in names}
        require(not seen.intersection(folded), 'DUPLICATE_CHANGE_PATH')
        seen.update(folded)
        rows.append(item)
    return {'schema_version': '1.0', 'changes': sorted(rows, key=lambda r: (r['path'], r['status'])),
            'model_changes': sorted(document['model_changes'])}


def validate_map(mapping, catalog):
    require(type(mapping) is dict and set(mapping) == MAP_KEYS, 'INVALID_MAP_FIELDS')
    require(mapping['schema_version'] == mapping['version'] == '1.0'
            and mapping['map_id'] == 'dark-factory-impact-v1', 'UNSUPPORTED_MAP_VERSION')
    require(mapping['catalog_sha256'] == canonical_digest(catalog), 'IMPACT_CATALOG_CHANGED')
    rows = mapping['components']
    require(type(rows) is list and 2 <= len(rows) <= 128, 'INVALID_COMPONENTS')
    ids, covered = set(), set()
    requirements = {r['id'] for r in catalog['requirements']}
    for row in rows:
        require(type(row) is dict and set(row) == COMPONENT_KEYS, 'INVALID_COMPONENT_FIELDS')
        require(type(row['id']) is str and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', row['id']), 'INVALID_COMPONENT_ID')
        require(row['id'] not in ids, 'DUPLICATE_COMPONENT_ID')
        ids.add(row['id'])
        require(type(row['reason']) is str and 0 < len(row['reason'].strip()) <= 4000, 'INVALID_COMPONENT_REASON')
        for field in ('paths', 'depends_on', 'requirements', 'profiles'):
            _strings(row[field], 'INVALID_COMPONENT_' + field.upper(),
                     empty=field != 'paths' or row['id'] in MODEL_COMPONENTS.values())
        normalized = []
        for path in row['paths']:
            name = path[:-1] if path.endswith('/') else path
            require(relative_name(name) == name, 'INVALID_MAP_PATH')
            normalized.append(path.casefold())
        require(len(set(normalized)) == len(normalized), 'DUPLICATE_COMPONENT_PATH')
        require(set(row['requirements']).issubset(requirements), 'UNKNOWN_REQUIREMENT')
        require(set(row['profiles']).issubset(PROFILES), 'UNKNOWN_PROFILE')
        covered.update(row['requirements'])
    require(set(MODEL_COMPONENTS.values()).issubset(ids), 'MODEL_COMPONENTS_REQUIRED')
    require(covered == requirements, 'UNMAPPED_REQUIREMENT')
    by_id = {row['id']: row for row in rows}
    for component, profile in (('model_llm', 'llm'), ('model_embedding', 'retrieval')):
        require(profile in by_id[component]['profiles'], 'MODEL_PROFILE_REQUIRED')
    for row in rows:
        require(set(row['depends_on']).issubset(ids), 'UNKNOWN_COMPONENT_DEPENDENCY')
    active, done = set(), set()

    def visit(component):
        require(component not in active, 'COMPONENT_CYCLE')
        if component in done:
            return
        active.add(component)
        for dependency in by_id[component]['depends_on']:
            visit(dependency)
        active.remove(component)
        done.add(component)

    for component in sorted(ids):
        visit(component)


def _matches(name, pattern):
    name, pattern = name.casefold(), pattern.casefold()
    return name.startswith(pattern) if pattern.endswith('/') else name == pattern


def build_plan(catalog, mapping, changes):
    validate_map(mapping, catalog)
    changes = normalize_changes(changes)
    components = {row['id']: row for row in mapping['components']}
    consumers = {name: [] for name in components}
    for row in components.values():
        for dependency in row['depends_on']:
            consumers[dependency].append(row['id'])
    reasons = {row['id']: [] for row in catalog['requirements']}
    profiles = {'full_regression': {'Обязательная регрессия: план не разрешает сокращение приёмочных проверок.'}}
    all_components, impacts, unknown = set(), [], []
    inputs = sorted({name for row in changes['changes'] for name in
                     ([row['path'], row['previous_path']] if row['status'] == 'R' else [row['path']])})
    seeds = [(name, None) for name in inputs]
    seeds += [('model:' + model, MODEL_COMPONENTS[model]) for model in changes['model_changes']]
    for name, model_component in seeds:
        direct = sorted([model_component] if model_component else
                        [key for key, row in components.items() if any(_matches(name, p) for p in row['paths'])])
        chains = {key: [key] for key in direct}
        queue = deque(direct)
        while queue:
            parent = queue.popleft()
            for child in sorted(consumers[parent]):
                if child not in chains:
                    chains[child] = chains[parent] + [child]
                    queue.append(child)
        exact_requirements = []
        if not model_component:
            for requirement in catalog['requirements']:
                files = {s.split('::')[0] for case in requirement['scenarios'] for s in case['test_selectors']}
                files.add(requirement['expectation_source']['path'])
                if name.casefold() in {path.casefold() for path in files}:
                    exact_requirements.append(requirement['id'])
                    profiles.setdefault('mapping_review', set()).add(
                        name + ': пересмотреть связь теста/основания с ожидаемым поведением требования.')
                    reasons[requirement['id']].append({'input': name, 'component': None, 'chain': [],
                        'reason': 'Изменён тест или источник ожидания, прямо указанный в каталоге.'})
        known = bool(direct or exact_requirements)
        if not known:
            unknown.append(name)
        for key, chain in sorted(chains.items()):
            row = components[key]
            all_components.add(key)
            for requirement in row['requirements']:
                reasons[requirement].append({'input': name, 'component': key, 'chain': chain,
                                             'reason': row['reason']})
            for profile in row['profiles']:
                profiles.setdefault(profile, set()).add(name + ' → ' + ' → '.join(chain))
        impacts.append({'input': name, 'status': 'MAPPED' if known else 'UNKNOWN', 'direct_components': direct,
                        'matched_patterns': {key: sorted(p for p in components[key]['paths'] if _matches(name, p))
                                             for key in direct},
                        'catalog_requirements': sorted(exact_requirements),
                        'component_chains': [{'component': key, 'chain': chain} for key, chain in sorted(chains.items())]})
    if unknown:
        for requirement in reasons:
            reasons[requirement].append({'input': None, 'component': None, 'chain': [],
                'reason': 'Неизвестное влияние: требуется весь каталог и уточнение карты; полнота не установлена.'})
        for profile in PROFILES:
            profiles.setdefault(profile, set()).add('Неизвестные пути: ' + ', '.join(unknown))
    rows = []
    for requirement in sorted(catalog['requirements'], key=lambda r: r['id']):
        if reasons[requirement['id']]:
            rows.append({'id': requirement['id'], 'title': requirement['title'], 'priority': requirement['priority'],
                         'reasons': reasons[requirement['id']],
                         'test_selectors': sorted({s for case in requirement['scenarios'] for s in case['test_selectors']})})
    if rows:
        profiles.setdefault('requirements', set()).add('Требования, выбранные по карте или консервативному резервному правилу.')
    return {'schema_version': '1.0', 'planning_status': 'INCOMPLETE' if unknown else 'READY',
            'execution_status': 'NOT_RUN', 'requirements_status': 'INCOMPLETE', 'product_acceptance': 'INCOMPLETE',
            'candidate_admission': 'NOT_EVALUATED', 'input_completeness': 'NOT_VERIFIED',
            'mandatory_full_regression': True, 'catalog_sha256': canonical_digest(catalog),
            'map_sha256': canonical_digest(mapping), 'change_set_sha256': canonical_digest(changes),
            'changes': changes, 'unknown_paths': sorted(unknown), 'path_impacts': impacts,
            'affected_components': sorted(all_components), 'requirements': rows,
            'selected_test_selectors': sorted({s for row in rows for s in row['test_selectors']}),
            'requirements_without_declared_impact': sorted(set(reasons) - {r['id'] for r in rows}),
            'profiles': [{'id': key, 'description': PROFILES[key][0], 'entrypoint': PROFILES[key][1],
                          'status': 'NOT_RUN', 'reasons': sorted(value)} for key, value in sorted(profiles.items())],
            'scope': 'Declared paths/model changes and curated component graph only. No Git discovery, '
                     'semantic dependency proof, execution, model identity verification or release decision.'}


def conservative_fallback(report, catalog, reason):
    """Keep discovered chains but withdraw completeness when references broke."""
    report = deepcopy(report)
    rows = {row['id']: row for row in report['requirements']}
    for requirement in catalog['requirements']:
        row = rows.setdefault(requirement['id'], {
            'id': requirement['id'], 'title': requirement['title'], 'priority': requirement['priority'],
            'reasons': [], 'test_selectors': sorted({s for case in requirement['scenarios'] for s in case['test_selectors']})})
        row['reasons'].append({'input': None, 'component': None, 'chain': [], 'reason': reason})
    profiles = {row['id']: row for row in report['profiles']}
    for key, (description, entrypoint) in PROFILES.items():
        row = profiles.setdefault(key, {'id': key, 'description': description, 'entrypoint': entrypoint,
                                        'status': 'NOT_RUN', 'reasons': []})
        row['reasons'] = sorted(set(row['reasons']) | {reason})
    report.update(planning_status='INCOMPLETE', requirements=[rows[key] for key in sorted(rows)],
                  profiles=[profiles[key] for key in sorted(profiles)], requirements_without_declared_impact=[],
                  selected_test_selectors=sorted({s for row in rows.values() for s in row['test_selectors']}))
    return report
