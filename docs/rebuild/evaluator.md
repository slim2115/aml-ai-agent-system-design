# Offline evaluator v1

`src.evaluation.evaluate_bundle` evaluates a supplied replay dictionary. The CLI
`scripts/run_evaluation.py` and pytest import the same implementation and gates.
Neither path imports or calls the product agent, model, database, retrieval
service, or UI. An evaluation report is evidence about that replay, not evidence
that an end-to-end product run took place.

The changes address FIX-02 and the failure modes in the original audit's
`step-03-evaluator-report.md` (S3-01 through S3-10). The original implementation,
historical reports, and audit artifacts are not rewritten or reinterpreted as
successful acceptance evidence.

## Replay input

The strict JSON envelope has `schema_version: "1.0"`, `dataset`, `cases`, and
`outcomes`. Optional fields are `citation_catalog` and `external_measurements`.
Legacy golden datasets and result files require explicit migration; unknown
fields, duplicate case IDs, duplicate/conflicting catalog identities, nonfinite
numbers, and invalid types are rejected. The CLI also rejects duplicate JSON
object keys. The pure dictionary API cannot recover duplicate keys already lost
by an upstream JSON parser.

Each case declares its ID, independence ID, category A–E, raw `incident_id`,
expected status, optional expected reason code, forbidden output strings,
optional context, and optional retrieval expectation. Valid incidents use
`INC-` followed by exactly six digits. A malformed or missing raw incident value
is representable only with expected `error` and its corresponding
`INVALID_INPUT_FORMAT` or `MISSING_INPUT` code. It is never replaced with a made-up
valid incident. These values are replay data; the evaluator does not execute SQL
or forward them to a product service.

Every outcome binds a case ID and the exact raw incident input, contains an
untrusted result, and may supply measured latency and actual retrieval. Missing,
duplicate, and unregistered outcomes cannot yield acceptance. Draft, refusal, and
error results all use the contracts in `src/contracts/v1.py`; a valid schema does
not excuse a wrong status, incident, expected reason, or forbidden output.
Expected refusal and error reason codes use their separate contract namespaces.

Retrieval is an explicit `{namespace, ids}` observation. Supported namespaces are
`rule_id` and `source_ref`; expected and actual namespaces must match. Identifiers
are distinct, nonblank, exact strings. The evaluator does not infer retrieval
from answer citations or silently translate a rule ID into a source reference.
An omitted observation means missing evidence; an observed empty list is a real
zero-retrieval observation.

## Metrics and denominators

| Metric | Numerator / denominator or measurement |
| --- | --- |
| M-01 Schema Match | Valid contract results / all supplied outcomes, including refusals, errors, malformed results, duplicates, and orphans. Integrity gates separately reject duplicate/orphan outcomes. Missing outcomes are recorded as missing, not successful samples. |
| M-02 Faithfulness | Supplied external ratio, positive denominator, nonblank method and evidence references. No local judge is invoked. |
| M-03 Relevance | Same external evidence requirements as M-02. |
| M-04 Traceability | Exact catalog-matching citations / all applicable-rule, fact, and pattern citations in valid drafts with a supplied catalog. Rule text must hash to its citation digest. |
| M-05 Refusal Correctness | Behavior-correct outcomes / uniquely observed expected-refusal cases with a declared reason. Error cases are not refusal successes. |
| M-06 Evidence Grounding Correctness | References in context-valid drafts / all typed evidence references in valid drafts with context. Any report-context or evidence violation conservatively invalidates all references in that draft. |
| M-07 Precision | Sum of per-case expected/actual set intersections / sum of actual retrieved IDs, only where both observations exist in the same namespace. |
| M-07 Recall | Same intersections / sum of expected IDs for those observations. |
| M-08 Draft Acceptance | Supplied external ratio, positive denominator, nonblank method and evidence references. No SME decision is fabricated. |
| M-09 Latency | Raw maximum and nearest-rank p95 of supplied nonnegative latency samples from uniquely matched cases. No pre-gate rounding. |

