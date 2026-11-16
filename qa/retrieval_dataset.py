"""Validate immutable retrieval labels without importing the product or a model."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FOLDER = ROOT / 'qa/datasets/retrieval-v1'
MANIFEST_SHA256 = 'bfc8dcd1be7a4c6d4c3c2f3b55472224e2b749a4ae98e1facbcb2dfce8d78ac7'
_FILES = {'README.md', 'build_dataset.py', 'cases.json', 'corpora/base.json', 'corpora/editions.json'}
_TARGETS = {'structuring': 'R-115-002', 'threshold': 'R-115-006', 'kyc': 'R-115-007',
            'high_risk': 'R-AML-010', 'reporting': 'R-AML-015'}
_ALIASES = {'threshold_breach': 'threshold', 'kyc_mismatch': 'kyc', 'high_risk_country': 'high_risk'}


class DatasetError(ValueError):
    """Safe fixed diagnostic code; source contents are never included."""


def _require(condition, code):
    if not condition:
        raise DatasetError(code)


def _json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            _require(key not in result, 'DUPLICATE_JSON_KEY')
            result[key] = value
        return result

    def reject(_):
        raise DatasetError('INVALID_JSON_NUMBER')

    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                      parse_float=reject, parse_constant=reject)


def _read(folder, relative):
    _require(type(relative) is str and '\\' not in relative, 'INVALID_DATASET_PATH')
    pieces = PurePosixPath(relative)
    _require(not pieces.is_absolute() and pieces.parts and
             all(part not in {'.', '..'} and ':' not in part for part in pieces.parts), 'INVALID_DATASET_PATH')
    path = folder
    for piece in pieces.parts:
        path = path / piece
        _require(not path.is_symlink(), 'SYMLINK_NOT_ALLOWED')
    _require(path.is_file() and path.stat().st_size <= 1024 * 1024, 'INVALID_DATASET_FILE')
    _require(path.resolve().is_relative_to(folder.resolve()), 'INVALID_DATASET_PATH')
    return path.read_bytes()


def _utc_day(value):
    _require(type(value) is str and re.fullmatch(
        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})', value), 'INVALID_AS_OF')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed.astimezone(timezone.utc).date().isoformat()


def _citation(row):
    digest = sha256(row['text'].encode('utf-8')).hexdigest()
    identity = {'rule_id': row['rule_id'], 'version': row['version'],
                'source_ref': row['regulation_ref'], 'char_start': 0,
                'char_end': len(row['text']), 'text_sha256': digest}
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode('utf-8')
    return {'rule_id': row['rule_id'], 'rule_version': row['version'],
            'source_ref': row['regulation_ref'], 'chunk_id': 'chunk-' + sha256(raw).hexdigest(),
            'content_sha256': digest}


def _inventory(records):
    _require(type(records) is list and records, 'INVALID_CORPUS')
    result, keys, intervals = [], set(), {}
    for row in records:
        _require(type(row) is dict and {'rule_id', 'version', 'regulation_ref', 'text',
                 'effective_from', 'category', 'title'} <= set(row) and
                 not set(row) - {'rule_id', 'version', 'regulation_ref', 'text',
                 'effective_from', 'category', 'title', 'effective_until'}, 'INVALID_CORPUS')
        _require(all(type(row[key]) is str and row[key].strip() for key in
                     ['rule_id', 'version', 'regulation_ref', 'text', 'effective_from', 'category', 'title']), 'INVALID_CORPUS')
        _require(row['category'] in _TARGETS and row['rule_id'] == _TARGETS[row['category']], 'INVALID_CORPUS')
        identity = row['rule_id'], row['version']
        _require(identity not in keys, 'DUPLICATE_RULE_EDITION')
        keys.add(identity)
        start, end = row['effective_from'], row.get('effective_until')
        _require(date.fromisoformat(start).isoformat() == start, 'INVALID_EDITION_DATE')
        if end is not None:
            _require(type(end) is str and date.fromisoformat(end).isoformat() == end and start < end,
                     'INVALID_EDITION_DATE')
        for existing_start, existing_end in intervals.get(row['rule_id'], []):
            _require((end is not None and end <= existing_start) or
                     (existing_end is not None and existing_end <= start), 'OVERLAPPING_EDITIONS')
        intervals.setdefault(row['rule_id'], []).append((start, end))
        cite = _citation(row)
        result.append({'citation': cite, 'char_start': 0, 'char_end': len(row['text']),
                       'source_text_sha256': cite['content_sha256'],
                       'effective_from': start, 'effective_until': end})
    return sorted(result, key=lambda item: item['citation']['chunk_id'])


def _validate_catalog(catalog, folder):
    _require(type(catalog) is dict and catalog.get('schema_version') == '1.0' and
             catalog.get('dataset_version') == '1.0.0', 'INVALID_CATALOG_VERSION')
    _require(catalog.get('purpose') == 'synthetic_learning_only' and catalog.get('expert_approved') is False,
             'SYNTHETIC_SCOPE_REQUIRED')
    _require(catalog.get('k_values') == [1, 3] and all(type(k) is int for k in catalog['k_values']) and
             catalog.get('metric_mode') == 'descriptive_no_threshold', 'INVALID_METRIC_CONTRACT')
    queries = catalog['query_text']
    _require(type(queries) is dict and set(queries) == set(_TARGETS) | set(_ALIASES) | {'velocity'} and
             all(type(text) is str and text.strip() for text in queries.values()), 'INVALID_QUERY_MAP')
    _require(all(queries[alias] == queries[canonical] for alias, canonical in _ALIASES.items()), 'ALIAS_QUERY_MISMATCH')
    _require(len({queries[key] for key in _TARGETS}) == 5, 'PRIMARY_QUERIES_NOT_DISTINCT')
    parent = catalog['parent_manifest']
    _require(parent['path'] == 'qa/datasets/aml-v1/manifest.json' and
             sha256(_read(ROOT, parent['path'])).hexdigest() == parent['sha256'], 'PARENT_MANIFEST_CHANGED')
    query_source = catalog['query_source']
    _require(query_source['path'] == 'src/runtime/retrieval.py' and
             re.fullmatch('[a-f0-9]{64}', query_source['sha256']), 'INVALID_QUERY_PROVENANCE')
    corpora = catalog['corpora']
    _require(type(corpora) is dict and set(corpora) == {'base', 'editions'}, 'INVALID_CORPORA')
    for name, fixture in [('base', 'S01'), ('editions', 'S04')]:
        corpus = corpora[name]
        _require(corpus['path'] == f'corpora/{name}.json' and
                 corpus['source_path'] == f'qa/datasets/aml-v1/fixtures/{fixture}/rules.json', 'INVALID_CORPUS_PROVENANCE')
        raw, original = _read(folder, corpus['path']), _read(ROOT, corpus['source_path'])
        _require(raw == original and sha256(raw).hexdigest() == corpus['source_sha256'], 'SOURCE_CORPUS_CHANGED')
        corpus['records'] = _json(raw)
        corpus['inventory'] = _inventory(corpus['records'])
        _require(len(corpus['inventory']) == (5 if name == 'base' else 6), 'INVALID_EDITION_COUNT')
    cases = catalog['cases']
    _require(type(cases) is list and len(cases) == 13 and
             [row.get('id') for row in cases] == [f'R{number:02d}' for number in range(1, 14)], 'INVALID_CASE_SET')
    _require(Counter(row['kind'] for row in cases) == Counter(primary=5, alias=3, edition=3,
             no_relevance=1, no_active=1), 'INVALID_CASE_KINDS')
    for case in cases:
        _require(type(case['primary']) is bool and case['primary'] == (case['kind'] == 'primary'), 'INVALID_PRIMARY_FLAG')
        _require(case['query'] in queries and case['corpus'] in corpora, 'INVALID_CASE_QUERY')
        _require(case['group'] == _ALIASES.get(case['query'], case['query']), 'INVALID_CASE_GROUP')
        day = _utc_day(case['as_of'])
        expected_active = [item['citation'] for item in corpora[case['corpus']]['inventory']
                           if item['effective_from'] <= day and
                           (item['effective_until'] is None or day < item['effective_until'])]
        _require(case['expected_active'] == expected_active, 'ACTIVE_INVENTORY_MISMATCH')
        expected_relevant = [cite for cite in expected_active if cite['rule_id'] == _TARGETS.get(case['group'])]
        _require(case['relevant'] == expected_relevant, 'RELEVANCE_LABEL_MISMATCH')
    _require([row['query'] for row in cases[:5]] == list(_TARGETS) and
             all(row['primary'] and row['corpus'] == 'base' and row['as_of'] == '2026-07-20T00:00:00Z'
                 for row in cases[:5]), 'INVALID_PRIMARY_COHORT')
    _require([row['query'] for row in cases[5:8]] == list(_ALIASES) and
             all(row['kind'] == 'alias' and row['corpus'] == 'base' and row['as_of'] == '2026-07-20T00:00:00Z'
                 for row in cases[5:8]), 'INVALID_ALIAS_COHORT')
    _require([row['as_of'] for row in cases[8:11]] == ['2026-07-19T23:59:59Z',
             '2026-07-20T00:00:00Z', '2026-07-20T03:00:00+03:00'] and
             all(row['kind'] == 'edition' and row['query'] == 'threshold' and row['corpus'] == 'editions'
                 for row in cases[8:11]), 'INVALID_EDITION_COHORT')
    _require(cases[11]['query'] == 'velocity' and cases[11]['kind'] == 'no_relevance' and
             cases[11]['corpus'] == 'base' and cases[11]['as_of'] == '2026-07-20T00:00:00Z' and
             cases[12]['query'] == 'threshold' and cases[12]['kind'] == 'no_active' and
             cases[12]['corpus'] == 'base' and cases[12]['as_of'] == '2023-12-31T23:59:59Z', 'INVALID_NEGATIVE_CONTROLS')


def load_dataset(folder=DEFAULT_FOLDER):
    """Read verified static labels; add corpus records/inventory and manifest hash.

    No model/provider imports, downloads, service calls or evidence writes occur.
    The caller separately compares the live product's query map/source digest.
    """
    try:
        folder = Path(folder)
        _require(folder.is_dir() and not folder.is_symlink(), 'INVALID_DATASET_FOLDER')
        manifest_raw = _read(folder, 'manifest.json')
        digest = sha256(manifest_raw).hexdigest()
        _require(digest == MANIFEST_SHA256, 'MANIFEST_CHANGED')
        manifest = _json(manifest_raw)
        _require(manifest['schema_version'] == '1.0' and manifest['dataset_version'] == '1.0.0', 'INVALID_MANIFEST')
        entries = manifest['files']
        _require(type(entries) is list and len(entries) == len(_FILES) and
                 {entry['path'] for entry in entries} == _FILES, 'INVALID_MANIFEST_FILES')
        for entry in entries:
            raw = _read(folder, entry['path'])
            _require(type(entry['bytes']) is int and len(raw) == entry['bytes'] and
                     sha256(raw).hexdigest() == entry['sha256'], 'DATASET_FILE_CHANGED')
        catalog = _json(_read(folder, 'cases.json'))
        _validate_catalog(catalog, folder)
        catalog['manifest_sha256'] = digest
        return catalog
    except DatasetError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise DatasetError('INVALID_DATASET') from None
