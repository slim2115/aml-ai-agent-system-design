"""Read-only fingerprints of the ordinary teaching stand, outside a QA profile.

This tool never starts services, reads event payloads, joins a Kafka group, or
commits offsets. Credentials are read privately and never included in output.
Offline Kafka uses an existing local image in a temporary, networkless read-only
helper to hash its owned volume, if Docker is available. The broker is never
started. Without that evidence, offline Kafka remains explicitly unobserved.
PostgreSQL's offline fallback hashes its existing cluster without starting it.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date, datetime, time, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
from uuid import UUID, uuid4


ROOT = Path(__file__).resolve().parents[2]
SQLITE_PATHS = ("var/workflow/workflow.sqlite3", "var/workflow/api-workflow.sqlite3")
PG_SCHEMAS = ("reference", "practice", "kafka_lab")
KAFKA_TOPICS = (
    "aml.lab.incident-review.v1", "aml.lab.incident-review.dlq.v1",
    "aml.lab.analysis-requested.v1", "aml.lab.analysis-requested.dlq.v1",
)
KAFKA_GROUPS = ("aml-lab-review-v1", "aml-lab-analysis-v1")
MAX_ROWS = 500_000
KAFKA_VOLUME = "aml-kafka-lab_kafka-data"
KAFKA_PROJECT = "aml-kafka-lab"
KAFKA_OWNER = "aml-ai-agent-system-rebuild:kafka:v1"
KAFKA_IMAGE = "apache/kafka:4.3.1"


def require_ordinary_environment() -> None:
    # Even an empty inherited selector is rejected, before any socket/file read.
    if "AML_QA_RUN_ID" in os.environ:
        raise ValueError("QA_PROFILE_PRESENT: run in a separate ordinary process")


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def cell(value):
    """Preserve types and avoid lossy decimal/byte coercion in row fingerprints."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return {"float": value.hex() if math.isfinite(value) else str(value)}
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"bytes": bytes(value).hex()}
    if isinstance(value, (datetime, date, time)):
        return {type(value).__name__: value.isoformat()}
    if isinstance(value, UUID):
        return {"uuid": str(value)}
    if isinstance(value, (tuple, list)):
        return [cell(item) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("UNSUPPORTED_MAP_KEY")
        return {key: cell(item) for key, item in value.items()}
    raise TypeError("UNSUPPORTED_DATABASE_TYPE")


def rows_fingerprint(rows) -> dict:
    encoded = []
    for row in rows:
        if len(encoded) >= MAX_ROWS:
            raise ValueError("ROW_LIMIT_EXCEEDED")
        encoded.append(canonical(cell(tuple(row))))
    # Sorting canonical rows does not depend on PKs, collation or physical order.
    encoded.sort()
    result = hashlib.sha256()
    result.update(str(len(encoded)).encode("ascii") + b"\n")
    for row in encoded:
        result.update(str(len(row)).encode("ascii") + b":" + row + b"\n")
    return {"rows": len(encoded), "rows_sha256": result.hexdigest()}


def safe_path(relative: str) -> Path:
    path = ROOT / relative
    if path.resolve() != path.absolute():
        raise ValueError("REDIRECTED_PATH")
    return path


def failure(code: str) -> dict:
    # Never expose driver messages: they can contain SQL, values or credentials.
    return {"status": "incomplete", "error_type": code, "observed": False}


def sqlite_snapshot(relative: str) -> dict:
    try:
        path = safe_path(relative)
        if not path.exists():
            return {"status": "absent", "observed": True, "path": relative}
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            tables = []
            for name, ddl in conn.execute(
                    "SELECT name, sql FROM sqlite_schema WHERE type='table' ORDER BY name").fetchall():
                quoted = '"' + name.replace('"', '""') + '"'
                cursor = conn.execute("SELECT * FROM " + quoted)
                tables.append({"table": name, "schema_sha256": digest(ddl),
                               "columns": [item[0] for item in cursor.description],
                               **rows_fingerprint(cursor)})
            conn.rollback()
        return {"status": "observed", "observed": True, "mode": "logical_read_only",
                "path": relative, "tables": tables, "content_sha256": digest(tables)}
    except Exception as exc:
        return {"path": relative, **failure(type(exc).__name__)}


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def offline_pg_snapshot() -> dict:
    """A stable file snapshot is weaker than a live SQL snapshot, but observable."""
    relative = "var/postgres/cluster"
    excluded_names = {"postmaster.pid", "postmaster.opts", "current_logfiles"}
    excluded_dirs = {"log", "logs", "pg_log"}
    try:
        cluster = safe_path(relative)
        if not cluster.exists():
            return {"status": "absent", "observed": True, "mode": "offline_cluster_files",
                    "path": relative}
        entries = []
        for directory, dirs, files in os.walk(cluster, followlinks=False):
            dirs[:] = sorted(name for name in dirs if name not in excluded_dirs)
            for name in dirs:
                path = Path(directory) / name
                if path.resolve() != path.absolute():
                    raise ValueError("REDIRECTED_CLUSTER_DIRECTORY")
            for name in sorted(files):
                if name in excluded_names:
                    continue
                path = Path(directory) / name
                if path.resolve() != path.absolute():
                    raise ValueError("REDIRECTED_CLUSTER_FILE")
                before = path.stat()
                hasher = hashlib.sha256()
                with path.open("rb") as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        hasher.update(block)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError("CLUSTER_CHANGED_DURING_READ")
                entries.append({"path": path.relative_to(cluster).as_posix(),
                                "bytes": before.st_size, "sha256": hasher.hexdigest()})
        if port_open(55432):
            raise ValueError("POSTGRES_STARTED_DURING_READ")
        entries.sort(key=lambda item: item["path"])
        return {"status": "observed", "observed": True, "mode": "offline_cluster_files",
                "server": "offline", "path": relative, "files": len(entries),
                "bytes": sum(item["bytes"] for item in entries), "manifest": entries,
                "content_sha256": digest(entries),
                "excluded_names": sorted(excluded_names), "excluded_dirs": sorted(excluded_dirs),
                "boundary": "Physical file hashes; no SQL or logical schema check while offline."}
    except Exception as exc:
        return {"server": "offline", **failure(type(exc).__name__)}


def postgres_snapshot() -> dict:
    if not port_open(55432):
        return offline_pg_snapshot()
    try:
        import psycopg
        from psycopg import sql

        path = safe_path("var/postgres/lab-connection.json")
        if path.stat().st_size > 16_384:
            raise ValueError("INVALID_CONFIG")
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        admin = config["roles"]["admin"]
        if (config["host"] != "127.0.0.1" or config["port"] != 55432
                or config["database"] != "aml_lab" or admin["user"] != "aml_lab_admin"
                or "run_id" in config):
            raise ValueError("NOT_ORDINARY_CONFIG")
        with psycopg.connect(
            host="127.0.0.1", hostaddr="127.0.0.1", port=55432, dbname="aml_lab",
            user="aml_lab_admin", password=admin["password"], connect_timeout=3,
            sslmode="disable", autocommit=True, application_name="aml-qa-preservation-read-only",
            options="-c timezone=UTC -c statement_timeout=10000 -c lock_timeout=2000 "
                    "-c idle_in_transaction_session_timeout=15000 -c search_path=pg_catalog "
                    "-c default_transaction_read_only=on",
        ) as conn:
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            schemas = []
            for schema in PG_SCHEMAS:
                exists = conn.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,)).fetchone()
                if not exists:
                    schemas.append({"schema": schema, "status": "absent"})
                    continue
                names = conn.execute(
                    "SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relkind IN ('r','p','S') "
                    "ORDER BY c.relname", (schema,)).fetchall()
                tables = []
                for name, kind in names:
                    cursor = conn.execute(sql.SQL("SELECT * FROM {}.{}").format(
                        sql.Identifier(schema), sql.Identifier(name)))
                    columns = [{"name": item.name, "type_oid": item.type_code} for item in cursor.description]
                    tables.append({"table": name, "kind": kind, "columns": columns,
                                   **rows_fingerprint(cursor)})
                schemas.append({"schema": schema, "status": "observed", "tables": tables})
            conn.execute("ROLLBACK")
        return {"status": "observed", "observed": True, "mode": "logical_read_only",
                "server": "online", "database": "aml_lab", "port": 55432,
                "schemas": schemas, "content_sha256": digest(schemas)}
    except Exception as exc:
        return {"server": "online", **failure(type(exc).__name__)}