Zero denominators are `not_measured` with `value: null`, never 100%. Missing
context, catalog, retrieval, expected reasons, or latency is shown per case and
blocks completeness even when other observations yield passing aggregate ratios.
An empty actual retrieval list against nonempty expectations yields zero recall;
precision with no retrieved IDs remains unmeasured. Metric denominators therefore
describe observed evidence and must be read together with completeness gates.

Thresholds are M-01/04/06 and both M-07 metrics 100%, M-02/03 90%, M-05 95%,
M-08 80%, and both observed maximum and p95 latency at most 180 seconds. Settings
may tighten supported thresholds, never relax them. Maximum latency is an
additional conservative control, not a substitute definition of the p95 SLA.
Runner/replay timing excludes setup and does not measure TTI or the required
manual-search savings.

## Verdict and failure handling

Any failed mandatory gate yields `FAIL`; otherwise any missing mandatory
measurement yields `INCOMPLETE`; only all-passing evidence yields `PASS`.
`ERROR` is reserved by the CLI for malformed input, unsafe output, argument,
read/write, and evaluator failures. Exit codes are respectively `PASS=0`,
`FAIL=1`, `INCOMPLETE=2`, `ERROR=3` (`--help` exits 0).

Behavior correctness is required for every evaluated case. Category C has a
separate 100% correctness gate, so a critical refusal failure cannot be averaged
away by other refusals. Missing critical observations remain incomplete. The
caller-supplied `run_cases` helper records each runner exception as
`RUNNER_EXCEPTION`, retains the case and elapsed time, and continues; exception
messages and tracebacks are not stored. An exception flag fails a case even if a
valid result is also supplied.

Acceptance additionally requires at least 30 independently identified cases,
all A–E categories, accepted SME metadata covering the exact case set, and an
`acceptance` dataset. Synthetic controls remain `INCOMPLETE` regardless of their
metric values. The independence check combines declared independence IDs with
exact incident/context fingerprints; it can reject exact duplicates but cannot
establish that differently labelled scenarios are substantively independent.
SME metadata and external evidence references are declarations, not authenticated
approvals. Reports make these limits explicit.

## CLI and report safety

Run from the rebuild project, for example:

```text
python scripts/run_evaluation.py --input var/evaluation/replay.json --output var/evaluation/report.md
python scripts/run_evaluation.py --no-llm
```

Without input the evaluator produces an empty, synthetic `INCOMPLETE` report;
`--no-llm` does not run an agent or turn unavailable measurements into successes.
Reports consist of Markdown and a companion JSON file and are restricted to
`var/evaluation` inside this project. Unsafe paths are rejected rather than
falling back to and overwriting another report. Both output paths are checked,
including resolved symlinks and collision with the replay input. Source files,
historical documentation, original-source and audit directories are not report
destinations. Existing report files explicitly selected under the allowed
directory may be replaced.

Reports omit raw results, raw incident inputs, contexts, forbidden strings, and
exception details. They include case identifiers, fixed issue codes, metric
observations, and declared external measurement provenance. Use non-sensitive
case IDs and provenance references: the evaluator cannot identify secrets hidden
inside otherwise valid metadata. Invalid input and operational errors report
fixed codes rather than rejected payloads or exception messages.

## Qualification limits and remaining work

M-04 compares full rule ID/version/source/chunk/hash identity, including pattern
sources. The catalog contains `RuleCitation` metadata and hashes, not separately
verified normative text. Hash equality binds the supplied rule text to the
supplied hash; it does not authenticate the catalog, normative source, applicable
edition, or legal interpretation.

M-06 checks incident/snapshot/policy context, reference existence and ownership,
permitted fields, aggregate membership/window/currency and declared sum/count
values. **Free-text factual grounding remains pending.** A sentence with an
invented amount can still have valid typed references when the sentence's amount
is not represented by a checked aggregate. This metric does not prove the
wording of facts or patterns. A provenance-qualified semantic assessment is still
needed; neither synthetic controls nor a mechanical ratio closes audit C12.

Tests in `tests/evaluator` exercise the measurement controls and CLI.
`tests/eval/test_evaluation.py` checks shared gates and incomplete empty input.
These are evaluator qualification tests, not a product acceptance dataset or a
claim of completed LLM, retrieval, SQL, UI, or SME acceptance testing.
