"""Version evidence detects changes without traversing private runtime data."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from qa.execution import provenance as p
from qa.requirements.catalog import canonical_digest


def write(root, name, content='sample\n'):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8', newline='\n')
    return path


@pytest.fixture
def project(tmp_path):
    groups = (
        ['.gitignore'], ['src/service.py'], ['qa/check.py'],
        sorted(p.JUDGE_FIXTURES), ['README.md', *p.MANIFESTS],
    )
    for names in groups:
        for name in names:
            write(tmp_path, name)
    for name, names in zip(p.MANIFESTS, groups):
        write(tmp_path, name, ''.join(value + '\n' for value in names))
    return tmp_path


def test_snapshot_stable_and_pins_each_file_as_raw_bytes(project):
    before = p.snapshot(project)
    assert before == p.snapshot(project)
    assert set(p.JUDGE_FIXTURES) <= before['files'].keys()
    assert before['files']['src/service.py'] == sha256((project / 'src/service.py').read_bytes()).hexdigest()
    assert before['digest'] == canonical_digest({key: before[key] for key in ('schema_version', 'files', 'scope')})
    (project / 'src/service.py').write_bytes(b'sample\r\n')
    result = p.compare_snapshots(before, p.snapshot(project))
    assert result == {'status': 'ERROR', 'added': [], 'removed': [], 'changed': ['src/service.py']}


@pytest.mark.parametrize('name', [
    'src/new_module.py', 'qa/execution/runner.py', 'tests/new/test_added.py',
    'scripts/new/Run.ps1', 'config/extra.json', 'contracts/v2/example.yaml',
    'data/demo-v1/extra.json', 'data/synthetic/extra.json', 'lab/sql/new.sql',
    'lab/api/static/new.js', 'prompts/new.md', 'requirements/new.lock', '.github/workflows/new.yml',
])
def test_unlisted_source_additions_and_removals_are_detected(project, name):
    before = p.snapshot(project)
    target = write(project, name)
    after = p.snapshot(project)
    assert p.compare_snapshots(before, after) == {
        'status': 'ERROR', 'added': [name], 'removed': [], 'changed': [],
    }
    target.unlink()
    assert p.compare_snapshots(after, p.snapshot(project)) == {
        'status': 'ERROR', 'added': [], 'removed': [name], 'changed': [],
    }


@pytest.mark.parametrize('name', [
    '.env', '.env.local', 'var/qa/execution/example/result.json',
    'data/chroma/db.sqlite3', '.venv/site.py', '.rebuild/connection.json',
    'verification/other-run/result.json', 'qa/progress.json',
    'qa/PROGRESS.json', 'src/__pycache__/cached.py', 'qa/.pytest_cache/state.json',
    'qa/.venv-private/payload.py', 'lab/desktop/local.lnk',
])
def test_private_runtime_additions_are_not_traversed_or_hashed(project, name, monkeypatch):
    before = p.snapshot(project)
    target = write(project, name, 'private-value')
    real_identity = p._identity

    def guarded_identity(path):
        assert path != target
        return real_identity(path)

    monkeypatch.setattr(p, '_identity', guarded_identity)
    assert p.snapshot(project) == before


def test_missing_manifest_and_declared_source_fail_closed(project):
    (project / 'src/service.py').unlink()
    with pytest.raises(p.ProvenanceError, match='PROVENANCE_SOURCE_MISSING'):
        p.snapshot(project)
    (project / p.MANIFESTS[0]).unlink()
    with pytest.raises(p.ProvenanceError, match='PROVENANCE_MANIFEST_MISSING'):
        p.snapshot(project)


@pytest.mark.parametrize('line', [
    '../outside.py', 'var/output.json', 'qa/progress.json', '.env',
    'src/../private.json', 'C:/outside.py', 'src/file.py:stream',
])
def test_manifest_cannot_expand_into_runtime_or_unsafe_paths(project, line):
    write(project, p.MANIFESTS[2], line + '\n')
    with pytest.raises(p.ProvenanceError, match='PROVENANCE_MANIFEST_PATH_FORBIDDEN'):
        p.snapshot(project)


def test_manifest_duplicates_and_unapproved_judge_fixture_rejected(project):
    write(project, p.MANIFESTS[2], 'qa/check.py\nQA/check.py\n')
    with pytest.raises(p.ProvenanceError, match='PROVENANCE_MANIFEST_PATH_FORBIDDEN|PROVENANCE_DUPLICATE_PATH'):
        p.snapshot(project)
    write(project, p.MANIFESTS[2], 'qa/check.py\n')
    write(project, p.MANIFESTS[3], p.PILOT + 'arbitrary.json\n')
    with pytest.raises(p.ProvenanceError, match='PROVENANCE_JUDGE_FIXTURES_INVALID'):
        p.snapshot(project)


def test_manifest_cannot_change_between_membership_and_hashing(project, monkeypatch):
    original = p._publication_files

    def racing_manifest(root):
        result = original(root)
        with (root / p.MANIFESTS[4]).open('a', encoding='utf-8') as stream:
            stream.write('docs/new.md\n')
        write(root, 'docs/new.md')
        return result

    monkeypatch.setattr(p, '_publication_files', racing_manifest)
    with pytest.raises(p.ProvenanceError, match='PROVENANCE_MANIFEST_CHANGED'):
        p.snapshot(project)


def test_hardlinked_source_is_rejected(project):
    original = project / 'src/service.py'
    os.link(original, project / 'src/alias.py')
    with pytest.raises(p.ProvenanceError, match='PROVENANCE_UNSAFE_PATH'):
        p.snapshot(project)


def test_discovered_link_is_rejected_before_content_access(project, monkeypatch):
    link = write(project, 'src/link.py')
    original = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda path: path == link or original(path))
    with pytest.raises(p.ProvenanceError, match='PROVENANCE_UNSAFE_PATH'):
        p.snapshot(project)


def test_source_tree_must_be_a_directory(project):
    write(project, 'prompts', 'wrong-kind')
    with pytest.raises(p.ProvenanceError, match='PROVENANCE_SOURCE_TREE_INVALID'):
        p.snapshot(project)


def test_snapshot_digests_are_validated_before_comparison(project):
    snapshot = p.snapshot(project)
    changed = deepcopy(snapshot)
    changed['files']['src/service.py'] = '0' * 64
    with pytest.raises(p.ProvenanceError, match='PROVENANCE_SNAPSHOT_DIGEST_MISMATCH'):
        p.compare_snapshots(snapshot, changed)


def test_ambiguous_case_aliases_in_imported_snapshot_are_rejected(project):
    snapshot = p.snapshot(project)
    changed = deepcopy(snapshot)
    changed['files']['SRC/service.py'] = changed['files']['src/service.py']
    changed['digest'] = canonical_digest({key: changed[key] for key in ('schema_version', 'files', 'scope')})
    with pytest.raises(p.ProvenanceError, match='PROVENANCE_SNAPSHOT_INVALID'):
        p.compare_snapshots(snapshot, changed)


def test_evidence_inventory_pins_sorted_files_and_changes(project):
    directory = project / 'var/qa/execution/run-01'
    one = write(project, 'var/qa/execution/run-01/one.json', '{}\n')
    two = write(project, 'var/qa/execution/run-01/nested/two.log', 'complete\n')
    rows = p.evidence_inventory(project, directory)
    assert rows == sorted([
        {'path': path.relative_to(project).as_posix(), 'sha256': sha256(path.read_bytes()).hexdigest(),
         'size': path.stat().st_size} for path in (one, two)
    ], key=lambda row: row['path'])
    two.write_text('changed\n', encoding='utf-8')
    assert rows != p.evidence_inventory(project, directory)


@pytest.mark.parametrize('path', ['.', '../outside', 'var/qa/../result', 'missing'])
def test_evidence_directory_scope_rejected(project, path):
    with pytest.raises(p.ProvenanceError):
        p.evidence_inventory(project, path)


def test_evidence_hardlink_rejected(project):
    source = write(project, 'var/qa/execution/check/source.json')
    os.link(source, source.parent / 'alias.json')
    with pytest.raises(p.ProvenanceError, match='PROVENANCE_UNSAFE_PATH'):
        p.evidence_inventory(project, source.parent)


@pytest.mark.parametrize('limit,expected', [
    ('MAX_FILE_SIZE', 'PROVENANCE_FILE_TOO_LARGE'),
    ('MAX_TOTAL_SIZE', 'PROVENANCE_TOTAL_TOO_LARGE'),
    ('MAX_FILES', 'PROVENANCE_TOO_MANY_FILES'),
])
def test_evidence_limits_fail_closed(project, monkeypatch, limit, expected):
    write(project, 'var/qa/execution/run/a.json', '1234')
    write(project, 'var/qa/execution/run/b.json', '1234')
    monkeypatch.setattr(p, limit, 1 if limit != 'MAX_TOTAL_SIZE' else 5)
    with pytest.raises(p.ProvenanceError, match=expected):
        p.evidence_inventory(project, 'var/qa/execution/run')


def distribution(name, version):
    return SimpleNamespace(metadata={'Name': name}, version=version)


def test_environment_is_stable_metadata_without_environment_secrets(tmp_path, monkeypatch):
    interpreter = write(tmp_path, 'python.exe', 'synthetic-interpreter')
    monkeypatch.setattr(p.sys, 'executable', str(interpreter))
    monkeypatch.setenv('API_KEY', 'never-store-this')
    monkeypatch.setenv('DATABASE_PASSWORD', 'also-private')
    monkeypatch.setattr(p.metadata, 'distributions', lambda: [distribution('Zed_Package', '1.2.3'), distribution('aaa', '2.0+local')])
    result = p.environment()
    assert result == p.environment()
    assert result['distributions'] == [{'name': 'aaa', 'version': '2.0+local'}, {'name': 'zed-package', 'version': '1.2.3'}]
    assert result['python']['executable_sha256'] == sha256(interpreter.read_bytes()).hexdigest()
    assert result['digest'] == canonical_digest({key: value for key, value in result.items() if key != 'digest'})
    text = json.dumps(result)
    assert all(value not in text for value in ('API_KEY', 'DATABASE_PASSWORD', 'never-store-this', 'also-private'))


@pytest.mark.parametrize('packages,error', [
    ([distribution('Same_Package', '1'), distribution('same-package', '1')], 'PROVENANCE_PACKAGE_DUPLICATE'),
    ([distribution(None, '1')], 'PROVENANCE_PACKAGE_METADATA_INVALID'),
    ([distribution('valid', '1\nsecret')], 'PROVENANCE_PACKAGE_METADATA_INVALID'),
])
def test_environment_rejects_ambiguous_or_invalid_package_metadata(tmp_path, monkeypatch, packages, error):
    interpreter = write(tmp_path, 'python.exe')
    monkeypatch.setattr(p.sys, 'executable', str(interpreter))
    monkeypatch.setattr(p.metadata, 'distributions', lambda: packages)
    with pytest.raises(p.ProvenanceError, match=error):
        p.environment()
