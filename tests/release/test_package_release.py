"""Source-release integrity/boundary checks: no services, network or real state."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/release/package_release.py"
SPEC = importlib.util.spec_from_file_location("package_release_test", SCRIPT)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    for name in release.REQUIRED_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("reviewed source\n", encoding="utf-8")
    (root / release.VERSION_FILE).write_text(json.dumps({
        "version": "0.1.0-local.1", "kind": "source",
        "automatic_deployment": False, "database_migration": False,
    }), encoding="utf-8")
    return root


def add_file(root, name, value="secret must never ship"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def checksum(path):
    path.with_suffix(".zip.sha256").write_text(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n", encoding="ascii")


def test_kafka_source_release_cannot_omit_compose_or_schema(source, tmp_path):
    add_file(source, 'src/lab/kafka/api.py', '# optional module\n')
    with pytest.raises(release.ReleaseError, match='Missing required source files'):
        release.build(source, tmp_path / 'out')


def test_complete_kafka_module_is_packaged_and_verified(source, tmp_path):
    for name in release.KAFKA_REQUIRED_FILES:
        add_file(source, name, '# optional module\n')
    result = release.build(source, tmp_path / 'out')
    archive = Path(result['archive'])
    with zipfile.ZipFile(archive) as opened:
        assert release.KAFKA_REQUIRED_FILES.issubset(opened.namelist())
    assert release.verify(archive)['status'] == 'PASS'


def altered_archive(path, mutate):
    with zipfile.ZipFile(path) as source:
        entries = [(info, source.read(info.filename)) for info in source.infolist()]
    entries = mutate(entries)
    with zipfile.ZipFile(path, "w") as archive:
        for info, data in entries:
            archive.writestr(info, data)
    checksum(path)


def test_release_excludes_state_credentials_dependencies_and_old_evidence(source, tmp_path):
    excluded = ["var/postgres/lab-connection.json", "var/workflow/workflow.sqlite3",
                "var/models/weights.py", ".venv/runtime.py", ".git/config", ".env",
                ".env.production", "verification/block-06/README.md", "reports/report.md",
                ".rebuild/progress.json", "data/private.json", "config/local-secrets.json",
                "src/__pycache__/cache.py", "src/.env", "src/key.pem", "lab/api/token.json"]
    for name in excluded:
        add_file(source, name)
    add_file(source, "src/runtime/example.py", "# runtime source\n")
    add_file(source, "tests/release/test_example.py", "# package test\n")
    add_file(source, ".env.example", "# documented example\n")
    result = release.build(source, tmp_path / "output")
    with zipfile.ZipFile(result["archive"]) as archive:
        names = set(archive.namelist())
        assert not names.intersection(excluded)
        assert {"src/runtime/example.py", "tests/release/test_example.py", ".env.example"} <= names
        manifest = json.loads(archive.read(release.MANIFEST))
        assert len(manifest["files"]) == result["files"]
        assert not manifest["runtime_included"]
    assert result["status"] == "PASS"
    assert not result["automatic_deployment"]


def test_same_sources_build_identical_bytes_and_existing_output_is_preserved(source, tmp_path):
    first = release.build(source, tmp_path / "first")
    second = release.build(source, tmp_path / "second")
    assert first["sha256"] == second["sha256"]
    original = Path(first["archive"]).read_bytes()
    with pytest.raises(release.ReleaseError, match="already exists"):
        release.build(source, tmp_path / "first")
    assert Path(first["archive"]).read_bytes() == original


def test_missing_required_source_is_rejected_before_output_creation(source, tmp_path):
    (source / "src/ui/app.py").unlink()
    with pytest.raises(release.ReleaseError, match="Missing required"):
        release.build(source, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_checksum_detects_changed_zip_bytes(source, tmp_path):
    archive = Path(release.build(source, tmp_path / "output")["archive"])
    with archive.open("ab") as stream:
        stream.write(b"corruption")
    with pytest.raises(release.ReleaseError, match="Archive SHA256 mismatch"):
        release.verify(archive)


def test_manifest_detects_changed_source_even_with_updated_zip_checksum(source, tmp_path):
    archive = Path(release.build(source, tmp_path / "output")["archive"])
    altered_archive(archive, lambda entries: [(info, b"changed" if info.filename == "src/ui/app.py" else data)
                                             for info, data in entries])
    with pytest.raises(release.ReleaseError, match="File integrity mismatch"):
        release.verify(archive)


@pytest.mark.parametrize("name", [
    "../escape.py", "/src/escape.py", "C:/src/escape.py", "src\\escape.py",
    "src//escape.py", "src/./escape.py", "src/escape.py:secret", "src/NUL.py",
    "src/trailing./escape.py", "src/unsafe?.py", "var/postgres/password.json",
    "verification/README.md", "config/credentials.json", "src/__pycache__/escape.py",
])
def test_unsafe_or_unlisted_archive_paths_rejected(source, tmp_path, name):
    archive = Path(release.build(source, tmp_path / "output")["archive"])
    unsafe = zipfile.ZipInfo("placeholder")
    # ZipInfo's constructor normalizes backslashes on Windows. Set the raw ZIP
    # member name afterwards to exercise an archive produced on another system.
    unsafe.filename = name
    altered_archive(archive, lambda entries: entries + [(unsafe, b"malicious")])
    with pytest.raises(release.ReleaseError, match="Unsafe, duplicate, or unlisted"):
        release.verify(archive)


def test_case_collisions_rejected_for_windows(source, tmp_path):
    archive = Path(release.build(source, tmp_path / "output")["archive"])
    altered_archive(archive, lambda entries: entries + [(zipfile.ZipInfo("src/ui/App.py"), b"duplicate")])
    with pytest.raises(release.ReleaseError, match="duplicate"):
        release.verify(archive)


def test_symlink_member_is_rejected(source, tmp_path):
    archive = Path(release.build(source, tmp_path / "output")["archive"])
    link = zipfile.ZipInfo("src/linked.py")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    altered_archive(archive, lambda entries: entries + [(link, b"../../private")])
    with pytest.raises(release.ReleaseError, match="Non-regular"):
        release.verify(archive)


def test_manifest_must_cover_exactly_the_payload(source, tmp_path):
    archive = Path(release.build(source, tmp_path / "output")["archive"])
    altered_archive(archive, lambda entries: entries + [(zipfile.ZipInfo("src/new.py"), b"unlisted source")])
    with pytest.raises(release.ReleaseError, match="does not match"):
        release.verify(archive)


def test_source_reparse_points_are_rejected_without_following_target(source, tmp_path, monkeypatch):
    path = source / "src/ui/app.py"
    original = Path.lstat

    class LinkedStat:
        st_file_attributes = 0x400
        st_mode = stat.S_IFREG | 0o644

    monkeypatch.setattr(Path, "lstat", lambda candidate: LinkedStat() if candidate == path else original(candidate))
    with pytest.raises(release.ReleaseError, match="Links/reparse"):
        release.build(source, tmp_path / "output")


def test_wrong_release_metadata_is_rejected(source, tmp_path):
    add_file(source, release.VERSION_FILE, json.dumps({
        "version": "../escape", "kind": "source", "automatic_deployment": False,
        "database_migration": False,
    }))
    with pytest.raises(release.ReleaseError, match="Invalid source-release"):
        release.build(source, tmp_path / "output")


def test_missing_checksum_sidecar_is_rejected(source, tmp_path):
    archive = Path(release.build(source, tmp_path / "output")["archive"])
    archive.with_suffix(".zip.sha256").unlink()
    with pytest.raises(release.ReleaseError, match="sidecar"):
        release.verify(archive)


def test_required_file_removed_from_both_manifest_and_payload_is_rejected(source, tmp_path):
    archive = Path(release.build(source, tmp_path / "output")["archive"])

    def remove_required(entries):
        changed = []
        for info, data in entries:
            if info.filename == "src/ui/app.py":
                continue
            if info.filename == release.MANIFEST:
                manifest = json.loads(data)
                manifest["files"] = [row for row in manifest["files"] if row["path"] != "src/ui/app.py"]
                data = json.dumps(manifest).encode()
            changed.append((info, data))
        return changed

    altered_archive(archive, remove_required)
    with pytest.raises(release.ReleaseError, match="missing required"):
        release.verify(archive)
