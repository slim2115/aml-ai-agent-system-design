"""Detect changes to a predeclared evaluator inventory.

This is an integrity diagnostic, not a security boundary or attestation. Its
commitment must be supplied by a separate trusted authority; code, files and
commitment in the same workspace do not constitute a hidden holdout.
"""
from __future__ import annotations

from hashlib import sha256
import hmac
import json
from pathlib import Path, PurePosixPath
import re

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_MEMBERS = 1000


class InventoryError(ValueError):
    """Invalid inventory, commitment, path or input file."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _member(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or '\\' in name or ':' in name:
        raise InventoryError('INVALID_PATH')
    pure = PurePosixPath(name)
    if pure.is_absolute() or '/'.join(pure.parts) != name or any(x in {'.', '..'} for x in pure.parts):
        raise InventoryError('INVALID_PATH')
    candidate = root
    for part in pure.parts:
        candidate = candidate / part
        if candidate.is_symlink() or (hasattr(candidate, 'is_junction') and candidate.is_junction()):
            raise InventoryError('LINK_FORBIDDEN')
    if not candidate.resolve(strict=False).is_relative_to(root):
        raise InventoryError('PATH_ESCAPE')
    return candidate


def capture(root: Path, names: list[str]) -> dict:
    """Capture regular files; caller selects the complete protected boundary."""
    root = Path(root).resolve(strict=True)
    if not root.is_dir() or not isinstance(names, list) or not 0 < len(names) <= MAX_MEMBERS:
        raise InventoryError('INVALID_INVENTORY')
    if any(not isinstance(name, str) for name in names) or len({x.casefold() for x in names}) != len(names):
        raise InventoryError('DUPLICATE_OR_INVALID_NAME')
    files = []
    for name in sorted(names):
        path = _member(root, name)
        try:
            if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                raise InventoryError('INVALID_FILE')
            raw = path.read_bytes()
        except OSError as exc:
            raise InventoryError('INVALID_FILE') from exc
        if len(raw) > MAX_FILE_BYTES:
            raise InventoryError('INVALID_FILE')
        files.append({'path': name, 'bytes': len(raw), 'sha256': sha256(raw).hexdigest()})
    return {'schema_version': 1, 'files': files}


def commitment(inventory: dict) -> str:
    """Return the digest to retain independently of the candidate workspace."""
    return sha256(_canonical(inventory)).hexdigest()


def verify(root: Path, inventory: dict, expected_commitment: str) -> dict:
    """Return PASS only when trusted inventory and every declared file match."""
    if not isinstance(expected_commitment, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_commitment):
        raise InventoryError('INVALID_COMMITMENT')
    if not hmac.compare_digest(commitment(inventory), expected_commitment):
        raise InventoryError('INVENTORY_COMMITMENT_MISMATCH')
    if (not isinstance(inventory, dict) or set(inventory) != {'schema_version', 'files'}
            or type(inventory['schema_version']) is not int or inventory['schema_version'] != 1):
        raise InventoryError('INVALID_INVENTORY')
    entries = inventory['files']
    if not isinstance(entries, list) or not entries or len(entries) > MAX_MEMBERS:
        raise InventoryError('INVALID_INVENTORY')
    if any(not isinstance(e, dict) or set(e) != {'path', 'bytes', 'sha256'}
           or type(e['bytes']) is not int or not 0 <= e['bytes'] <= MAX_FILE_BYTES
           or not isinstance(e['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', e['sha256'])
           for e in entries):
        raise InventoryError('INVALID_INVENTORY')
    names = [e['path'] for e in entries]
    if any(not isinstance(n, str) for n in names) or len({n.casefold() for n in names}) != len(names):
        raise InventoryError('INVALID_INVENTORY')
    changes = []
    for entry in entries:
        try:
            current = capture(root, [entry['path']])['files'][0]
        except InventoryError:
            changes.append({'path': entry['path'], 'reason': 'UNREADABLE_OR_UNSAFE'})
            continue
        if current != entry:
            changes.append({'path': entry['path'], 'reason': 'CONTENT_CHANGED'})
    return {'status': 'FAIL' if changes else 'PASS', 'checked_count': len(entries),
            'changes': changes, 'scope': 'Declared evaluator files only; not attestation or holdout isolation.'}
