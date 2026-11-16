"""Content identities for owned QA runs, not signatures or machine attestation.

Snapshot published files and discover source/config additions in explicit trees.
Never traverse the runtime, model, database or historical-evidence trees.
"""
from hashlib import sha256
from importlib import metadata
from pathlib import Path
import platform
import re
import stat
import sys

from qa.requirements.catalog import canonical_digest
from scripts.release.package_release import DATA_FILES, EXACT_FILES, valid_path
from src.qa_profile import checked_path


MANIFESTS = tuple('docs/publication/' + name + '.txt' for name in (
    '01-git', '02-service', '03-qa', '03-judge-fixtures', '04-docs'))
PILOT = 'verification/qa-step-07-2026-10-20/pilot-first/'
JUDGE_FIXTURES = frozenset(PILOT + name for name in (
    'result.json', 'qualification.json', 'cases/QA-S01/repeat-01.json',
    'cases/QA-S03/repeat-01.json', 'cases/QA-S04/repeat-01.json'))
SOURCE_TREES = ('src', 'qa', 'tests', 'scripts', 'config', 'contracts',
                'data/demo-v1', 'data/synthetic', 'lab', 'prompts', 'requirements', '.github')
ROOT_FILES = frozenset({'.env.example', '.gitignore', '.gitattributes', 'README.md',
                       'pyproject.toml', 'requirements.txt', 'requirements-contracts.txt',
                       '.rebuild/origin.json', '.rebuild/release-version.json'})
EXTENSIONS = frozenset({'.py', '.pyi', '.json', '.jsonl', '.yaml', '.yml', '.toml',
    '.ini', '.cfg', '.conf', '.lock', '.in', '.sql', '.ps1', '.psm1', '.psd1',
    '.java', '.js', '.ts', '.css', '.html', '.svg', '.md', '.txt', '.http',
    '.bpmn', '.puml', '.png', '.bat', '.cmd', '.sh', '.xml', '.xsd'})
EXCLUDED_PARTS = frozenset({'__pycache__', '.pytest_cache', 'node_modules', '.git',
                          'var', 'cache', '.cache', '.mypy_cache', '.ruff_cache'})
MAX_FILE_SIZE = 32 * 1024 * 1024
MAX_TOTAL_SIZE = 512 * 1024 * 1024
MAX_FILES = 20000
SCOPE = ('Raw-byte identities of publication manifests and their files, plus discovered '
         'source/config assets in explicit trees. Runtime/cache, local progress, desktop '
         'shortcuts and unlisted historical evidence are excluded. No Git, installed-binary, '
         'model, operating-system or publisher attestation.')


class ProvenanceError(ValueError):
    """A bounded, safe code for invalid or incomplete local evidence."""


def _require(condition, code):
    if not condition:
        raise ProvenanceError(code)


def _root(value):
    root = Path(value).absolute()
    try:
        checked_path(root, root)
    except (ValueError, OSError):
        raise ProvenanceError('PROVENANCE_UNSAFE_ROOT') from None
    _require(root.is_dir(), 'PROVENANCE_ROOT_MISSING')
    return root


def _path(root, name):
    _require(valid_path(name), 'PROVENANCE_INVALID_PATH')
    try:
        return checked_path(root / name, root)
    except (ValueError, OSError):
        raise ProvenanceError('PROVENANCE_UNSAFE_PATH') from None


def _identity(path):
    """Bound both stat and streamed reads; reject mutation while hashing a file."""
    before = path.stat()
    _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, 'PROVENANCE_REGULAR_FILE_REQUIRED')
    _require(before.st_size <= MAX_FILE_SIZE, 'PROVENANCE_FILE_TOO_LARGE')
    digest, size = sha256(), 0
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            _require(size <= MAX_FILE_SIZE, 'PROVENANCE_FILE_TOO_LARGE')
            digest.update(chunk)
    after = path.stat()
    fields = lambda record: (record.st_dev, record.st_ino, record.st_size, record.st_mtime_ns,
                              record.st_ctime_ns, record.st_nlink)
    _require(fields(before) == fields(after) and size == after.st_size, 'PROVENANCE_FILE_CHANGED')
    return digest.hexdigest(), size


