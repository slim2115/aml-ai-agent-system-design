"""Validate a versioned requirement catalogue without importing test modules."""
from __future__ import annotations

import ast
from hashlib import sha256
import json
from pathlib import Path
import re
import tomllib

from qa.reporting import checked, strict_json
from src.qa_profile import checked_path

ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = 'qa/requirements/catalog-v1.json'
POLICY_PATH = 'qa/strategy/quality-policy.json'
ROOT_KEYS = {'schema_version', 'catalog_id', 'version', 'title', 'scope', 'policy', 'requirements'}
ROW_KEYS = {'id', 'version', 'title', 'statement', 'risk', 'priority', 'owner', 'scope',
            'preconditions', 'allowed_outcomes', 'expectation_source', 'limitations', 'scenarios'}
SCENARIO_KEYS = {'id', 'kind', 'given', 'action', 'expected_outcome', 'test_selectors'}
SELECTOR = re.compile(r'tests/[A-Za-z0-9_/]+\.py::(?:[A-Za-z_]\w*::)?test_[A-Za-z0-9_]+')


class RequirementError(ValueError):
    """Invalid catalogue or source reference; messages are fixed error codes."""


def canonical_digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _require(condition, code):
    if not condition:
        raise RequirementError(code)


def _shape(value, keys, code):
    _require(type(value) is dict and set(value) == keys, code)


def _text(value):
    return type(value) is str and bool(value.strip()) and len(value) <= 4000


def _texts(value, code):
    _require(type(value) is list and 0 < len(value) <= 256 and all(_text(x) for x in value), code)
    _require(len(value) == len(set(value)), code)


def _file(root, name):
    try:
        path = checked_path(checked(root, name), root)
        _require(path.is_file(), 'SOURCE_NOT_FOUND')
        _require(path.stat().st_size <= 2 * 1024 * 1024, 'SOURCE_TOO_LARGE')
        return path
    except (OSError, ValueError, TypeError) as exc:
        if isinstance(exc, RequirementError):
            raise
        raise RequirementError('INVALID_SOURCE_PATH') from None


def load_catalog(root=ROOT):
    try:
        return strict_json(_file(Path(root), CATALOG_PATH).read_text('utf-8-sig'))
    except (OSError, ValueError, UnicodeError) as exc:
        if isinstance(exc, RequirementError):
            raise
        raise RequirementError('INVALID_CATALOG_JSON') from None


def selectors(catalog):
    return sorted({selector for row in catalog['requirements'] for case in row['scenarios']
                   for selector in case['test_selectors']})


