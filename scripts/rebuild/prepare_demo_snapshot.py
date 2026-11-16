"""Create a separate deterministic synthetic fixture with declared UTC assumption.

This reads only this rebuild's frozen synthetic copy and writes only data/demo-v1
and config/demo-policy.json. It is not a conversion workflow for operational data.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.runtime.policy import DemoPolicy, strict_json
from src.runtime.context import SnapshotManifest
from src.contracts.v1 import CardRecord, ClientProfile, TransactionRecord
from src.runtime.context import CaseRecord


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def main():
    origin = ROOT / 'data' / 'synthetic'
    target = ROOT / 'data' / 'demo-v1'
    window = dict(start_inclusive='2026-07-01T00:00:00Z', end_exclusive='2026-07-20T00:00:00Z',
                  as_of='2026-07-20T00:00:00Z')
    original_sources, files, blobs = [], [], {}
    for name, model in [('cases', CaseRecord), ('clients', ClientProfile), ('cards', CardRecord),
                        ('transactions', TransactionRecord)]:
        data = (origin / f'{name}.json').read_bytes()
        original_sources.append(dict(source=f'{name}.json', version='535940fd645c2b41d1ec167e8a98d58597adadf0',
                                     content_sha256=hashlib.sha256(data).hexdigest()))
        converted = []
        for row in strict_json(data):
            selected = {key: row[key] for key in model.model_fields}
            time_field = 'created_at' if name == 'cases' else ('timestamp' if name == 'transactions' else None)
            if time_field:
                # Preserve civil clock components; this is a declared fixture assumption, not inferred timezone.
                from datetime import datetime
                if datetime.fromisoformat(selected[time_field]).tzinfo is not None:
                    raise ValueError('Fixture conversion expected a naive source timestamp')
                selected[time_field] += 'Z'
            model.model_validate(selected)
            converted.append(selected)
        blob = encoded(converted)
        blobs[f'{name}.json'] = blob
        files.append(dict(name=f'{name}.json', content_sha256=hashlib.sha256(blob).hexdigest(), row_count=len(converted)))
    rules_bytes = (origin / 'rules.json').read_bytes()
    rules = strict_json(rules_bytes)
    if not isinstance(rules, list) or not rules:
        raise ValueError('The synthetic rule corpus must be nonempty')
    # Preserve rule text and provenance verbatim; no legal verification is implied.
    blobs['rules.json'] = rules_bytes
    files.append(dict(name='rules.json', content_sha256=hashlib.sha256(rules_bytes).hexdigest(), row_count=len(rules)))
    original_sources.append(dict(source='rules.json', version='535940fd645c2b41d1ec167e8a98d58597adadf0',
                                 content_sha256=hashlib.sha256(rules_bytes).hexdigest()))
    snapshot_id = 'demo-' + hashlib.sha256(encoded(files)).hexdigest()[:24]
    manifest = dict(schema_version='1.0', snapshot_id=snapshot_id, captured_at=window['as_of'],
                    purpose='synthetic_learning_only', policy_id='synthetic-demo-2026-07-v1',
                    assumed_naive_timezone='UTC', window=window, original_sources=original_sources, files=files)
    SnapshotManifest.model_validate(manifest)
    manifest_bytes = encoded(manifest)
    policy = dict(schema_version='1.0', policy_id=manifest['policy_id'], purpose='synthetic_learning_only',
                  expert_approved=False, assumed_naive_timezone='UTC', snapshot_id=snapshot_id,
                  snapshot_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(), window=window,
                  max_snapshot_age_seconds=0, currency_scales={'RUB': 2, 'USD': 2},
                  applicability={'structuring': ['R-115-002'], 'high_risk_country': ['R-AML-010'],
                                 'threshold_breach': ['R-115-006'], 'kyc_mismatch': ['R-115-007'], 'velocity': []})
    DemoPolicy.model_validate(policy)
    for directory in (target, ROOT / 'config'):
        if directory.is_symlink() or not directory.resolve().is_relative_to(ROOT):
            raise ValueError('Output must remain inside the rebuild')
        directory.mkdir(parents=True, exist_ok=True)
    outputs = {target / name: blob for name, blob in blobs.items()}
    outputs[target / 'manifest.json'] = manifest_bytes
    outputs[ROOT / 'config' / 'demo-policy.json'] = encoded(policy)
    for path, blob in outputs.items():
        if path.is_symlink() or (path.exists() and path.stat().st_nlink != 1):
            raise ValueError('Refusing linked output')
        # Idempotent creation only. Existing edited snapshots require a new directory/version.
        if path.exists() and path.read_bytes() != blob:
            raise ValueError('Refusing to overwrite an edited demo fixture')
    for path, blob in outputs.items():
        if not path.exists():
            path.write_bytes(blob)
    print(json.dumps({'snapshot_id': snapshot_id, 'files': len(outputs), 'purpose': 'synthetic_learning_only'}))


if __name__ == '__main__':
    main()
