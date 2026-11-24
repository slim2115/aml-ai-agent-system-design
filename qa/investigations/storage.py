"""Bounded read-only access to fixed investigation packs, never arbitrary reports."""
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from qa.analyst.reader import Reader, SAFE_ID, HEX, MAX_FILE, path_for
from qa.execution.provenance import compare_snapshots, snapshot
from qa.investigations.core import LearningError, load_catalog, validate_catalog
from qa.requirements.catalog import ROOT, canonical_digest

FILES = ('catalog.json', 'observations.json', 'bundle.json', 'qualification.json',
         'source-before.json', 'source-after.json', 'environment-before.json',
         'environment-after.json', 'fixtures/checksum-control.json', 'result.json')


def require(condition, code):
    if not condition:
        raise LearningError(code)


def pack_path(run_id, root=ROOT):
    require(type(run_id) is str and SAFE_ID.fullmatch(run_id), 'LEARNING_RUN_ID')
    return path_for(root, 'var/qa/investigations/' + run_id)


def list_packs(root=ROOT):
    folder = path_for(root, 'var/qa/investigations')
    if not folder.exists():
        return []
    result = []
    for index, entry in enumerate(folder.iterdir()):
        require(index < 500, 'LEARNING_TOO_MANY_PACKS')
        if SAFE_ID.fullmatch(entry.name) and pack_path(entry.name, root).is_dir():
            result.append(entry.name)
    return sorted(result, reverse=True)


def load_pack(run_id, root=ROOT):
    """Recheck bytes, derived facts and current source before permitting assessment."""
    from qa.investigations.observations import project, qualify
    root = Path(root)
    prefix = pack_path(run_id, root).relative_to(root).as_posix() + '/'
    reader = Reader(root)
    manifest = reader.json(prefix + 'evidence.json')
    require(type(manifest) is dict and set(manifest) == {'files'}, 'LEARNING_MANIFEST')
    pins = manifest['files']
    require(type(pins) is list and len(pins) == len(FILES) + 1, 'LEARNING_PIN_COUNT')
    expected = {prefix + name for name in FILES}
    expected.add(f'var/workflow/investigations/{run_id}/http-ack-replay/workflow.sqlite3')
    seen = set()
    for pin in pins:
        require(type(pin) is dict and set(pin) == {'path', 'sha256', 'size'}, 'LEARNING_PIN')
        name = pin['path']
        require(type(name) is str and name in expected and name not in seen, 'LEARNING_PIN_SCOPE')
        require(type(pin['sha256']) is str and HEX.fullmatch(pin['sha256'])
                and type(pin['size']) is int and 0 <= pin['size'] <= MAX_FILE, 'LEARNING_PIN')
        reader.raw(name)
        require(reader.loaded[name] == pin, 'LEARNING_EVIDENCE_CHANGED')
        seen.add(name)
    require(seen == expected, 'LEARNING_PIN_MISSING')
    values = {name: reader.json(prefix + name) for name in FILES if name.endswith('.json')}
    catalog = validate_catalog(values['catalog.json'])
    require(canonical_digest(catalog) == canonical_digest(load_catalog(root)), 'LEARNING_CATALOG_CHANGED')
    observations = values['observations.json']
    qualification = qualify(observations)
    require(canonical_digest(qualification) == canonical_digest(values['qualification.json'])
            and qualification['status'] == 'PASS', 'LEARNING_QUALIFICATION')
    fixture_name = prefix + 'fixtures/checksum-control.json'
    integrity = observations['integrity']
    require(integrity['before_pin']['path'] == fixture_name
            and canonical_digest(integrity['after_pin']) == canonical_digest(reader.loaded[fixture_name]),
            'LEARNING_FIXTURE_BINDING')
    bundle = project(observations)
    require(canonical_digest(bundle) == canonical_digest(values['bundle.json']), 'LEARNING_FACTS_CHANGED')
    result = values['result.json']
    require(type(result) is dict and set(result) == {'schema_version', 'run_id', 'status',
            'lesson_count', 'new_model_calls', 'source_digest', 'finished_at_utc',
            'operator_practice', 'product_acceptance'}
            and result.get('schema_version') == '1.0' and result.get('status') == 'PASS'
            and result.get('run_id') == run_id
            and type(result.get('lesson_count')) is int and result['lesson_count'] == 6
            and type(result.get('new_model_calls')) is int and result['new_model_calls'] == 0
            and type(result.get('source_digest')) is str and HEX.fullmatch(result['source_digest'])
            and type(result.get('finished_at_utc')) is str and len(result['finished_at_utc']) <= 40
            and result.get('operator_practice') == 'NOT_MEASURED'
            and result.get('product_acceptance') == 'INCOMPLETE', 'LEARNING_RESULT')
    try:
        finished = datetime.fromisoformat(result['finished_at_utc'])
    except ValueError:
        raise LearningError('LEARNING_RESULT_TIME') from None
    require(finished.utcoffset() is not None and finished.utcoffset().total_seconds() == 0,
            'LEARNING_RESULT_TIME')
    before, after = values['source-before.json'], values['source-after.json']
    require(compare_snapshots(before, after)['status'] == 'PASS', 'LEARNING_SOURCE_DURING_BUILD')
    require(result['source_digest'] == before['digest'], 'LEARNING_RESULT_SOURCE')
    require(values['environment-before.json'] == values['environment-after.json'], 'LEARNING_ENVIRONMENT_DURING_BUILD')
    current = snapshot(root)
    source_check = 'MATCH' if compare_snapshots(before, current)['status'] == 'PASS' else 'DIFFERENT'
    # Detect changes during this read, including replacement of the manifest itself.
    for name, pin in list(reader.loaded.items()):
        reader.raw(name)
        require(reader.loaded[name] == pin, 'LEARNING_EVIDENCE_CHANGED')
    return {'run_id': run_id, 'catalog': catalog, 'lessons': bundle,
            'integrity': 'MATCH', 'source_check': source_check,
            'assessment_enabled': source_check == 'MATCH',
            'fingerprint': sha256((canonical_digest(manifest) + current['digest']).encode()).hexdigest(),
            'operator_practice': 'NOT_MEASURED', 'product_acceptance': 'INCOMPLETE'}
