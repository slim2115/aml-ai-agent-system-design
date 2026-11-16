"""Durable local outbox. A simulator receipt never asserts external delivery.

Acceptance and publication use separate transactions: after a crash, the same
durable receipt is published without resending or reconsidering an accepted
export. The transaction accepting an export freezes that report's decisions.
"""
from datetime import datetime, timezone
import hashlib
import json
import uuid

from pydantic import ValidationError

from src.contracts.semantics import canonical_digest
from src.contracts.v1 import ADAPTERS, DeliveryRequest, Receipt
from .store import WorkflowError


DESTINATIONS = {'export': 'simulator-export', 'manual_queue': 'simulator-manual-queue'}
MANUAL_TERMINALS = frozenset({'refused', 'failed', 'timed_out', 'cancelled'})


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


class DeliveryService:
    """Trusted local application service; principal comes from the auth adapter.

    Destinations are fixed identifiers, not URLs or file paths. No caller can
    submit a receipt, choose external mode, or assert successful acceptance.
    """

    def __init__(self, store):
        self.store = store
        with store.transaction() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS delivery_outbox (
                delivery_id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
                owner_subject TEXT NOT NULL, revision INTEGER NOT NULL,
                status TEXT NOT NULL, payload_json TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES jobs(job_id))''')
            connection.execute('''CREATE TABLE IF NOT EXISTS delivery_simulator_receipts (
                delivery_id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL UNIQUE,
                receipt_json TEXT NOT NULL, request_sha256 TEXT NOT NULL,
                report_payload BLOB, report_sha256 TEXT,
                FOREIGN KEY(delivery_id) REFERENCES delivery_outbox(delivery_id))''')

    def create(self, request, principal, idempotency_key):
        try:
            request = DeliveryRequest.model_validate(
                request.model_dump(mode='json') if isinstance(request, DeliveryRequest) else request)
        except (ValidationError, TypeError, ValueError):
            raise WorkflowError('INVALID_INPUT_FORMAT') from None
        if request.destination != DESTINATIONS[request.operation]:
            raise WorkflowError('NOT_READY')
        payload = request.model_dump(mode='json')
        with self.store.transaction() as connection:
            job = self.store.authorize_job(connection, request.job_id, principal, allow_supervisor=False)
            previous = self.store.idempotent_response(
                connection, principal, 'create_delivery', idempotency_key, payload)
            if previous is not None:
                return self._validate(previous)
            self._binding(connection, request, principal, job)
            pending = self._validate({**payload, 'delivery_id': f'DEL-{uuid.uuid4().hex}',
                                      'mode': 'local_simulator', 'revision': 0,
                                      'attempt_count': 0, 'status': 'pending'})
            connection.execute('INSERT INTO delivery_outbox VALUES (?, ?, ?, ?, ?, ?)',
                               (pending.delivery_id, pending.job_id, job['owner_subject'],
                                0, 'pending', _json(pending.model_dump(mode='json'))))
            self.store.remember_idempotency(connection, principal, 'create_delivery',
                                            idempotency_key, payload, pending.model_dump(mode='json'))
            self.store.add_event(connection, pending.job_id, principal, 'DELIVERY_PENDING')
            return pending

    def get(self, delivery_id, principal):
        with self.store.transaction() as connection:
            delivery = self._get(connection, delivery_id, principal)
            if delivery.status == 'delivered':
                receipt = self._receipt(connection, delivery)
                if receipt is None or receipt != delivery.receipt:
                    raise WorkflowError('INTERNAL_ERROR')
            return delivery

    def list_for_job(self, job_id, principal):
        with self.store.transaction() as connection:
            self.store.authorize_job(connection, job_id, principal)
            deliveries = []
            for row in connection.execute(
                    'SELECT delivery_id FROM delivery_outbox WHERE job_id = ? ORDER BY rowid', (job_id,)):
                delivery = self._get(connection, row['delivery_id'], principal)
                if delivery.status == 'delivered':
                    receipt = self._receipt(connection, delivery)
                    if receipt is None or receipt != delivery.receipt:
                        raise WorkflowError('INTERNAL_ERROR')
                deliveries.append(delivery)
            return deliveries

    def export_artifact(self, delivery_id, principal) -> bytes:
        """Read verified immutable canonical JSON bytes for an accepted local export."""
        with self.store.transaction() as connection:
            delivery = self._get(connection, delivery_id, principal)
            if delivery.operation != 'export' or delivery.status != 'delivered':
                raise WorkflowError('INVALID_STATE')
            receipt = self._receipt(connection, delivery)
            if receipt is None or receipt != delivery.receipt:
                raise WorkflowError('INTERNAL_ERROR')
            return bytes(connection.execute(
                'SELECT report_payload FROM delivery_simulator_receipts WHERE delivery_id = ?',
                (delivery_id,)).fetchone()['report_payload'])

    def dispatch(self, delivery_id, principal, expected_revision):
        if type(expected_revision) is not int or expected_revision < 0:
            raise WorkflowError('INVALID_INPUT_FORMAT')
        with self.store.transaction() as connection:
            current = self._get(connection, delivery_id, principal, allow_supervisor=False)
            receipt = self._receipt(connection, current)
            if current.status == 'delivered':
                if receipt is None or receipt != current.receipt:
                    raise WorkflowError('INTERNAL_ERROR')
                return current
            # Receipt existence denotes a completed acceptance. An old revision
            # is then a replay/recovery, never permission for a second send.
            if receipt is None:
                if current.revision != expected_revision:
                    raise WorkflowError('VERSION_CONFLICT')
                request = DeliveryRequest.model_validate(self._request_payload(current))
                report = self._binding(connection, request, principal)
                updated = current.model_dump(mode='json')
                updated.pop('error', None)
                updated.update(status='pending', revision=current.revision + 1,
                               attempt_count=current.attempt_count + 1)
                current = self._validate(updated)
                self._save(connection, current)
                # If the simulator fails before acceptance, retain a safe retry
                # state; any partial adapter writes are rolled back as one unit.
                connection.execute('SAVEPOINT simulator_accept')
                try:
                    self._accept(connection, current, report, principal)
                except Exception:
                    connection.execute('ROLLBACK TO simulator_accept')
                    connection.execute('RELEASE simulator_accept')
                    updated.update(status='failed', error={
                        'schema_version': '1.0', 'status': 'error', 'generated_at': _now(),
                        'correlation_id': f'ERR-{uuid.uuid4().hex}',
                        'code': 'DEPENDENCY_UNAVAILABLE',
                        'message': 'Local simulator did not accept the delivery.', 'retryable': True})
                    failed = self._validate(updated)
                    self._save(connection, failed)
                    self.store.add_event(connection, current.job_id, principal, 'DELIVERY_FAILED')
                    return failed
                connection.execute('RELEASE simulator_accept')
        return self._publish(delivery_id, principal)

    def _publish(self, delivery_id, principal):
        """Separate commit deliberately makes accepted-but-unpublished recoverable."""
        with self.store.transaction() as connection:
            current = self._get(connection, delivery_id, principal, allow_supervisor=False)
            receipt = self._receipt(connection, current)
            if receipt is None:
                raise WorkflowError('INTERNAL_ERROR')
            if current.status == 'delivered':
                if current.receipt != receipt:
                    raise WorkflowError('INTERNAL_ERROR')
                return current
            payload = current.model_dump(mode='json')
            payload.pop('error', None)
            payload.update(status='delivered', revision=current.revision + 1,
                           receipt=receipt.model_dump(mode='json'))
            confirmed = self._validate(payload)
            self._save(connection, confirmed)
            self.store.add_event(connection, current.job_id, principal, 'SIMULATOR_DELIVERY_CONFIRMED')
            return confirmed

    def _accept(self, connection, delivery, report, principal):
        """Internal in-process simulator; no networking and no client receipt input."""
        receipt = Receipt.model_validate({
            'receipt_id': f'REC-{uuid.uuid4().hex}', 'delivery_id': delivery.delivery_id,
            'job_id': delivery.job_id, 'report': delivery.report.model_dump(mode='json') if delivery.report else None,
            'destination': delivery.destination, 'operation': delivery.operation,
            'mode': 'local_simulator', 'outcome': 'accepted', 'received_at': _now()})
        report_bytes = _json(json.loads(report['payload_json'])).encode('utf-8') if report else None
        digest = hashlib.sha256(report_bytes).hexdigest() if report_bytes is not None else None
        if delivery.report is not None and digest != delivery.report.content_sha256:
            raise WorkflowError('INTERNAL_ERROR')
        connection.execute('INSERT INTO delivery_simulator_receipts VALUES (?, ?, ?, ?, ?, ?)',
                           (delivery.delivery_id, receipt.receipt_id, _json(receipt.model_dump(mode='json')),
                            canonical_digest(self._request_payload(delivery)), report_bytes, digest))
        if delivery.operation == 'export':
            connection.execute('INSERT OR IGNORE INTO delivery_export_locks VALUES (?, ?, ?)',
                               (delivery.report.report_id, delivery.report.report_version, delivery.delivery_id))
        self.store.add_event(connection, delivery.job_id, principal, 'SIMULATOR_ACCEPTED')

    def _binding(self, connection, request, principal, job=None):
        job = job if job is not None else self.store.authorize_job(
            connection, request.job_id, principal, allow_supervisor=False)
        if job['status'] in MANUAL_TERMINALS:
            if request.operation != 'manual_queue' or request.report is not None or request.decision_id is not None:
                raise WorkflowError('INVALID_STATE')
            return None
        if job['status'] != 'succeeded' or request.report is None:
            raise WorkflowError('INVALID_STATE')
        report = self.store.get_report_row(connection, request.report.report_id,
                                          request.report.report_version, principal, allow_supervisor=False)
        if (report['job_id'] != request.job_id
                or report['content_sha256'] != request.report.content_sha256):
            raise WorkflowError('INVALID_STATE')
        try:
            report_payload = json.loads(report['payload_json'])
            draft = ADAPTERS['InvestigationResult'].validate_python(report_payload)
            job_payload = ADAPTERS['Job'].validate_python(json.loads(job['payload_json']))
            if (draft.status != 'draft' or draft.report_id != request.report.report_id
                    or draft.report_version != request.report.report_version
                    or canonical_digest(report_payload) != request.report.content_sha256
                    or job_payload.result != draft):
                raise ValueError('binding')
        except (ValidationError, ValueError, TypeError, AttributeError):
            raise WorkflowError('INTERNAL_ERROR') from None
        latest = self.store.latest_decision_row(connection, request.report.report_id, request.report.report_version)
        if request.operation == 'export' or request.decision_id is not None:
            if latest is None or latest['decision_id'] != request.decision_id:
                raise WorkflowError('INVALID_STATE')
            try:
                decision = ADAPTERS['DecisionRecord'].validate_python(json.loads(latest['payload_json']))
            except (ValidationError, ValueError, TypeError):
                raise WorkflowError('INTERNAL_ERROR') from None
            if (decision.decision_id != latest['decision_id'] or decision.revision != latest['revision']
                    or decision.report != request.report
                    or (request.operation == 'export' and decision.action != 'approve')):
                raise WorkflowError('INVALID_STATE')
        return report

    def _receipt(self, connection, delivery):
        row = connection.execute('SELECT * FROM delivery_simulator_receipts WHERE delivery_id = ?',
                                 (delivery.delivery_id,)).fetchone()
        if row is None:
            return None
        try:
            receipt = Receipt.model_validate(json.loads(row['receipt_json']))
            if (receipt.receipt_id != row['receipt_id'] or receipt.mode != 'local_simulator'
                    or row['request_sha256'] != canonical_digest(self._request_payload(delivery))):
                raise ValueError('receipt')
            for field in ('delivery_id', 'job_id', 'report', 'destination', 'operation', 'mode'):
                if getattr(receipt, field) != getattr(delivery, field):
                    raise ValueError('binding')
            if delivery.report is not None:
                if (row['report_payload'] is None or row['report_sha256'] != delivery.report.content_sha256
                        or hashlib.sha256(row['report_payload']).hexdigest() != row['report_sha256']):
                    raise ValueError('artifact')
            elif row['report_payload'] is not None or row['report_sha256'] is not None:
                raise ValueError('unexpected artifact')
            if delivery.operation == 'export' and connection.execute(
                    'SELECT 1 FROM delivery_export_locks WHERE report_id = ? AND report_version = ?',
                    (delivery.report.report_id, delivery.report.report_version)).fetchone() is None:
                raise ValueError('missing acceptance lock')
            return receipt
        except (ValidationError, ValueError, TypeError):
            raise WorkflowError('INTERNAL_ERROR') from None

    def _get(self, connection, delivery_id, principal, *, allow_supervisor=True):
        row = connection.execute('SELECT * FROM delivery_outbox WHERE delivery_id = ?', (delivery_id,)).fetchone()
        if row is None:
            raise WorkflowError('NOT_FOUND')
        job = self.store.authorize_job(connection, row['job_id'], principal, allow_supervisor=allow_supervisor)
        try:
            delivery = self._validate(json.loads(row['payload_json']))
            if (delivery.delivery_id != row['delivery_id'] or delivery.job_id != row['job_id']
                    or delivery.revision != row['revision'] or delivery.status != row['status']
                    or job['owner_subject'] != row['owner_subject'] or delivery.mode != 'local_simulator'):
                raise ValueError('binding')
            return delivery
        except (ValueError, TypeError):
            raise WorkflowError('INTERNAL_ERROR') from None

    @staticmethod
    def _request_payload(delivery):
        return {field: delivery.model_dump(mode='json')[field] for field in DeliveryRequest.model_fields}

    @staticmethod
    def _validate(payload):
        try:
            return ADAPTERS['Delivery'].validate_python(payload)
        except (ValidationError, ValueError, TypeError):
            raise WorkflowError('INTERNAL_ERROR') from None

    @staticmethod
    def _save(connection, delivery):
        connection.execute('UPDATE delivery_outbox SET revision = ?, status = ?, payload_json = ? WHERE delivery_id = ?',
                           (delivery.revision, delivery.status, _json(delivery.model_dump(mode='json')), delivery.delivery_id))
