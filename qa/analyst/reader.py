"""Bounded, read-only QA evidence access. Stored verdicts are never release approval."""
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import re
import stat

from qa.execution.provenance import compare_snapshots, snapshot
from qa.reporting import strict_json
from src.qa_profile import checked_path

ROOT = Path(__file__).resolve().parents[2]
KINDS = ('execution', 'evaluator-qualification', 'environments', 'stability', 'autonomy')
STATUS = frozenset(('PASS', 'FAIL', 'ERROR', 'INCOMPLETE'))
SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
HEX = re.compile(r'[0-9a-f]{64}')
MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
MAX_RUNS = 500
MAX_PINS = 2000


class EvidenceError(ValueError):
    """Stable diagnostic code, without source payloads or absolute paths."""


def require(condition, code):
    if not condition:
        raise EvidenceError(code)


def path_for(root, name):
    require(type(name) is str and re.fullmatch(r'[A-Za-z0-9_./-]+', name)
            and all(part not in ('', '.', '..') for part in name.split('/')),
            'ANALYST_PATH_INVALID')
    try:
        return checked_path(Path(root) / name, Path(root))
    except (OSError, ValueError):
        raise EvidenceError('ANALYST_PATH_UNSAFE') from None


def run_path(root, kind, run_id):
    require(type(kind) is str and kind in KINDS and type(run_id) is str
            and SAFE_ID.fullmatch(run_id), 'ANALYST_RUN_INVALID')
    return path_for(root, f'var/qa/{kind}/{run_id}')


def list_runs(kind, root=ROOT):
    """Names only, reverse lexical order, not a claim about execution chronology."""
    require(type(kind) is str and kind in KINDS, 'ANALYST_KIND_INVALID')
    directory = path_for(root, 'var/qa/' + kind)
    if not directory.exists():
        return []
    require(directory.is_dir(), 'ANALYST_DIRECTORY_INVALID')
    rows = []
    for index, child in enumerate(directory.iterdir()):
        require(index < MAX_RUNS, 'ANALYST_TOO_MANY_RUNS')
        if SAFE_ID.fullmatch(child.name):
            safe = run_path(root, kind, child.name)
            if safe.is_dir():
                rows.append(child.name)
    return sorted(rows, reverse=True)


class Reader:
    def __init__(self, root):
        self.root, self.total, self.loaded = Path(root), 0, {}

    def raw(self, name):
        path = path_for(self.root, name)
        before = path.stat()
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, 'ANALYST_FILE_INVALID')
        require(before.st_size <= MAX_FILE and self.total + before.st_size <= MAX_TOTAL,
                'ANALYST_SIZE_LIMIT')
        with path.open('rb') as stream:
            raw = stream.read(MAX_FILE + 1)
        self.total += len(raw)
        after = path.stat()
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        require(len(raw) <= MAX_FILE and self.total <= MAX_TOTAL, 'ANALYST_SIZE_LIMIT')
        require(identity(before) == identity(after) and len(raw) == after.st_size,
                'ANALYST_FILE_CHANGED')
        pin = {'path': name, 'sha256': sha256(raw).hexdigest(), 'size': len(raw)}
        if name in self.loaded:
            require(self.loaded[name] == pin, 'ANALYST_FILE_CHANGED')
        self.loaded[name] = pin
        return raw

    def json(self, name):
        try:
            return strict_json(self.raw(name).decode('utf-8-sig'))
        except (UnicodeError, RecursionError, ValueError) as exc:
            if isinstance(exc, EvidenceError):
                raise
            raise EvidenceError('ANALYST_JSON_INVALID') from None


def _external_pin(name, kind, run_id):
    if kind == 'execution':
        prefix = 'var/qa/requirements/execution-' + sha256(run_id.encode()).hexdigest()[:32] + '/'
        return name.startswith(prefix)
    if kind == 'environments':
        return name.startswith(f'var/workflow/environment-models/{run_id}/')
    if kind == 'stability':
        return name == 'qa/datasets/aml-v1/manifest.json' or bool(re.fullmatch(
            r'(?:var/qa/llm/[A-Za-z0-9][A-Za-z0-9_-]{0,79}|'
            r'verification/qa-step-07-\d{4}-\d{2}-\d{2}/[A-Za-z0-9][A-Za-z0-9_-]{0,79})/'
            r'[A-Za-z0-9_./-]+', name))
    return False


