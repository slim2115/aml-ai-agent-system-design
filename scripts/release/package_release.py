"""Build/verify a deterministic source release; standard library only.

Never installs, extracts, starts services, or includes a runtime/database/model.
The SHA256 sidecar detects corruption; it is not a publisher signature.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = "release-manifest.json"
VERSION_FILE = ".rebuild/release-version.json"
VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9]+(?:[.-][a-z0-9]+)*)?\Z")
SHA256_PATTERN = re.compile(r"[a-f0-9]{64}\Z")

# Only reviewed source/documentation classes are eligible. Runtime and historic
# evidence directories are never walked. New asset classes require code review.
EXACT_FILES = frozenset({
    ".env.example", ".gitignore", "README.md", "pyproject.toml",
    "requirements.txt", "requirements-contracts.txt", ".rebuild/origin.json",
    VERSION_FILE, ".github/workflows/ci.yml", "tests/golden_dataset.json",
    "config/README.md", "config/demo-policy.json", "config/domain-policy-pending.json", "config/embedding-files.json",
    "contracts/v1/openapi.json", "contracts/v1/schema.json",
    "lab/README.md", "lab/api/README.md", "lab/api/exercises.http",
    "lab/api/static/LICENSE", "lab/api/static/favicon.svg",
    "lab/api/static/favicon-32x32.png", "lab/api/static/swagger-ui-bundle.js",
    "lab/api/static/swagger-ui.css",
})
SOURCE_TREES = {
    "src": frozenset({".py"}),
    "tests": frozenset({".py"}),
    "scripts": frozenset({".py", ".ps1"}),
    "docs": frozenset({".md", ".puml", ".bpmn"}),
    "prompts": frozenset({".md"}),
    "requirements": frozenset({".lock", ".in"}),
    "lab/sql": frozenset({".sql", ".md"}),
    "lab/kafka": frozenset({".sql", ".md", ".yaml"}),
}
DATA_FILES = frozenset({
    f"data/{snapshot}/{name}.json"
    for snapshot, names in {
        "demo-v1": ("cards", "cases", "clients", "manifest", "rules", "transactions"),
        "synthetic": ("cards", "cases", "clients", "rules", "transactions"),
    }.items()
    for name in names
})
REQUIRED_FILES = frozenset({
    VERSION_FILE, ".rebuild/origin.json", "pyproject.toml", ".github/workflows/ci.yml",
    "src/ui/app.py", "scripts/rebuild/Enter-LocalEnvironment.ps1",
    "scripts/rebuild/run_checks.py", "scripts/lab/Start-Lab.ps1",
    "scripts/rebuild/check_ci_inputs.py", "scripts/ci/Test-Ci.ps1",
    "scripts/diagnostics/qualify_ci_ui.py", "requirements/ci-windows-py312.lock",
    "requirements/ci.in", "docs/rebuild/ci-cd.md",
    "scripts/lab/Stop-Lab.ps1", "scripts/lab/Start-LocalPostgres.ps1", "scripts/release/package_release.py",
    "docs/operator/service-quickstart.md", "docs/operator/sql-api-quickstart.md",
    "docs/operator/release-update.md", "requirements/windows-py312.lock",
    "requirements/unit-windows-py312.lock", "requirements/lab-extra-windows-py312.lock",
    "data/demo-v1/manifest.json", "lab/api/static/swagger-ui-bundle.js",
})
KAFKA_REQUIRED_FILES = frozenset({
    "lab/kafka/compose.yaml", "lab/kafka/schema.sql", "lab/kafka/exercises.md",
    "src/lab/kafka/contracts.py", "src/lab/kafka/broker.py", "src/lab/kafka/consumer.py",
    "src/lab/kafka/store.py", "src/lab/kafka/api.py", "docs/operator/kafka-quickstart.md",
    "requirements/kafka-extra-windows-py312.lock", "scripts/kafka/Broker.ps1",
    "scripts/kafka/lab.py", "scripts/kafka/Install-KafkaDependencies.ps1",
    "scripts/kafka/Initialize-KafkaLab.ps1", "scripts/kafka/Run-Worker.ps1",
    "scripts/kafka/Test-KafkaLab.ps1", "scripts/kafka/qualify_cycle.py",
})
FORBIDDEN_PARTS = frozenset({"__pycache__", ".git", ".venv", "var", "verification",
                             ".pytest_cache", "node_modules"})
WINDOWS_RESERVED = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?\Z", re.I)
ZIP_TIME = (2026, 1, 1, 0, 0, 0)
MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_TOTAL_SIZE = 100 * 1024 * 1024
MAX_MEMBERS = 3000


class ReleaseError(ValueError):
    """Rejected source/archive; no deployment action is taken."""


def valid_path(name: str) -> bool:
    if not isinstance(name, str) or not name or any(char in name for char in '\\:*?<>|"'):
        return False
    if any(ord(char) < 32 for char in name):
        return False
    path = PurePosixPath(name)
    return (not path.is_absolute() and path.as_posix() == name
            and all(part not in {"", ".", ".."} and not part.endswith((" ", "."))
                    and not WINDOWS_RESERVED.fullmatch(part) for part in name.split("/")))


def allowed_path(name: str) -> bool:
    if not valid_path(name):
        return False
    parts = PurePosixPath(name).parts
    if any(part.casefold() in FORBIDDEN_PARTS for part in parts):
        return False
    if name in EXACT_FILES or name in DATA_FILES:
        return True
    if any(part.startswith(".") for part in parts):
        return False
    return any(name.startswith(tree + "/") and PurePosixPath(name).suffix in suffixes
               for tree, suffixes in SOURCE_TREES.items())


def _regular(path: Path) -> None:
    info = path.lstat()
    if (path.is_symlink() or getattr(info, "st_file_attributes", 0) &
            getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise ReleaseError(f"Links/reparse points are not allowed: {path.name}")
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise ReleaseError(f"Unsupported filesystem entry: {path.name}")


def _assert_source_path(root: Path, path: Path) -> None:
    relative = path.relative_to(root)
    current = root
    _regular(current)
    for part in relative.parts:
        current = current / part
        _regular(current)


def source_files(root: Path) -> list[Path]:
    """Find eligible files without descending into runtime or link targets."""
    root = root.absolute()
    names = set()
    for name in sorted(EXACT_FILES | DATA_FILES):
        path = root / name
        if path.exists() or path.is_symlink():
            _assert_source_path(root, path)
            if not path.is_file():
                raise ReleaseError(f"Expected file: {name}")
            names.add(name)
    for tree in SOURCE_TREES:
        start = root / tree
        if not start.exists():
            continue
        _assert_source_path(root, start)
        stack = [start]
        while stack:
            directory = stack.pop()
            for path in sorted(directory.iterdir()):
                if path.name.casefold() in FORBIDDEN_PARTS or path.name.startswith("."):
                    continue
                _regular(path)
                if path.is_dir():
                    stack.append(path)
                else:
                    name = path.relative_to(root).as_posix()
                    if allowed_path(name):
                        names.add(name)
    missing = REQUIRED_FILES - names
    if any(name.startswith('src/lab/kafka/') for name in names):
        missing |= KAFKA_REQUIRED_FILES - names
    if missing:
        raise ReleaseError("Missing required source files: " + ", ".join(sorted(missing)))
    if len(names) != len({name.casefold() for name in names}):
        raise ReleaseError("Case-insensitive source path collision")
    if len(names) > MAX_MEMBERS - 1:
        raise ReleaseError("Too many source files")
    return [root / name for name in sorted(names)]


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _read_version(data: bytes) -> str:
    try:
        value = json.loads(data)
        version = value["version"]
        if (not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version)
                or value.get("kind") != "source"
                or value.get("automatic_deployment") is not False
                or value.get("database_migration") is not False):
            raise ReleaseError("Invalid source-release version metadata")
        return version
    except (KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ReleaseError("Invalid release version file") from exc


def _zip_write(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, ZIP_TIME)
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, data, compresslevel=9)


def build(root: Path, output: Path) -> dict:
    root = root.absolute()
    files = source_files(root)
    payload = {}
    total = 0
    for path in files:
        if path.stat().st_size > MAX_FILE_SIZE:
            raise ReleaseError(f"Source file too large: {path.relative_to(root)}")
        data = path.read_bytes()
        total += len(data)
        if len(data) > MAX_FILE_SIZE or total > MAX_TOTAL_SIZE:
            raise ReleaseError("Source size limit exceeded")
        payload[path.relative_to(root).as_posix()] = data
    version = _read_version(payload[VERSION_FILE])
    manifest = {
        "schema_version": 1, "version": version, "kind": "source",
        "automatic_deployment": False, "database_migration": False,
        "runtime_included": False,
        "files": [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                  for name, data in payload.items()],
    }
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"aml-ai-agent-system-{version}-source.zip"
    checksum = target.with_suffix(".zip.sha256")
    if target.exists() or checksum.exists():
        raise ReleaseError("Release output already exists; use a new directory/version")
    # 'x' deliberately refuses to replace any existing release file.
    try:
        with target.open("xb") as stream, zipfile.ZipFile(stream, "w") as archive:
            for name, data in payload.items():
                _zip_write(archive, name, data)
            _zip_write(archive, MANIFEST, _json_bytes(manifest))
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        with checksum.open("x", encoding="ascii", newline="\n") as stream:
            stream.write(f"{digest}  {target.name}\n")
    except Exception:
        # Partial output is retained for diagnosis; never delete caller files.
        raise
    return verify(target)


def verify(archive_path: Path) -> dict:
    checksum = archive_path.with_suffix(".zip.sha256")
    try:
        sidecar = checksum.read_text(encoding="ascii").strip().split("  ")
    except (OSError, UnicodeError) as exc:
        raise ReleaseError("Missing/invalid archive SHA256 sidecar") from exc
    if (len(sidecar) != 2 or not SHA256_PATTERN.fullmatch(sidecar[0])
            or sidecar[1] != archive_path.name):
        raise ReleaseError("Invalid archive SHA256 sidecar")
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if digest != sidecar[0]:
        raise ReleaseError("Archive SHA256 mismatch")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            if not members or len(members) > MAX_MEMBERS:
                raise ReleaseError("Invalid member count")
            seen = set()
            total = 0
            for member in members:
                name = member.filename
                if (member.orig_filename != name or not valid_path(name) or name.casefold() in seen
                        or (name != MANIFEST and not allowed_path(name))):
                    raise ReleaseError(f"Unsafe, duplicate, or unlisted member: {name}")
                seen.add(name.casefold())
                mode = member.external_attr >> 16
                if (member.is_dir() or stat.S_ISLNK(mode)
                        or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                        or member.flag_bits & 1):
                    raise ReleaseError(f"Non-regular/encrypted member: {name}")
                total += member.file_size
                if member.file_size > MAX_FILE_SIZE or total > MAX_TOTAL_SIZE:
                    raise ReleaseError("Archive size limit exceeded")
            if MANIFEST not in archive.namelist():
                raise ReleaseError("Missing release manifest")
            manifest = json.loads(archive.read(MANIFEST))
            if (manifest.get("schema_version") != 1 or manifest.get("kind") != "source"
                    or manifest.get("automatic_deployment") is not False
                    or manifest.get("database_migration") is not False
                    or manifest.get("runtime_included") is not False):
                raise ReleaseError("Unsupported manifest")
            version = _read_version(archive.read(VERSION_FILE))
            if version != manifest.get("version"):
                raise ReleaseError("Manifest/version mismatch")
            expected = {}
            for record in manifest["files"]:
                name = record["path"]
                if (not allowed_path(name) or name in expected
                        or type(record["bytes"]) is not int or record["bytes"] < 0
                        or not SHA256_PATTERN.fullmatch(record["sha256"])):
                    raise ReleaseError("Invalid manifest file entry")
                expected[name] = record
            if set(expected) | {MANIFEST} != set(archive.namelist()):
                raise ReleaseError("Manifest does not match archive members")
            if REQUIRED_FILES - set(expected):
                raise ReleaseError("Release is missing required files")
            if (any(name.startswith('src/lab/kafka/') for name in expected)
                    and KAFKA_REQUIRED_FILES - set(expected)):
                raise ReleaseError("Release is missing Kafka module files")
            for name, record in expected.items():
                data = archive.read(name)
                if len(data) != record["bytes"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
                    raise ReleaseError(f"File integrity mismatch: {name}")
    except (zipfile.BadZipFile, KeyError, TypeError, AttributeError, json.JSONDecodeError,
            UnicodeDecodeError, RuntimeError) as exc:
        raise ReleaseError("Malformed source archive or manifest") from exc
    return {"status": "PASS", "version": version, "kind": "source", "files": len(expected),
            "uncompressed_bytes": total, "archive": str(archive_path.absolute()),
            "sha256": digest, "runtime_included": False, "automatic_deployment": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("build", help="Create source ZIP + SHA256; no installation")
    make.add_argument("--source-root", type=Path, default=ROOT)
    make.add_argument("--output-dir", type=Path, default=ROOT / "var/releases")
    check = commands.add_parser("verify", help="Verify ZIP, sidecar, manifest and paths; no extraction")
    check.add_argument("archive", type=Path)
    args = parser.parse_args()
    try:
        result = build(args.source_root, args.output_dir) if args.command == "build" else verify(args.archive)
    except (ReleaseError, OSError, ValueError) as exc:
        print(f"RELEASE_REJECTED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
