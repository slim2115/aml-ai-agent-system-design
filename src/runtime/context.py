"""Read a coherent, hash-bound synthetic snapshot afresh for every request."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timezone
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, ValidationError

from src.config import PROJECT_ROOT, resolve_project_path
from src.contracts.v1 import (AnalysisWindow, CardRecord, ClientProfile, Digest, Identifier,
    IncidentId, InvestigationContext, Snapshot, SourceVersion, StrictModel, Timestamp,
    TransactionRecord, instant)
from src.runtime.policy import DemoPolicy, strict_json

FILES = ('cases.json', 'clients.json', 'cards.json', 'transactions.json', 'rules.json')
_INCIDENT = TypeAdapter(IncidentId)


class ContextFailure(ValueError):
    """Only fixed safe diagnostics leave the data boundary."""
    _MESSAGES = {
        'MISSING_INPUT': 'An incident identifier is required.',
        'INVALID_INPUT_FORMAT': 'The incident identifier has an invalid format.',
        'NOT_FOUND': 'The incident was not found in the selected snapshot.',
        'NOT_READY': 'The selected snapshot is unavailable or cannot be verified.',
        'CONFLICTING_DATA': 'The snapshot contains conflicting or invalid source records.',
        'INSUFFICIENT_DATA': 'The selected incident lacks the data required by the synthetic replay policy.',
        'POLICY_NOT_APPROVED': 'An explicit valid synthetic replay policy is required.',
    }

    def __init__(self, code: str):
        self.code = code
        super().__init__(self._MESSAGES[code])


class CaseRecord(StrictModel):
    case_id: Identifier
    incident_id: IncidentId
    client_id: Identifier
    alert_type: Annotated[str, Field(pattern=r'^[a-z][a-z0-9_]{0,63}$', max_length=64)]
    created_at: Timestamp


class SnapshotFile(StrictModel):
    name: Literal['cases.json', 'clients.json', 'cards.json', 'transactions.json', 'rules.json']
    content_sha256: Digest
    row_count: Annotated[int, Field(ge=0)]


class SnapshotManifest(StrictModel):
    schema_version: Literal['1.0']
    snapshot_id: Identifier
    captured_at: Timestamp
    purpose: Literal['synthetic_learning_only']
    policy_id: Identifier
    assumed_naive_timezone: Literal['UTC']
    window: AnalysisWindow
    original_sources: list[SourceVersion]
    files: list[SnapshotFile]


@dataclass(frozen=True)
class PreparedContext:
    context: InvestigationContext
    alert_type: str
    rule_corpus_path: Path
    rule_corpus_sha256: str


def _unique(rows, field):
    values = [getattr(row, field) for row in rows]
    if len(values) != len(set(values)):
        raise ContextFailure('CONFLICTING_DATA')
    return {getattr(row, field): row for row in rows}


class ContextBuilder:
    def __init__(self, snapshot_dir: Path, policy: DemoPolicy):
        try:
            self.snapshot_dir = Path(resolve_project_path(snapshot_dir, root=PROJECT_ROOT, key='snapshot'))
            # Frozen Pydantic models still contain mutable nested lists/dicts.
            self.policy = DemoPolicy.model_validate(policy.model_dump(mode='python'))
        except (AttributeError, OSError, ValueError, TypeError):
            raise ContextFailure('POLICY_NOT_APPROVED') from None

    def _bytes(self, name, limit=16 * 1024 * 1024):
        try:
            path = self.snapshot_dir / name
            resolved = path.resolve()
            resolved.relative_to(self.snapshot_dir)
            if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
                raise ValueError
            data = path.read_bytes()
            if len(data) > limit:
                raise ValueError
            return data
        except (OSError, ValueError, RuntimeError):
            raise ContextFailure('NOT_READY') from None

    def _read(self):
        try:
            policy = DemoPolicy.model_validate(self.policy.model_dump(mode='python'))
        except (ValueError, TypeError):
            raise ContextFailure('POLICY_NOT_APPROVED') from None
        manifest_bytes = self._bytes('manifest.json', 131072)
        if sha256(manifest_bytes).hexdigest() != policy.snapshot_manifest_sha256:
            raise ContextFailure('NOT_READY')
        try:
            manifest = SnapshotManifest.model_validate(strict_json(manifest_bytes))
        except (ValueError, TypeError):
            raise ContextFailure('CONFLICTING_DATA') from None
        if (manifest.snapshot_id != policy.snapshot_id or manifest.policy_id != policy.policy_id
                or manifest.window != policy.window or len(manifest.files) != len(FILES)
                or {f.name for f in manifest.files} != set(FILES)
                or not manifest.original_sources
                or len({s.source for s in manifest.original_sources}) != len(manifest.original_sources)):
            raise ContextFailure('CONFLICTING_DATA')
        snapshot_age = (instant(manifest.captured_at) - instant(policy.window.as_of)).total_seconds()
        if not 0 <= snapshot_age <= policy.max_snapshot_age_seconds:
            raise ContextFailure('NOT_READY')
        descriptors = {f.name: f for f in manifest.files}
        models = dict(zip(FILES, (CaseRecord, ClientProfile, CardRecord, TransactionRecord)))
        result = {}
        for name in FILES:
            data = self._bytes(name)
            if sha256(data).hexdigest() != descriptors[name].content_sha256:
                raise ContextFailure('NOT_READY')
            try:
                rows = strict_json(data)
                if not isinstance(rows, list) or len(rows) != descriptors[name].row_count:
                    raise ValueError
                if name == 'rules.json':
                    if not rows or any(not isinstance(row, dict) for row in rows):
                        raise ValueError
                    # Rule editions/chunks are validated by the retrieval boundary.
                    result[name] = rows
                else:
                    result[name] = [models[name].model_validate(row) for row in rows]
            except (TypeError, ValueError, ValidationError):
                raise ContextFailure('CONFLICTING_DATA') from None
        # An atomic directory/manifest replacement during this read is not silently accepted.
        if self._bytes('manifest.json', 131072) != manifest_bytes:
            raise ContextFailure('NOT_READY')
        return policy, manifest, result

    def build(self, incident_id: str) -> PreparedContext:
        if incident_id is None or incident_id == '':
            raise ContextFailure('MISSING_INPUT')
        try:
            incident_id = _INCIDENT.validate_python(incident_id, strict=True)
        except (ValueError, TypeError):
            raise ContextFailure('INVALID_INPUT_FORMAT') from None
        policy, manifest, rows = self._read()
        cases = _unique(rows['cases.json'], 'incident_id')
        _unique(rows['cases.json'], 'case_id')
        clients = _unique(rows['clients.json'], 'client_id')
        cards = _unique(rows['cards.json'], 'card_id')
        transactions = _unique(rows['transactions.json'], 'tx_id')
        # Validate every record before selecting a client/window. No invalid row is dropped.
        as_of_date = instant(policy.window.as_of).astimezone(timezone.utc).date()
        for client in clients.values():
            if date.fromisoformat(client.client_since) > as_of_date:
                raise ContextFailure('CONFLICTING_DATA')
        for card in cards.values():
            if card.client_id not in clients:
                raise ContextFailure('CONFLICTING_DATA')
        for tx in transactions.values():
            if tx.client_id not in clients or (tx.card_id is not None and (
                    tx.card_id not in cards or cards[tx.card_id].client_id != tx.client_id)):
                raise ContextFailure('CONFLICTING_DATA')
            scale = policy.currency_scales.get(tx.currency)
            if scale is None or len(tx.amount.partition('.')[2]) > scale:
                raise ContextFailure('CONFLICTING_DATA')
            if instant(tx.timestamp) > instant(manifest.captured_at):
                raise ContextFailure('CONFLICTING_DATA')
        for case in cases.values():
            # A capture lag must not make incidents from after the requested as_of visible.
            if instant(case.created_at) > instant(policy.window.as_of):
                raise ContextFailure('CONFLICTING_DATA')
            if case.client_id not in clients:
                # A missing profile is incomplete input, while contradictory FK ownership above is conflicting.
                raise ContextFailure('INSUFFICIENT_DATA')
        case = cases.get(incident_id)
        if case is None:
            raise ContextFailure('NOT_FOUND')
        selected = [t for t in transactions.values() if t.client_id == case.client_id and
                    instant(policy.window.start_inclusive) <= instant(t.timestamp) < instant(policy.window.end_exclusive)]
        if not selected:
            raise ContextFailure('INSUFFICIENT_DATA')
        context = InvestigationContext(schema_version='1.0', incident_id=incident_id, client_id=case.client_id,
            snapshot=Snapshot(snapshot_id=manifest.snapshot_id, captured_at=manifest.captured_at,
                sources=[SourceVersion(source=f.name, version=f.content_sha256, content_sha256=f.content_sha256)
                         for f in manifest.files]), window=policy.window, policy_version=policy.policy_id,
            data_completeness='full', consistency='consistent', missing_fields=[], client=clients[case.client_id],
            cards=[c for c in cards.values() if c.client_id == case.client_id], transactions=selected)
        return PreparedContext(context=context, alert_type=case.alert_type,
            rule_corpus_path=self.snapshot_dir / 'rules.json',
            rule_corpus_sha256=next(f.content_sha256 for f in manifest.files if f.name == 'rules.json'))