def _excluded(name):
    folded = name.casefold()
    parts = folded.split('/')
    return (folded == 'qa/progress.json'
            or any(part.casefold() in EXCLUDED_PARTS or part.casefold().startswith('.venv') for part in parts)
            or (folded.startswith('lab/desktop/') and Path(name).suffix.casefold() == '.lnk'))


def _manifest_allowed(name):
    if not valid_path(name) or _excluded(name):
        return False
    if name in EXACT_FILES or name in DATA_FILES or name in ROOT_FILES or name in JUDGE_FIXTURES:
        return True
    parts = name.split('/')
    return (parts[0] in {'src', 'qa', 'tests', 'scripts', 'docs', 'requirements', 'lab', 'prompts', 'config', 'contracts'}
            and all(not part.startswith('.') for part in parts)
            and Path(name).suffix.casefold() in EXTENSIONS)


def _publication_files(root):
    names, folded, manifest_hashes = set(MANIFESTS), set(), {}
    for manifest in MANIFESTS:
        path = _path(root, manifest)
        _require(path.is_file(), 'PROVENANCE_MANIFEST_MISSING')
        _require(path.stat().st_size <= 2 * 1024 * 1024, 'PROVENANCE_MANIFEST_TOO_LARGE')
        with path.open('rb') as stream:
            raw = stream.read(2 * 1024 * 1024 + 1)
        _require(len(raw) <= 2 * 1024 * 1024, 'PROVENANCE_MANIFEST_TOO_LARGE')
        manifest_hashes[manifest] = sha256(raw).hexdigest()
        rows = raw.decode('utf-8-sig').splitlines()
        _require(rows and all(row == row.strip() and row for row in rows), 'PROVENANCE_MANIFEST_INVALID')
        if manifest.endswith('/03-judge-fixtures.txt'):
            _require(set(rows) == JUDGE_FIXTURES, 'PROVENANCE_JUDGE_FIXTURES_INVALID')
        for name in rows:
            _require(_manifest_allowed(name), 'PROVENANCE_MANIFEST_PATH_FORBIDDEN')
            _require(name.casefold() not in folded, 'PROVENANCE_DUPLICATE_PATH')
            _require((name in JUDGE_FIXTURES) == manifest.endswith('/03-judge-fixtures.txt'),
                     'PROVENANCE_JUDGE_FIXTURES_INVALID')
            folded.add(name.casefold())
            names.add(name)
    return names, manifest_hashes


def _discover(root, directory, *, source_only):
    names, pending = [], [directory]
    while pending:
        current = pending.pop()
        for child in sorted(current.iterdir(), key=lambda item: item.name):
            name = child.relative_to(root).as_posix()
            if source_only and _excluded(name):
                continue
            checked = _path(root, name)
            if checked.is_dir():
                pending.append(checked)
            elif checked.is_file():
                if not source_only or checked.suffix.casefold() in EXTENSIONS or name in EXACT_FILES:
                    names.append(name)
                    _require(len(names) <= MAX_FILES, 'PROVENANCE_TOO_MANY_FILES')
            else:
                raise ProvenanceError('PROVENANCE_REGULAR_FILE_REQUIRED')
    return names


def snapshot(root):
    root = _root(root)
    names, manifest_hashes = _publication_files(root)
    names.update(name for name in ROOT_FILES if (root / name).exists())
    for tree in SOURCE_TREES:
        directory = _path(root, tree)
        if directory.exists():
            _require(directory.is_dir(), 'PROVENANCE_SOURCE_TREE_INVALID')
            names.update(_discover(root, directory, source_only=True))
    _require(len(names) <= MAX_FILES, 'PROVENANCE_TOO_MANY_FILES')
    _require(len({name.casefold() for name in names}) == len(names), 'PROVENANCE_DUPLICATE_PATH')
    files, total = {}, 0
    for name in sorted(names):
        path = _path(root, name)
        _require(path.is_file(), 'PROVENANCE_SOURCE_MISSING')
        digest, size = _identity(path)
        if name in manifest_hashes:
            _require(digest == manifest_hashes[name], 'PROVENANCE_MANIFEST_CHANGED')
        total += size
        _require(total <= MAX_TOTAL_SIZE, 'PROVENANCE_TOTAL_TOO_LARGE')
        files[name] = digest
    value = {'schema_version': '1.0', 'files': files, 'scope': SCOPE}
    return dict(value, digest=canonical_digest(value))


