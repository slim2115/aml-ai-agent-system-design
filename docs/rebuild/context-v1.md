# Synthetic context v1

`src/runtime/context.py` replaces the indefinitely cached legacy repositories in
the new runtime. `ContextBuilder(snapshot_dir, policy).build(incident_id)` returns
`PreparedContext(context, alert_type, rule_corpus_path, rule_corpus_sha256)`.
The context uses the strict v1 contract. `ContextFailure.code` provides a fixed
safe error or refusal code without source values or exception details.

## Explicit replay scope

`config/demo-policy.json` is a **synthetic learning policy, without expert approval**.
Only explicitly selected demo mode uses it. Its alert-to-rule mapping selects
educational candidates; it does not establish legal applicability, transaction
thresholds, high-risk jurisdictions or adequate operational KYC.

`scripts/rebuild/prepare_demo_snapshot.py` creates a separate `data/demo-v1` from
the rebuild's synthetic copy. Source timestamps had no timezone. The conversion
preserves their clock values and explicitly assumes UTC for this fixture only.
The manifest retains the five original source hashes and frozen commit identity.
Rules are copied verbatim, without legal verification. Existing edited generated
files are not overwritten by the conversion script.

The fixture window is `[2026-07-01T00:00:00Z, 2026-07-20T00:00:00Z)`, with `as_of`
and capture at the end. Its permitted capture lag is zero seconds. The general
check is `0 <= captured_at - as_of <= max_snapshot_age_seconds`; this is historical
replay consistency, **not wall-clock freshness or an approved live-data TTL**.
Cases created after `as_of` and transactions dated after capture are rejected.
The client's relationship-start date must not follow the UTC calendar date of
`as_of`; equality is allowed because this source field has date precision only.
Current profiles are fixture attributes, not a bitemporal customer-history model.

## Read and integrity boundary

Every build reads the policy-bound manifest and all five files again. The policy
pins the manifest SHA-256, snapshot ID, window and policy ID. Every source has an
exact byte digest and row count. A changed or missing file is `NOT_READY`, never a
cached successful context. Re-reading the manifest detects replacement during
assembly. The retrieval boundary checks its loaded corpus digest against the
prepared digest before publishing a result.

Files must resolve inside the rebuild; per-file limits are 16 MiB, and the
manifest limit is 128 KiB. Duplicate JSON keys, NaN/Infinity and JSON floating
numbers are rejected. All records are validated before client/window selection:
duplicate primary keys, unknown card/client references, mismatched ownership,
invalid timestamps, unconfigured currencies and excess decimal places cause
`CONFLICTING_DATA`. No invalid row is discarded to obtain a successful report.
A missing profile without contradictory transaction/card references, or no
selected transactions, produces `INSUFFICIENT_DATA`; an absent incident produces
`NOT_FOUND`. Missing or malformed identifiers have distinct error codes.

Money remains a decimal string throughout this boundary. RUB and USD scales of
two decimal places are explicitly declared fixture assumptions. Values are never
rounded or converted through float. Other currencies need a new explicit policy.

## Data minimization and remaining limits

The conversion excludes client name, INN, masked PAN, card issue date and operator
assignment. The context retains only client ID, risk category, KYC status,
client-since date, card ID/owner/status, and typed transactions. Counterparty and
purpose remain untrusted source text; downstream generation separately excludes
them from model input. Their presence in a synthetic source context is not a
production free-text disclosure policy.

`full` means that this educational policy found a profile, at least one in-window
transaction and consistent relationships. It does not prove source exhaustiveness,
required production KYC, regulatory sufficiency or expert acceptance. A defect in
any row fails the whole snapshot, including a row for another client.

SHA-256 binding detects drift relative to the selected local policy; it is not a
signature, authorization control or defense against an administrator replacing
both policy and data. Snapshot IDs are assigned by the fixture converter from
source-file digests; no operational immutable snapshot registry or multi-process
database transaction exists yet. New operational data, history, retention rules
and approved policies remain outside this demo boundary.

Targeted tests: `tests/runtime/test_context.py`. Runtime integration tests also
exercise the shipped snapshot and its refusal paths in `test_service.py`.