def validate_catalog(catalog, root=ROOT):
    """Check definitions/references only. PASS is not execution or acceptance."""
    root = Path(root).absolute()
    _shape(catalog, ROOT_KEYS, 'INVALID_CATALOG_FIELDS')
    _require(catalog['schema_version'] == catalog['version'] == '1.0'
             and catalog['catalog_id'] == 'dark-factory-requirements-v1', 'UNSUPPORTED_CATALOG_VERSION')
    _require(_text(catalog['title']) and _text(catalog['scope']), 'INVALID_CATALOG_DESCRIPTION')
    _shape(catalog['policy'], {'path', 'canonical_sha256'}, 'INVALID_POLICY_REFERENCE')
    _require(catalog['policy']['path'] == POLICY_PATH, 'INVALID_POLICY_REFERENCE')
    sources = {}

    def source(name):
        path = _file(root, name)
        data = path.read_bytes()
        digest = sha256(data).hexdigest()
        _require(name not in sources or sources[name] == digest, 'SOURCE_CHANGED_DURING_VALIDATION')
        sources[name] = digest
        return data

    policy_bytes = source(POLICY_PATH)
    try:
        policy = strict_json(policy_bytes.decode('utf-8-sig'))
        _require(catalog['policy']['canonical_sha256'] == canonical_digest(policy), 'POLICY_CHANGED')
        config = tomllib.loads(source('pyproject.toml').decode('utf-8-sig'))
        testpaths = config['tool']['pytest']['ini_options']['testpaths']
        _require(type(testpaths) is list and testpaths and all(type(p) is str
                 and re.fullmatch(r'tests/[A-Za-z0-9_/]+', p) for p in testpaths), 'INVALID_TESTPATHS')
    except RequirementError:
        raise
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise RequirementError('INVALID_SOURCE_CONFIGURATION') from None
    rows = catalog['requirements']
    _require(type(rows) is list and 0 < len(rows) <= 128, 'INVALID_REQUIREMENTS')
    ids, case_ids, trees = set(), set(), {}
    for row in rows:
        _shape(row, ROW_KEYS, 'INVALID_REQUIREMENT_FIELDS')
        _require(type(row['id']) is str and re.fullmatch(r'DFR-\d{3}', row['id']) is not None,
                 'INVALID_REQUIREMENT_ID')
        _require(row['id'] not in ids, 'DUPLICATE_REQUIREMENT_ID')
        ids.add(row['id'])
        _require(row['version'] == '1.0', 'UNSUPPORTED_REQUIREMENT_VERSION')
        _require(all(_text(row[k]) for k in ('title', 'statement', 'risk', 'scope')),
                 'INVALID_REQUIREMENT_DESCRIPTION')
        _require(row['priority'] in ('P0', 'P1') and row['owner'] in ('qa_automation', 'model_evaluation'),
                 'INVALID_REQUIREMENT_CLASSIFICATION')
        for field in ('preconditions', 'allowed_outcomes', 'limitations'):
            _texts(row[field], 'INVALID_' + field.upper())
        _shape(row['expectation_source'], {'path', 'reference'}, 'INVALID_EXPECTATION_SOURCE')
        _require(_text(row['expectation_source']['reference']), 'INVALID_EXPECTATION_SOURCE')
        source(row['expectation_source']['path'])
        cases = row['scenarios']
        _require(type(cases) is list and 2 <= len(cases) <= 16, 'BOTH_SCENARIO_KINDS_REQUIRED')
        kinds, by_kind = set(), {'positive': set(), 'negative': set()}
        for case in cases:
            _shape(case, SCENARIO_KEYS, 'INVALID_SCENARIO_FIELDS')
            _require(type(case['id']) is str and re.fullmatch(re.escape(row['id']) + r'-[A-Z0-9-]+', case['id']),
                     'INVALID_SCENARIO_ID')
            _require(case['id'] not in case_ids, 'DUPLICATE_SCENARIO_ID')
            case_ids.add(case['id'])
            _require(case['kind'] in ('positive', 'negative'), 'INVALID_SCENARIO_KIND')
            kinds.add(case['kind'])
            _require(all(_text(case[k]) for k in ('given', 'action', 'expected_outcome')),
                     'INVALID_SCENARIO_DESCRIPTION')
            _require(case['expected_outcome'] in row['allowed_outcomes'], 'UNDECLARED_OUTCOME')
            _texts(case['test_selectors'], 'INVALID_TEST_SELECTORS')
            for selector in case['test_selectors']:
                _require(SELECTOR.fullmatch(selector) is not None, 'INVALID_TEST_SELECTOR')
                filename, *definitions = selector.split('::')
                _require(any(filename.startswith(p.rstrip('/') + '/') for p in testpaths),
                         'SELECTOR_OUTSIDE_CONFIGURED_SUITE')
                if filename not in trees:
                    try:
                        trees[filename] = ast.parse(source(filename).decode('utf-8-sig'))
                    except RequirementError:
                        raise
                    except (ValueError, SyntaxError, UnicodeError):
                        raise RequirementError('INVALID_TEST_SOURCE') from None
                node = trees[filename]
                for position, name in enumerate(definitions):
                    expected = (ast.FunctionDef, ast.AsyncFunctionDef) if position == len(definitions) - 1 else (ast.ClassDef,)
                    found = [child for child in node.body if isinstance(child, expected) and child.name == name]
                    _require(len(found) == 1, 'TEST_DEFINITION_NOT_FOUND')
                    node = found[0]
                by_kind[case['kind']].add(selector)
        _require(kinds == {'positive', 'negative'}, 'BOTH_SCENARIO_KINDS_REQUIRED')
        _require(not by_kind['positive'].intersection(by_kind['negative']), 'SAME_CHECK_FOR_BOTH_SCENARIO_KINDS')
    return {'status': 'PASS', 'catalog_sha256': canonical_digest(catalog),
            'source_hashes': dict(sorted(sources.items())), 'requirement_count': len(rows),
            'scenario_count': len(case_ids), 'selector_count': len(selectors(catalog)),
            'scope': 'Static structure, declared policy identity and AST references only; no tests executed.'}