def _validated_snapshot(value):
    _require(type(value) is dict and set(value) == {'schema_version', 'files', 'scope', 'digest'},
             'PROVENANCE_SNAPSHOT_INVALID')
    _require(value['schema_version'] == '1.0' and value['scope'] == SCOPE
             and type(value['files']) is dict and 0 < len(value['files']) <= MAX_FILES,
             'PROVENANCE_SNAPSHOT_INVALID')
    _require(all(type(name) is str for name in value['files'])
             and len({name.casefold() for name in value['files']}) == len(value['files']),
             'PROVENANCE_SNAPSHOT_INVALID')
    for name, digest in value['files'].items():
        _require(valid_path(name) and type(digest) is str and re.fullmatch(r'[0-9a-f]{64}', digest),
                 'PROVENANCE_SNAPSHOT_INVALID')
    payload = {key: value[key] for key in ('schema_version', 'files', 'scope')}
    _require(value['digest'] == canonical_digest(payload), 'PROVENANCE_SNAPSHOT_DIGEST_MISMATCH')
    return value['files']


def compare_snapshots(before, after):
    left, right = _validated_snapshot(before), _validated_snapshot(after)
    added, removed = sorted(right.keys() - left.keys()), sorted(left.keys() - right.keys())
    changed = sorted(name for name in left.keys() & right.keys() if left[name] != right[name])
    return {'status': 'ERROR' if added or removed or changed else 'PASS',
            'added': added, 'removed': removed, 'changed': changed}


def environment():
    """Observe Python and distribution metadata; never capture environment variables."""
    executable = Path(sys.executable).absolute()
    _require(executable.is_file(), 'PROVENANCE_PYTHON_MISSING')
    executable_digest, _ = _identity(executable)
    packages, seen = [], set()
    for distribution in metadata.distributions():
        raw_name, version = distribution.metadata.get('Name'), distribution.version
        _require(type(raw_name) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', raw_name)
                 and type(version) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.!+_-]*', version),
                 'PROVENANCE_PACKAGE_METADATA_INVALID')
        name = re.sub(r'[-_.]+', '-', raw_name).lower()
        _require(name not in seen, 'PROVENANCE_PACKAGE_DUPLICATE')
        seen.add(name)
        packages.append({'name': name, 'version': version})
    value = {'schema_version': '1.0', 'python': {
        'executable': str(executable), 'executable_sha256': executable_digest,
        'version': platform.python_version(), 'implementation': platform.python_implementation()},
        'platform': {'system': platform.system(), 'release': platform.release(), 'machine': platform.machine()},
        'distributions': sorted(packages, key=lambda row: row['name']),
        'scope': 'Observed interpreter file and installed distribution metadata only; '
                 'no installed-package binary integrity, OS attestation or model identity.'}
    return dict(value, digest=canonical_digest(value))


def evidence_inventory(root, directory):
    root = _root(root)
    supplied = Path(directory)
    path = supplied.absolute() if supplied.is_absolute() else root / supplied
    try:
        path = checked_path(path, root)
    except (ValueError, OSError):
        raise ProvenanceError('PROVENANCE_UNSAFE_EVIDENCE_PATH') from None
    _require(path != root and path.is_dir(), 'PROVENANCE_EVIDENCE_DIRECTORY_REQUIRED')
    rows, total = [], 0
    for name in sorted(_discover(root, path, source_only=False)):
        digest, size = _identity(_path(root, name))
        total += size
        _require(total <= MAX_TOTAL_SIZE, 'PROVENANCE_TOTAL_TOO_LARGE')
        rows.append({'path': name, 'sha256': digest, 'size': size})
    return rows