class DockerProbeError(RuntimeError):
    """Stable, source-owned codes; raw Docker stderr is never emitted."""


def docker_cli(arguments: list[str], *, timeout=20) -> bytes:
    candidates = [shutil.which("docker"),
                  str(Path(os.environ.get("LOCALAPPDATA", "")) /
                      "Programs/DockerDesktop/resources/bin/docker.exe"),
                  r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"]
    executable = next((item for item in candidates if item and Path(item).is_file()), None)
    if executable is None:
        raise DockerProbeError("DOCKER_CLI_ABSENT")
    environment = os.environ.copy()
    for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        environment.pop(key, None)
    environment["PATH"] = str(Path(executable).parent) + os.pathsep + environment.get("PATH", "")
    try:
        result = subprocess.run(
            [executable, "--context", "desktop-linux", *arguments], cwd=ROOT,
            env=environment, capture_output=True, timeout=timeout, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        # Only the CLI child is ended by subprocess.run. Never kill arbitrary
        # containers; an already-created helper exits and removes itself via --rm.
        raise DockerProbeError("DOCKER_TIMEOUT_HELPER_MAY_STILL_BE_RUNNING") from None
    except OSError:
        raise DockerProbeError("DOCKER_EXEC_FAILED") from None
    if result.returncode:
        raise DockerProbeError("DOCKER_COMMAND_FAILED")
    return result.stdout


def docker_json(arguments: list[str]):
    try:
        return json.loads(docker_cli(arguments))
    except (ValueError, TypeError):
        raise DockerProbeError("DOCKER_INVALID_RESPONSE") from None


def ordinary_kafka_stopped() -> list[dict]:
    ids = docker_cli(["ps", "-aq", "--filter",
                      "label=com.docker.compose.project=" + KAFKA_PROJECT]).decode("ascii").split()
    # Also check every other container referencing this volume: an unlabelled
    # writer must not be hidden merely because the ordinary broker is stopped.
    mounted = docker_cli(["ps", "-aq", "--filter", "volume=" + KAFKA_VOLUME]).decode("ascii").split()
    ids = sorted(set(ids) | set(mounted))
    if not all(re.fullmatch(r"[0-9a-f]{12,64}", item) for item in ids):
        raise DockerProbeError("DOCKER_INVALID_CONTAINER_IDS")
    containers = docker_json(["inspect", *ids]) if ids else []
    records = []
    for container in containers:
        labels = container.get("Config", {}).get("Labels") or {}
        if (labels.get("com.docker.compose.project") != KAFKA_PROJECT
                or labels.get("aml.lab.owner") != KAFKA_OWNER):
            raise DockerProbeError("ORDINARY_KAFKA_CONTAINER_OWNERSHIP")
        state = container.get("State", {})
        if state.get("Running") is not False or state.get("Restarting") is not False:
            raise DockerProbeError("ORDINARY_KAFKA_CONTAINER_NOT_STOPPED")
        records.append({"id": container["Id"], "name": container["Name"], "running": False})
    return sorted(records, key=lambda item: item["id"])


def offline_kafka_snapshot() -> dict:
    helper = "qa-preserve-" + uuid4().hex
    helper_attempted = False
    try:
        contexts = docker_json(["context", "inspect", "desktop-linux"])
        endpoint = contexts[0]["Endpoints"]["docker"]["Host"].replace("\\", "/")
        if not re.fullmatch(r"npipe:/+\./pipe/(docker_engine|dockerDesktopLinuxEngine)", endpoint):
            raise DockerProbeError("LOCAL_DOCKER_NAMED_PIPE_REQUIRED")
        if docker_cli(["info", "--format", "{{.OSType}}"]).strip() != b"linux":
            raise DockerProbeError("LOCAL_DOCKER_LINUX_REQUIRED")
        volumes = docker_cli(["volume", "ls", "--quiet", "--filter",
                              "name=^" + KAFKA_VOLUME + "$" ]).decode("utf-8").split()
        if not volumes:
            return {"status": "absent", "observed": True, "server": "offline",
                    "mode": "offline_volume_files", "volume": KAFKA_VOLUME}
        if volumes != [KAFKA_VOLUME]:
            raise DockerProbeError("ORDINARY_KAFKA_VOLUME_NAME")
        volume = docker_json(["volume", "inspect", KAFKA_VOLUME])[0]
        labels = volume.get("Labels") or {}
        if (volume.get("Name") != KAFKA_VOLUME or labels.get("aml.lab.owner") != KAFKA_OWNER
                or labels.get("com.docker.compose.project") != KAFKA_PROJECT):
            raise DockerProbeError("ORDINARY_KAFKA_VOLUME_OWNERSHIP")
        before = ordinary_kafka_stopped()
        image = docker_json(["image", "inspect", KAFKA_IMAGE])[0]
        image_id = image["Id"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise DockerProbeError("LOCAL_KAFKA_IMAGE_ID_INVALID")
        # Constant shell text: no host values, event data or user strings are
        # interpolated. NUL records preserve file names unambiguously.
        command = (
            "set -euo pipefail; cd /snapshot; "
            "if [ -n \"$(find . -type l -print -quit)\" ]; then exit 44; fi; "
            "find . -type f -print0 | LC_ALL=C sort -z | "
            "while IFS= read -r -d '' file; do sha256sum --zero -- \"$file\"; done"
        )
        helper_attempted = True
        output = docker_cli([
            "run", "--rm", "--name", helper, "--label", "aml.qa.purpose=preservation-read-only",
            "--network", "none", "--read-only", "--pull", "never", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", "64", "--memory", "256m",
            "--cpus", "1", "--mount", "type=volume,src=" + KAFKA_VOLUME + ",dst=/snapshot,readonly",
            "--entrypoint", "/bin/bash", image_id, "-c", command,
        ], timeout=60)
        after = ordinary_kafka_stopped()
        volume_after = docker_json(["volume", "inspect", KAFKA_VOLUME])[0]
        identity_keys = ("Name", "Driver", "CreatedAt", "Labels", "Scope", "Options")
        identity = {key: volume.get(key) for key in identity_keys}
        if before != after or identity != {key: volume_after.get(key) for key in identity_keys}:
            raise DockerProbeError("ORDINARY_KAFKA_CHANGED_DURING_SNAPSHOT")
        if port_open(9092):
            raise DockerProbeError("ORDINARY_KAFKA_STARTED_DURING_SNAPSHOT")
        entries = []
        for record in output.split(b"\0"):
            if not record:
                continue
            if len(record) < 69 or record[64:66] != b"  ":
                raise DockerProbeError("KAFKA_HASH_OUTPUT_INVALID")
            file_hash, relative = record[:64].decode("ascii"), record[66:].decode("utf-8")
            if (not re.fullmatch(r"[0-9a-f]{64}", file_hash) or not relative.startswith("./")
                    or any(part in ("", ".", "..") for part in relative[2:].split("/"))):
                raise DockerProbeError("KAFKA_HASH_ENTRY_INVALID")
            entries.append({"path": relative[2:], "sha256": file_hash})
        entries.sort(key=lambda item: item["path"])
        if len({item["path"] for item in entries}) != len(entries):
            raise DockerProbeError("KAFKA_HASH_DUPLICATE_PATH")
        return {"status": "observed", "observed": True, "server": "offline",
                "mode": "offline_volume_files", "volume": KAFKA_VOLUME,
                "volume_identity_sha256": digest(identity), "containers": before,
                "helper_image": KAFKA_IMAGE, "helper_image_id": image_id,
                "files": len(entries), "manifest": entries, "content_sha256": digest(entries),
                "boundary": "Backing file bytes only; logical group offsets are not decoded. "
                            "Broker containers verified stopped before/after; temporary helper used a read-only volume."}
    except Exception as exc:
        result = {"server": "offline", **failure(type(exc).__name__),
                  "boundary": "Kafka volume was not verified; ordinary broker was not started."}
        if isinstance(exc, DockerProbeError):
            result["error_code"] = str(exc)
        if helper_attempted:
            result["helper_name"] = helper
            result["helper_boundary"] = "Helper uses --rm; on timeout it may finish later. No container was forcibly stopped."
        return result


def kafka_snapshot() -> dict:
    if not port_open(9092):
        return offline_kafka_snapshot()
    consumers = []
    try:
        from confluent_kafka import Consumer, TopicPartition

        def observer(group):
            consumer = Consumer({
                "bootstrap.servers": "127.0.0.1:9092", "group.id": group,
                "client.id": "aml-qa-preservation-read-only", "enable.auto.commit": False,
                "enable.auto.offset.store": False, "allow.auto.create.topics": False,
                "socket.timeout.ms": 5000, "log_level": 0,
            })
            consumers.append(consumer)
            return consumer

        # Creating a Consumer and querying offsets does not join a group.
        # Deliberately never call subscribe, assign, poll, consume or commit.
        reader = observer(KAFKA_GROUPS[0])
        metadata = reader.list_topics(timeout=5)
        topics, coordinates = [], []
        for name in KAFKA_TOPICS:
            topic = metadata.topics.get(name)
            if topic is None:
                topics.append({"topic": name, "status": "absent"})
                continue
            if topic.error is not None:
                raise ValueError("TOPIC_METADATA_ERROR")
            partitions = []
            for number in sorted(topic.partitions):
                partition = topic.partitions[number]
                if partition.error is not None:
                    raise ValueError("PARTITION_METADATA_ERROR")
                tp = TopicPartition(name, number)
                low, high = reader.get_watermark_offsets(tp, timeout=5, cached=False)
                coordinates.append(tp)
                partitions.append({"partition": number, "low_offset": low, "log_end_offset": high})
            topics.append({"topic": name, "status": "observed", "partitions": partitions})
        groups = []
        for index, group in enumerate(KAFKA_GROUPS):
            consumer = reader if index == 0 else observer(group)
            offsets = []
            if coordinates:
                for item in consumer.committed(coordinates, timeout=5):
                    if item.error is not None:
                        raise ValueError("GROUP_OFFSETS_ERROR")
                    offsets.append({"topic": item.topic, "partition": item.partition,
                                    "committed_offset": item.offset})
            offsets.sort(key=lambda item: (item["topic"], item["partition"]))
            groups.append({"group": group, "offsets": offsets})
        content = {"topics": topics, "groups": groups}
        return {"status": "observed", "observed": True, "server": "online",
                "bootstrap": "127.0.0.1:9092", **content, "content_sha256": digest(content),
                "boundary": "Offsets only, not event payloads. Per-request snapshots are not atomic across partitions."}
    except Exception as exc:
        return {"server": "online", **failure(type(exc).__name__)}
    finally:
        for consumer in consumers:
            consumer.close()


def snapshot() -> dict:
    require_ordinary_environment()
    resources = {path: sqlite_snapshot(path) for path in SQLITE_PATHS}
    resources["postgresql"] = postgres_snapshot()
    resources["kafka"] = kafka_snapshot()
    return {"format": "aml-learning-preservation-v1",
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "scope": "ordinary_learning_stand_only", "resources": resources,
            "fully_observed": all(item["observed"] for item in resources.values()),
            "substantive_sha256": digest(resources),
            "boundary": "External user/service writes can change observations. Ordinary services were not started, "
                        "stopped or mutated. Offline Kafka may use a temporary read-only helper container."}


def compare(previous: dict, current: dict) -> dict:
    if previous.get("format") != "aml-learning-preservation-v1":
        raise ValueError("UNSUPPORTED_PREVIOUS_SNAPSHOT")
    changes, unobserved = [], []
    before, after = previous["resources"], current["resources"]
    for key in sorted(set(before) | set(after)):
        if key not in before or key not in after:
            changes.append(key)
        elif not before[key]["observed"] or not after[key]["observed"]:
            unobserved.append(key)
        elif (before[key].get("status") != "absent" and after[key].get("status") != "absent"
              and before[key].get("mode") != after[key].get("mode")):
            # A physical offline hash cannot be compared with a live logical SQL hash.
            unobserved.append(key)
        elif digest(before[key]) != digest(after[key]):
            changes.append(key)
    verdict = "CHANGED" if changes else "INCOMPLETE" if unobserved else "UNCHANGED"
    return {"verdict": verdict, "changed_resources": changes, "unobserved_resources": unobserved,
            "observed_resources_unchanged": not changes,
            "substantive_digest_equal": previous["substantive_sha256"] == current["substantive_sha256"]}


def artifact_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    if path.resolve() != path.absolute():
        raise ValueError("REDIRECTED_ARTIFACT_PATH")
    relative = path.relative_to(ROOT / "verification")
    if len(relative.parts) < 2 or not relative.parts[0].startswith("qa-step-02") or path.suffix != ".json":
        raise ValueError("OUTPUT_REQUIRES_VERIFICATION_QA_STEP_02_JSON")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--compare", help="Previous preservation JSON in verification/qa-step-02...")
    args = parser.parse_args()
    try:
        require_ordinary_environment()
        output = artifact_path(args.output)
        if output.exists():
            raise FileExistsError("OUTPUT_EXISTS")
        previous = None
        if args.compare:
            previous = json.loads(artifact_path(args.compare).read_text(encoding="utf-8"))
        result = snapshot()
        if previous is not None:
            result["comparison"] = compare(previous, result)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print(json.dumps({"output": str(output), "fully_observed": result["fully_observed"],
                          "substantive_sha256": result["substantive_sha256"],
                          "comparison": result.get("comparison")}, ensure_ascii=False))
        verdict = result.get("comparison", {}).get("verdict")
        # Capturing partial evidence succeeds; comparison must never pass a gate
        # solely because two unobserved/offline resources look equally unknown.
        return 1 if verdict == "CHANGED" else 3 if verdict == "INCOMPLETE" else 0
    except Exception as exc:
        print(json.dumps({"status": "ERROR", "error_type": type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