def _pins(reader, prefix, kind, run_id):
    manifest = reader.json(prefix + 'evidence.json')
    require(type(manifest) is dict and type(manifest.get('files')) is list
            and 0 < len(manifest['files']) <= MAX_PINS, 'ANALYST_MANIFEST_INVALID')
    seen, rows = set(), []
    for row in manifest['files']:
        require(type(row) is dict and set(row) == {'path', 'sha256', 'size'}, 'ANALYST_PIN_INVALID')
        name = row['path']
        require(type(name) is str and name.casefold() not in seen
                and type(row['sha256']) is str and HEX.fullmatch(row['sha256'])
                and type(row['size']) is int and 0 <= row['size'] <= MAX_FILE, 'ANALYST_PIN_INVALID')
        path_for(reader.root, name)
        require(name.startswith(prefix) or _external_pin(name, kind, run_id), 'ANALYST_PIN_OUT_OF_SCOPE')
        seen.add(name.casefold())
        rows.append(row)
    require(sum(row['size'] for row in rows) <= MAX_TOTAL, 'ANALYST_SIZE_LIMIT')
    return rows


def _error_code(exc):
    return str(exc) if isinstance(exc, EvidenceError) else 'ANALYST_READ_ERROR'


def load_run(kind, run_id, *, root=ROOT, verify=False):
    """Return only selected presentation fields; no raw model output or log bodies.

    Verification compares saved bytes with the local manifest and the source
    snapshot with current files. It does not rerun the original evaluators,
    authenticate the report author, or measure current dependencies/models.
    """
    from qa.analyst.projection import project
    view = {'kind': kind, 'run_id': run_id, 'read_status': 'ERROR', 'recorded_status': 'UNAVAILABLE',
            'evidence_check': 'NOT_CHECKED', 'source_check': 'NOT_CHECKED', 'issues': [],
            'tables': {}, 'summary': {}, 'pins': [], 'changed_sources': [],
            'product_acceptance': 'INCOMPLETE', 'candidate_admission': 'NOT_EVALUATED',
            'checked_at_utc': None}
    try:
        directory = run_path(root, kind, run_id)
        require(directory.is_dir(), 'ANALYST_RUN_MISSING')
        prefix = directory.relative_to(root).as_posix() + '/'
        result_path = path_for(root, prefix + 'result.json')
        if not result_path.exists():
            view.update(read_status='INCOMPLETE', recorded_status='INCOMPLETE')
            view['issues'].append('FINAL_RESULT_MISSING')
            return view
        reader = Reader(root)
        result = reader.json(prefix + 'result.json')
        require(type(result) is dict and result.get('schema_version') == '1.0'
                and result.get('run_id') == run_id and type(result.get('status')) is str
                and result['status'] in STATUS, 'ANALYST_RESULT_INVALID')
        view['recorded_status'] = result['status']
        pins = _pins(reader, prefix, kind, run_id)
        pin_map = {row['path']: row for row in pins}

        def artifact(name, optional=False):
            path = prefix + name
            if optional and not path_for(root, path).exists():
                return None
            require(path in pin_map, 'ANALYST_DETAIL_NOT_PINNED')
            value = reader.json(path)
            require(reader.loaded[path] == pin_map[path], 'ANALYST_DETAIL_CHANGED')
            return value

        project(view, result, artifact)
        view['read_status'] = 'PASS'
        view['pins'] = [{'path': row['path'], 'size': row['size'], 'status': 'NOT_CHECKED'} for row in pins]
        if verify:
            view['checked_at_utc'] = datetime.now(timezone.utc).isoformat()
            failed = False
            for row, shown in zip(pins, view['pins']):
                try:
                    reader.raw(row['path'])
                    shown['status'] = 'MATCH' if reader.loaded[row['path']] == row else 'MISMATCH'
                except (OSError, ValueError):
                    shown['status'] = 'UNREADABLE'
                failed |= shown['status'] != 'MATCH'
            view['evidence_check'] = 'ERROR' if failed else 'MATCH'
            if failed:
                view['issues'].append('EVIDENCE_CHANGED_OR_MISSING')
                view['source_check'] = 'UNAVAILABLE'
            else:
                saved = artifact('source-before.json')
                require(result.get('source_digest') == saved.get('digest'), 'ANALYST_SOURCE_IDENTITY_MISMATCH')
                difference = compare_snapshots(saved, snapshot(root))
                view['source_check'] = 'MATCH' if difference['status'] == 'PASS' else 'DIFFERENT'
                view['changed_sources'] = [{'change': key, 'path': name}
                    for key in ('added', 'removed', 'changed') for name in difference[key]]
            # Check control records again after the potentially longer scan.
            for name in (prefix + 'result.json', prefix + 'evidence.json'):
                reader.raw(name)
        view['view_fingerprint'] = sha256(str(sorted(reader.loaded.items())).encode()).hexdigest()
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        view['read_status'] = 'ERROR'
        view['issues'].append(_error_code(exc))
        # Malformed detail must never leave a half-filled, apparently validated panel.
        view['tables'], view['summary'] = {}, {}
        if verify:
            view['evidence_check'], view['source_check'] = 'ERROR', 'UNAVAILABLE'
    return view
