# Durable local delivery simulator

Block 5 implements a local outbox and a simulated receiver in the rebuild SQLite
database. `delivered` means that this local simulator durably accepted the item.
It does not mean delivery to a bank, regulator, case system, operator, or network
queue. SHA-256 binds bytes; it is not a digital signature. Neither delivery nor
approval closes an AML case. External destinations, signatures and case closure
remain `NOT_READY` pending approved integration and authority contracts.

## Public application interface

`DeliveryService(SQLiteStore(...))` exposes:

- `create(request, principal, idempotency_key) -> Delivery`: validates the v1
  request and commits a pending outbox record without sending anything.
- `get(delivery_id, principal) -> Delivery` and
  `list_for_job(job_id, principal) -> list[Delivery]`: read owned state; the local
  supervisor may inspect other owners' state.
- `dispatch(delivery_id, principal, expected_revision) -> Delivery`: only the
  owner can request local acceptance. Caller-supplied receipts, mode flags and
  acceptance booleans are not inputs.
- `export_artifact(delivery_id, principal) -> bytes`: reads verified canonical
  report JSON bytes from a confirmed local export; it does not write a file or
  transmit anything. Read authorization also permits the local supervisor.

Principals are constructed by the trusted local identity adapter, never from
request fields. This is a local simulation of authorization, not production IAM.
Errors expose a `WorkflowError.code`, never source records or adapter exceptions.

## Eligibility

| Job state | Operation | Required binding | Destination |
|---|---|---|---|
| succeeded | export | Exact stored report id, version and SHA; current approved decision id | `simulator-export` |
| succeeded | manual_queue | Exact stored report reference; optional current decision id | `simulator-manual-queue` |
| refused, failed, timed_out, cancelled | manual_queue | Job id; report and decision must both be null | `simulator-manual-queue` |
| queued, running | either | Not eligible | none |

An approval is required for export. Rejection, a changes request, an older
approval, a report from another job or a mismatched hash cannot authorize it.
The service verifies the stored job result against the stored report and its
canonical digest. An optional decision for a draft manual queue request must be
the current decision bound to the same report. Manual queue acceptance is a
simulated task registration; it neither transfers authority nor freezes decisions.

## Transactions, replay and recovery

Creation uses subject-scoped persistent idempotency. Repeating a key and identical
request returns its original pending response; use `get` for current state.
Reusing a key with different request data raises `IDEMPOTENCY_CONFLICT`.

Dispatch performs these actions under a single `BEGIN IMMEDIATE` transaction:

1. Authorize the owner and check the current revision.
2. Recheck current report and decision binding.
3. Increment attempt count/revision and record local acceptance: one immutable
   receipt plus canonical report UTF-8 bytes and SHA, when a report is present.
4. For export, insert a report-version decision lock in the same transaction.

After acceptance commits, a second transaction publishes `delivered`. A crash
between these commits leaves a pending record with a durable receipt. Retrying
publishes that exact receipt without a second acceptance or attempt increment.
The original pre-crash revision is accepted only for an already accepted item.
Replaying an already delivered item likewise returns its existing receipt.
Before acceptance, a stale revision fails with `VERSION_CONFLICT`.

Once export acceptance commits, later decisions for that report version are
rejected with `INVALID_STATE`. This explicit simulator rule prevents changing
the decision underneath an accepted immutable export, including during recovery.
The prototype has no post-export revocation workflow; a real integration must
define that policy before replacing this rule.

An adapter failure before acceptance rolls back all tentative receipt and lock
writes and records a safe retryable `failed` state. Retry requires its current
revision and rechecks authorization and decision eligibility. Database commit
failure cannot produce a reported success. Concurrent dispatches converge on
one receipt. A deliberately new create idempotency key requests a separate
delivery and can produce a separate receipt for the same frozen report.

Receipt identity, job/report/destination/operation/mode, request hash, exported
bytes and SHA are checked on reads and recovery. A missing or mismatched receipt,
artifact or export decision lock is an internal consistency error, not a
confirmed delivery. Receipt rows have no public insertion endpoint.

## Verification scope

`tests/workflow/test_delivery.py` exercises persistence across service restart,
authorization, CAS, idempotency, approval changes before dispatch, export locks,
failed acceptance rollback, crash recovery, concurrent dispatch, manual terminal
states, reference mismatches, client receipt injection and corrupted receipt or
artifact detection. Tests use synthetic reports and isolated databases under
`var/workflow/delivery-tests`; default test fixtures deny network access.

This establishes local transaction semantics. It does not establish qualified
human acceptance, legal approval, external exactly-once delivery, network access
control, production authentication or signed evidence custody.
