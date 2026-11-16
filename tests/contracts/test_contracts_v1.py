"""Offline v1 acceptance tests using synthetic fixtures and unittest only.

JSON Schema parity covers wire shape. Source resolution, aggregates, temporal
relations, and state transitions are deliberately tested as semantic checks.
"""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from src.contracts import v1
from src.contracts.semantics import (
    ContractViolation,
    canonical_digest,
    check_idempotent_replay,
    classify_incident_id,
    validate_job,
    validate_job_transition,
    validate_report_context,
)


ROOT = Path(__file__).resolve().parents[2]
DIGEST = "a" * 64


def window_payload():
    return {
        "start_inclusive": "2024-02-29T00:00:00Z",
        "end_exclusive": "2024-03-01T00:00:00Z",
        "as_of": "2024-03-01T10:00:00Z",
    }


def context_payload():
    """A complete synthetic context with two exact-decimal transactions."""
    return {
        "schema_version": "1.0",
        "incident_id": "INC-000042",
        "client_id": "synthetic-client-1",
        "snapshot": {
            "snapshot_id": "synthetic-snapshot-1",
            "captured_at": "2024-03-01T12:00:00Z",
            "sources": [{"source": "synthetic-ledger", "version": "v1", "content_sha256": DIGEST}],
        },
        "window": window_payload(),
        "policy_version": "synthetic-policy-v1",
        "data_completeness": "full",
        "consistency": "consistent",
        "missing_fields": [],
        "client": {
            "client_id": "synthetic-client-1",
            "risk_category": "low",
            "kyc_status": "verified",
            "client_since": "2020-02-29",
        },
        "cards": [{"card_id": "synthetic-card-1", "client_id": "synthetic-client-1", "card_status": "active"}],
        "transactions": [
            {
                "tx_id": "synthetic-tx-1", "client_id": "synthetic-client-1",
                "card_id": "synthetic-card-1", "amount": "0.10", "currency": "USD",
                "counterparty": "Synthetic counterparty A", "purpose": "Synthetic fixture",
                "channel": "transfer", "status": "completed", "timestamp": "2024-02-29T00:00:00Z",
            },
            {
                "tx_id": "synthetic-tx-2", "client_id": "synthetic-client-1",
                "card_id": None, "amount": "0.20", "currency": "USD",
                "counterparty": "Synthetic counterparty B", "purpose": "Synthetic fixture",
                "channel": "online", "status": "pending", "timestamp": "2024-02-29T23:59:59.999999Z",
            },
        ],
    }


def citation_payload():
    return {
        "rule_id": "synthetic-rule-1", "rule_version": "v1",
        "source_ref": "synthetic://rules/1", "chunk_id": "synthetic-chunk-1", "content_sha256": DIGEST,
    }


def evidence_payload(kind="transaction"):
    common = {"kind": kind, "snapshot_id": "synthetic-snapshot-1"}
    if kind == "transaction":
        return {**common, "tx_id": "synthetic-tx-1", "field": "amount"}
    if kind == "profile":
        return {**common, "client_id": "synthetic-client-1", "field": "kyc_status"}
    aggregate = {**common, "tx_ids": ["synthetic-tx-1", "synthetic-tx-2"], "window": window_payload()}
    if kind == "sum":
        return {**aggregate, "field": "amount", "currency": "USD", "value": "0.30"}
    if kind == "count":
        return {**aggregate, "value": 2}
    raise ValueError("Unknown synthetic evidence kind")


def draft_payload():
    return {
        "schema_version": "1.0", "status": "draft", "incident_id": "INC-000042",
        "report_id": "synthetic-report-1", "report_version": 1,
        "generated_at": "2024-03-01T12:01:00Z", "snapshot_id": "synthetic-snapshot-1",
        "policy_version": "synthetic-policy-v1", "data_completeness": "full",
        "found_facts": [{"fact": "Synthetic observed transfer.", "evidence": [evidence_payload()], "sources": []}],
        "suspicious_patterns": [],
        "applicable_rules": [{"citation": citation_payload(), "rule_text": "Synthetic policy fixture."}],
        "confidence": 0.75,
    }


def refusal_payload():
    return {
        "schema_version": "1.0", "status": "refusal", "incident_id": "INC-000042",
        "generated_at": "2024-03-01T12:01:00Z", "data_completeness": "insufficient",
        "reason_code": "INSUFFICIENT_DATA", "reason": "Synthetic required input is unavailable.",
        "recommended_action": "manual_review",
    }


def error_payload():
    return {
        "schema_version": "1.0", "status": "error", "generated_at": "2024-03-01T12:01:00Z",
        "correlation_id": "synthetic-correlation-1", "code": "DEPENDENCY_UNAVAILABLE",
        "message": "Synthetic dependency unavailable.", "retryable": True,
    }


def job_payload(status="running", revision=1):
    job = {
        "schema_version": "1.0", "job_id": "synthetic-job-1", "incident_id": "INC-000042",
        "revision": revision, "accepted_at": "2024-03-01T12:00:00Z",
        "deadline_at": "2024-03-01T12:05:00Z", "status": status,
    }
    if status not in ("queued", "running"):
        job["completed_at"] = "2024-03-01T12:05:00Z" if status == "timed_out" else "2024-03-01T12:02:00Z"
    factories = {"succeeded": draft_payload, "refused": refusal_payload, "failed": error_payload}
    if status in factories:
        job["result"] = factories[status]()
    return job


def report_ref_payload():
    return {"report_id": "synthetic-report-1", "report_version": 1, "content_sha256": DIGEST}


def decision_request_payload():
    return {
        "schema_version": "1.0", "report": report_ref_payload(), "expected_revision": 0,
        "action": "approve", "reason": "Synthetic operator review completed.",
    }


def decision_payload():
    return {
        "schema_version": "1.0", "decision_id": "synthetic-decision-1", "incident_id": "INC-000042",
        "report": report_ref_payload(), "previous_revision": 0, "revision": 1,
        "action": "approve", "reason": "Synthetic operator review completed.",
        "actor": {"subject_id": "synthetic-operator-1", "role": "operator"},
        "decided_at": "2024-03-01T12:03:00Z", "audit_event_id": "synthetic-audit-1",
    }


def delivery_request_payload():
    return {
        "schema_version": "1.0", "job_id": "synthetic-job-1", "decision_id": "synthetic-decision-1",
        "report": report_ref_payload(), "destination": "synthetic-export", "operation": "export",
    }


def delivery_payload(status="delivered"):
    delivery = {
        **delivery_request_payload(), "delivery_id": "synthetic-delivery-1", "mode": "local_simulator",
        "revision": 1, "attempt_count": 1, "status": status,
    }
    if status == "delivered":
        delivery["receipt"] = {
            "receipt_id": "synthetic-receipt-1", "delivery_id": delivery["delivery_id"],
            "job_id": delivery["job_id"], "report": report_ref_payload(),
            "destination": delivery["destination"], "operation": delivery["operation"], "mode": delivery["mode"],
            "outcome": "accepted", "received_at": "2024-03-01T12:04:00Z",
        }
    elif status == "failed":
        delivery["error"] = error_payload()
    return delivery


def valid_wire_fixtures():
    return [
        ("InvestigationContext", context_payload()),
        *[("InvestigationResult", f()) for f in (draft_payload, refusal_payload, error_payload)],
        ("ErrorResult", error_payload()),
        ("CreateJobRequest", {"schema_version": "1.0", "incident_id": "INC-000042", "window": window_payload()}),
        *[("Job", job_payload(s)) for s in ("queued", "running", "succeeded", "refused", "failed", "cancelled", "timed_out")],
        ("DecisionRequest", decision_request_payload()), ("DecisionRecord", decision_payload()),
        ("DeliveryRequest", delivery_request_payload()),
        *[("Delivery", delivery_payload(s)) for s in ("pending", "delivered", "failed")],
    ]


def replace_path(payload, path, value):
    changed = deepcopy(payload)
    target = changed
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = value
    return changed


def shape_negative_fixtures():
    """Only JSON-representable shape failures, never cross-field semantics."""
    draft = draft_payload()
    context = context_payload()
    for value in ("", None, 42, "BADID", "INC-１２３４５６", "INC-000042\n", " INC-000042", "INC-000042 "):
        yield "InvestigationResult", replace_path(draft, ("incident_id",), value)
    for value in ("", " ", "synthetic-client-1\n", "café", "client 1", 1, None):
        yield "InvestigationContext", replace_path(context, ("client_id",), value)
    for value in (-0.01, 1.01, "0.5", True):
        yield "InvestigationResult", replace_path(draft, ("confidence",), value)
    for value in ("1", True, 1.5, 0):
        yield "InvestigationResult", replace_path(draft, ("report_version",), value)
    for value in (0.1, 1, True, None, "1e2", "+1.0", "01.0", "1.", ".5", " 1", "1\n", "NaN", "Infinity"):
        yield "InvestigationContext", replace_path(context, ("transactions", 0, "amount"), value)
    for value in ("2024-03-01T12:01:00", "2023-02-29T12:01:00Z", "2024-04-31T12:01:00Z", "2024-03-01T25:01:00Z", "2024-03-01T12:01:00+24:00", "2024-03-01T12:01:00Z\n"):
        yield "InvestigationResult", replace_path(draft, ("generated_at",), value)
    for value in ("2023-02-29", "2024-04-31", "2024-2-29"):
        yield "InvestigationContext", replace_path(context, ("client", "client_since"), value)
    for path in (("extra",), ("found_facts", 0, "extra"), ("found_facts", 0, "evidence", 0, "extra"), ("applicable_rules", 0, "citation", "extra")):
        yield "InvestigationResult", replace_path(draft, path, "forbidden")
    for status in (None, "succeeded", "Draft", 1):
        yield "InvestigationResult", replace_path(draft, ("status",), status)
    missing = deepcopy(draft)
    del missing["status"]
    yield "InvestigationResult", missing
    for field in ("found_facts", "suspicious_patterns", "applicable_rules", "confidence", "report_id"):
        yield "InvestigationResult", {**refusal_payload(), field: draft[field]}
    for path, value in [(("found_facts",), []), (("applicable_rules",), []), (("found_facts", 0, "fact"), " \t"), (("found_facts", 0, "evidence"), []), (("found_facts", 0, "evidence", 0, "kind"), "unknown"), (("found_facts", 0, "evidence", 0, "field"), "client_id")]:
        yield "InvestigationResult", replace_path(draft, path, value)
    yield "InvestigationResult", {**draft, "data_completeness": "partial"}
    yield "ErrorResult", {**error_payload(), "retryable": "true"}
    yield "ErrorResult", {**error_payload(), "code": "UNKNOWN_ERROR"}
    yield "DecisionRequest", {**decision_request_payload(), "actor": decision_payload()["actor"]}
    yield "DecisionRecord", replace_path(decision_payload(), ("actor", "admin"), True)
    yield "DecisionRecord", replace_path(decision_payload(), ("report", "content_sha256"), "A" * 64)
    yield "Job", {**job_payload("running"), "result": draft_payload()}
    yield "Job", {**job_payload("cancelled"), "result": draft_payload()}
    yield "Job", {**job_payload("succeeded"), "result": refusal_payload()}
    yield "Delivery", {**delivery_payload("pending"), "receipt": delivery_payload()["receipt"]}
    yield "Delivery", replace_path(delivery_payload(), ("receipt", "outcome"), "pending")
    for adapter, payload in valid_wire_fixtures():
        yield adapter, {**payload, "unexpected": "forbidden"}
        yield adapter, {**payload, "schema_version": "2.0"}


class WireShapeTests(unittest.TestCase):
    def test_valid_public_contracts_round_trip_without_coercion(self):
        for adapter, payload in valid_wire_fixtures():
            with self.subTest(adapter=adapter, status=payload.get("status")):
                parsed = v1.ADAPTERS[adapter].validate_python(payload)
                self.assertEqual(parsed.model_dump(mode="json"), payload)
                self.assertEqual(v1.ADAPTERS[adapter].validate_json(json.dumps(payload)), parsed)

    def test_all_shape_negative_fixtures_are_rejected(self):
        for index, (adapter, payload) in enumerate(shape_negative_fixtures()):
            with self.subTest(index=index, adapter=adapter):
                with self.assertRaises(ValidationError):
                    v1.ADAPTERS[adapter].validate_python(payload)

    def test_result_discriminators_select_exact_model(self):
        for factory, expected in ((draft_payload, v1.DraftReport), (refusal_payload, v1.RefusalResult), (error_payload, v1.ErrorResult)):
            with self.subTest(expected=expected.__name__):
                self.assertIsInstance(v1.ADAPTERS["InvestigationResult"].validate_python(factory()), expected)

    def test_draft_sections_and_policy_are_required(self):
        for field in ("found_facts", "suspicious_patterns", "applicable_rules", "snapshot_id", "policy_version", "confidence"):
            payload = draft_payload()
            del payload[field]
            with self.subTest(field=field), self.assertRaises(ValidationError):
                v1.DraftReport.model_validate(payload)

    def test_refusal_reason_codes_and_manual_review_action(self):
        codes = ("INSUFFICIENT_DATA", "CONFLICTING_DATA", "NO_APPLICABLE_RULE", "AMBIGUOUS_RULE", "UNTRUSTED_INPUT", "POLICY_NOT_APPROVED", "VALIDATION_FAILED")
        for code in codes:
            self.assertEqual(v1.RefusalResult.model_validate({**refusal_payload(), "reason_code": code}).reason_code, code)
        for field, value in (("reason_code", "OTHER"), ("recommended_action", "approve"), ("reason", "\t\n")):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                v1.RefusalResult.model_validate({**refusal_payload(), field: value})

    def test_nonfinite_confidence_never_enters_contract_or_digest(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            payload = {**draft_payload(), "confidence": value}
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    v1.DraftReport.model_validate(payload)
                with self.assertRaises(ValueError):
                    canonical_digest(payload)

    def test_confidence_closed_interval(self):
        for value in (0.0, 0.25, 1.0):
            self.assertEqual(v1.DraftReport.model_validate({**draft_payload(), "confidence": value}).confidence, value)

    def test_incident_classification_separates_missing_from_malformed(self):
        for value in (None, "", " ", "\t\r\n"):
            with self.subTest(value=value):
                self.assertEqual(classify_incident_id(value), "MISSING_INPUT")
        for value in (42, True, [], {}, "BADID", "INC-１２３４５６", "INC-000042\n", " INC-000042", "INC-000042 "):
            with self.subTest(value=value):
                self.assertEqual(classify_incident_id(value), "INVALID_INPUT_FORMAT")
        self.assertIsNone(classify_incident_id("INC-000042"))

    def test_timezone_offsets_and_leap_day_are_valid(self):
        for timestamp in ("2024-02-29T12:01:00Z", "2024-03-01T15:01:00+03:00", "2024-03-01T07:01:00-05:00", "2024-03-01T12:01:00.123456Z"):
            with self.subTest(timestamp=timestamp):
                self.assertEqual(v1.DraftReport.model_validate({**draft_payload(), "generated_at": timestamp}).generated_at, timestamp)
        self.assertEqual(v1.InvestigationContext.model_validate(context_payload()).client.client_since, "2020-02-29")

    def test_decimal_money_preserves_exact_string_and_scale(self):
        for value in ("0", "-0.00", "0.10", "-12.345678901234567890", "9" * 80):
            payload = context_payload()
            payload["transactions"][0]["amount"] = value
            with self.subTest(value=value):
                self.assertEqual(v1.InvestigationContext.model_validate(payload).transactions[0].amount, value)
        payload["transactions"][0]["amount"] = "9" * 81
        with self.assertRaises(ValidationError):
            v1.InvestigationContext.model_validate(payload)

    def test_nullable_fields_are_required_and_none_is_explicit(self):
        payload = context_payload()
        payload.update(data_completeness="insufficient", client=None, policy_version=None)
        self.assertIsNone(v1.InvestigationContext.model_validate(payload).client)
        for field in ("client", "policy_version"):
            missing = deepcopy(payload)
            del missing[field]
            with self.subTest(field=field), self.assertRaises(ValidationError):
                v1.InvestigationContext.model_validate(missing)
        del payload["transactions"][1]["card_id"]
        with self.assertRaises(ValidationError):
            v1.InvestigationContext.model_validate(payload)

    def test_suspicious_patterns_require_evidence_and_rule_citation(self):
        pattern = {"pattern": "Synthetic pattern.", "evidence": [evidence_payload()], "sources": [citation_payload()]}
        v1.DraftReport.model_validate({**draft_payload(), "suspicious_patterns": [pattern]})
        for field in ("evidence", "sources"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                v1.DraftReport.model_validate({**draft_payload(), "suspicious_patterns": [{**pattern, field: []}]})


class ContextSemanticTests(unittest.TestCase):
    def test_foreign_profile_card_and_transaction_are_rejected(self):
        for path in (("client", "client_id"), ("cards", 0, "client_id"), ("transactions", 0, "client_id")):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                v1.InvestigationContext.model_validate(replace_path(context_payload(), path, "synthetic-foreign-client"))

    def test_unresolved_card_is_rejected_but_cardless_transfer_is_valid(self):
        context = context_payload()
        self.assertIsNone(v1.InvestigationContext.model_validate(context).transactions[1].card_id)
        context["transactions"][1]["card_id"] = "synthetic-unknown-card"
        with self.assertRaises(ValidationError):
            v1.InvestigationContext.model_validate(context)

    def test_duplicate_transaction_card_source_and_missing_field_are_rejected(self):
        for path in (("transactions",), ("cards",), ("snapshot", "sources"), ("missing_fields",)):
            context = context_payload()
            if path == ("missing_fields",):
                context.update(data_completeness="partial", missing_fields=["purpose"])
            target = context
            for part in path:
                target = target[part]
            target.append(deepcopy(target[0]))
            with self.subTest(path=path), self.assertRaises(ValidationError):
                v1.InvestigationContext.model_validate(context)

    def test_window_start_is_inclusive_and_end_is_exclusive(self):
        v1.InvestigationContext.model_validate(context_payload())
        for timestamp in ("2024-02-28T23:59:59.999999Z", "2024-03-01T00:00:00Z"):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValidationError):
                v1.InvestigationContext.model_validate(replace_path(context_payload(), ("transactions", 0, "timestamp"), timestamp))
        context = replace_path(context_payload(), ("transactions", 0, "timestamp"), "2024-02-29T03:00:00+03:00")
        v1.InvestigationContext.model_validate(context)

    def test_window_ordering_and_as_of_are_enforced(self):
        window = window_payload()
        for patch in ({"end_exclusive": window["start_inclusive"]}, {"start_inclusive": window["end_exclusive"]}, {"as_of": "2024-02-29T23:59:59Z"}):
            with self.subTest(patch=patch), self.assertRaises(ValidationError):
                v1.AnalysisWindow.model_validate({**window, **patch})
        v1.AnalysisWindow.model_validate({**window, "as_of": window["end_exclusive"]})

    def test_snapshot_must_not_predate_as_of(self):
        with self.assertRaises(ValidationError):
            v1.InvestigationContext.model_validate(replace_path(context_payload(), ("snapshot", "captured_at"), "2024-03-01T09:59:59Z"))

    def test_full_requires_client_transactions_policy_and_consistency(self):
        for patch in ({"client": None}, {"transactions": []}, {"policy_version": None}, {"missing_fields": ["purpose"]}, {"consistency": "conflicting"}, {"consistency": "unassessed"}):
            with self.subTest(patch=patch), self.assertRaises(ValidationError):
                v1.InvestigationContext.model_validate({**context_payload(), **patch})

    def test_partial_context_can_represent_missing_data_without_claiming_full(self):
        for completeness in ("partial", "insufficient", "unassessed"):
            payload = {**context_payload(), "data_completeness": completeness, "client": None, "transactions": [], "policy_version": None, "missing_fields": ["client"], "consistency": "unassessed"}
            with self.subTest(completeness=completeness):
                self.assertEqual(v1.InvestigationContext.model_validate(payload).data_completeness, completeness)


class ReportSemanticTests(unittest.TestCase):
    def assert_semantic_failure(self, code, report=None, context=None):
        report_model = v1.DraftReport.model_validate(report if report is not None else draft_payload())
        context_model = v1.InvestigationContext.model_validate(context if context is not None else context_payload())
        with self.assertRaises(ContractViolation) as failure:
            validate_report_context(report_model, context_model)
        self.assertEqual(str(failure.exception), code)

    def test_transaction_profile_sum_and_count_resolve_to_snapshot(self):
        report = draft_payload()
        report["found_facts"][0]["evidence"] = [evidence_payload(k) for k in ("transaction", "profile", "sum", "count")]
        report["found_facts"][0]["sources"] = [citation_payload()]
        report["suspicious_patterns"] = [{"pattern": "Synthetic pattern.", "evidence": [evidence_payload("count")], "sources": [citation_payload()]}]
        validate_report_context(v1.DraftReport.model_validate(report), v1.InvestigationContext.model_validate(context_payload()))

    def test_report_identity_policy_and_snapshot_are_bound(self):
        for field, value in (("incident_id", "INC-000043"), ("snapshot_id", "synthetic-other-snapshot"), ("policy_version", "synthetic-policy-v2")):
            with self.subTest(field=field):
                self.assert_semantic_failure("REPORT_CONTEXT_MISMATCH", report={**draft_payload(), field: value})

    def test_ineligible_context_cannot_support_draft(self):
        for patch in ({"data_completeness": "partial"}, {"data_completeness": "partial", "consistency": "conflicting"}):
            with self.subTest(patch=patch):
                self.assert_semantic_failure("CONTEXT_NOT_ELIGIBLE", context={**context_payload(), **patch})

    def test_report_cannot_predate_source_snapshot(self):
        self.assert_semantic_failure("REPORT_PREDATES_SNAPSHOT", report={**draft_payload(), "generated_at": "2024-03-01T11:59:59Z"})

    def test_transaction_and_profile_evidence_must_exist(self):
        for kind, field, code in (("transaction", "tx_id", "UNRESOLVED_TRANSACTION"), ("profile", "client_id", "UNRESOLVED_PROFILE")):
            report = draft_payload()
            report["found_facts"][0]["evidence"] = [{**evidence_payload(kind), field: "synthetic-missing"}]
            with self.subTest(kind=kind):
                self.assert_semantic_failure(code, report=report)

    def test_every_evidence_kind_is_bound_to_snapshot(self):
        for kind in ("transaction", "profile", "sum", "count"):
            report = draft_payload()
            report["found_facts"][0]["evidence"] = [{**evidence_payload(kind), "snapshot_id": "synthetic-other-snapshot"}]
            with self.subTest(kind=kind):
                self.assert_semantic_failure("EVIDENCE_SNAPSHOT_MISMATCH", report=report)

    def test_aggregates_require_existing_members_and_exact_window(self):
        for kind in ("sum", "count"):
            report = draft_payload()
            evidence = evidence_payload(kind)
            evidence["tx_ids"][1] = "synthetic-missing-tx"
            report["found_facts"][0]["evidence"] = [evidence]
            with self.subTest(kind=kind, failure="member"):
                self.assert_semantic_failure("UNRESOLVED_AGGREGATE_MEMBER", report=report)
            evidence = evidence_payload(kind)
            evidence["window"]["start_inclusive"] = "2024-02-28T00:00:00Z"
            report["found_facts"][0]["evidence"] = [evidence]
            with self.subTest(kind=kind, failure="window"):
                self.assert_semantic_failure("AGGREGATE_WINDOW_MISMATCH", report=report)

    def test_cross_currency_sum_is_rejected(self):
        context = replace_path(context_payload(), ("transactions", 1, "currency"), "EUR")
        report = draft_payload()
        report["found_facts"][0]["evidence"] = [evidence_payload("sum")]
        self.assert_semantic_failure("CROSS_CURRENCY_SUM", report, context)

    def test_sum_uses_exact_decimal_including_long_operands(self):
        for amounts, expected in ((["0.10", "0.20"], "0.30"), (["-0.10", "0.20"], "0.10"), (["9" * 60, "-" + "9" * 59], "9" + "0" * 59), (["0." + "0" * 60 + "1", "0." + "0" * 60 + "2"], "0." + "0" * 60 + "3")):
            context, report = context_payload(), draft_payload()
            for tx, amount in zip(context["transactions"], amounts):
                tx["amount"] = amount
            report["found_facts"][0]["evidence"] = [{**evidence_payload("sum"), "value": expected}]
            with self.subTest(amounts=amounts):
                validate_report_context(v1.DraftReport.model_validate(report), v1.InvestigationContext.model_validate(context))

    def test_wrong_sum_and_binary_float_artifact_are_rejected(self):
        for value in ("0.31", "0.30000000000000004"):
            report = draft_payload()
            report["found_facts"][0]["evidence"] = [{**evidence_payload("sum"), "value": value}]
            with self.subTest(value=value):
                self.assert_semantic_failure("AGGREGATE_VALUE_MISMATCH", report)

    def test_aggregate_members_are_distinct_and_count_matches_members(self):
        for model, kind in ((v1.SumEvidence, "sum"), (v1.CountEvidence, "count")):
            evidence = evidence_payload(kind)
            evidence["tx_ids"] = ["synthetic-tx-1", "synthetic-tx-1"]
            with self.subTest(kind=kind), self.assertRaises(ValidationError):
                model.model_validate(evidence)
        with self.assertRaises(ValidationError):
            v1.CountEvidence.model_validate({**evidence_payload("count"), "value": 1})

    def test_rule_citations_cannot_be_unbound_or_duplicated(self):
        report = draft_payload()
        report["applicable_rules"].append(deepcopy(report["applicable_rules"][0]))
        self.assert_semantic_failure("DUPLICATE_RULE_CITATION", report)
        for citation_field in ("rule_id", "rule_version", "source_ref", "chunk_id", "content_sha256"):
            report = draft_payload()
            citation = citation_payload()
            citation[citation_field] = "b" * 64 if citation_field == "content_sha256" else "synthetic-unbound"
            report["found_facts"][0]["sources"] = [citation]
            with self.subTest(field=citation_field):
                self.assert_semantic_failure("UNBOUND_RULE_CITATION", report)

    def test_same_rule_identity_cannot_claim_conflicting_content(self):
        for field, value in (("content_sha256", "b" * 64), ("source_ref", "synthetic://other/rule")):
            report = draft_payload()
            conflicting = deepcopy(report["applicable_rules"][0])
            conflicting["citation"][field] = value
            report["applicable_rules"].append(conflicting)
            with self.subTest(field=field):
                self.assert_semantic_failure("CONFLICTING_RULE_CITATION", report)

    def test_publication_revalidates_mutated_nested_lists(self):
        for target in ("facts", "transactions", "aggregate_members"):
            report_payload = draft_payload()
            report_payload["found_facts"][0]["evidence"] = [evidence_payload("count")]
            report = v1.DraftReport.model_validate(report_payload)
            context = v1.InvestigationContext.model_validate(context_payload())
            if target == "facts":
                report.found_facts.clear()
            elif target == "transactions":
                context.transactions.append(context.transactions[0])
            else:
                report.found_facts[0].evidence[0].tx_ids.append("synthetic-tx-1")
            with self.subTest(target=target), self.assertRaises(ValidationError):
                validate_report_context(report, context)


class JobSemanticTests(unittest.TestCase):
    def assert_job_failure(self, code, payload):
        with self.assertRaises(ContractViolation) as failure:
            validate_job(payload)
        self.assertEqual(str(failure.exception), code)

    def test_all_job_statuses_are_valid_with_matching_result_variant(self):
        for status in ("queued", "running", "succeeded", "refused", "failed", "cancelled", "timed_out"):
            with self.subTest(status=status):
                self.assertEqual(validate_job(job_payload(status)).status, status)

    def test_transition_matrix_and_terminal_immutability(self):
        allowed = {
            "queued": {"running", "cancelled", "timed_out", "failed"},
            "running": {"succeeded", "refused", "failed", "cancelled", "timed_out"},
            "succeeded": set(), "refused": set(), "failed": set(), "cancelled": set(), "timed_out": set(),
        }
        for before in allowed:
            for after in allowed:
                with self.subTest(before=before, after=after):
                    if after in allowed[before]:
                        validate_job_transition(job_payload(before, 3), job_payload(after, 4))
                    else:
                        with self.assertRaises(ContractViolation) as failure:
                            validate_job_transition(job_payload(before, 3), job_payload(after, 4))
                        self.assertEqual(str(failure.exception), "INVALID_STATE")

    def test_transition_revision_must_increment_once(self):
        for revision in (0, 3, 5):
            with self.subTest(revision=revision), self.assertRaises(ContractViolation) as failure:
                validate_job_transition(job_payload("running", 3), job_payload("succeeded", revision))
            self.assertEqual(str(failure.exception), "VERSION_CONFLICT")

    def test_transition_identity_is_immutable(self):
        for field, value in (("job_id", "synthetic-job-2"), ("incident_id", "INC-000043"), ("accepted_at", "2024-03-01T11:59:59Z"), ("deadline_at", "2024-03-01T12:06:00Z")):
            following = {**job_payload("cancelled", 2), field: value}
            with self.subTest(field=field), self.assertRaises(ContractViolation) as failure:
                validate_job_transition(job_payload("running", 1), following)
            self.assertEqual(str(failure.exception), "JOB_IDENTITY_CHANGED")

    def test_success_and_refusal_result_incident_must_match_job(self):
        for status in ("succeeded", "refused"):
            payload = job_payload(status)
            payload["result"]["incident_id"] = "INC-000043"
            with self.subTest(status=status):
                self.assert_job_failure("JOB_INCIDENT_MISMATCH", payload)

    def test_result_at_or_after_deadline_is_late(self):
        for status in ("succeeded", "refused", "failed"):
            for completed_at in ("2024-03-01T12:05:00Z", "2024-03-01T12:05:00.000001Z"):
                with self.subTest(status=status, completed_at=completed_at):
                    self.assert_job_failure("LATE_RESULT", {**job_payload(status), "completed_at": completed_at})
            validate_job({**job_payload(status), "completed_at": "2024-03-01T12:04:59.999999Z"})

    def test_completion_and_result_times_must_belong_to_job_lifetime(self):
        for status in ("succeeded", "refused", "failed", "cancelled", "timed_out"):
            with self.subTest(status=status):
                self.assert_job_failure("COMPLETION_BEFORE_ACCEPTANCE", {**job_payload(status), "completed_at": "2024-03-01T11:59:59Z"})
        for generated_at in ("2024-03-01T11:59:59Z", "2024-03-01T12:02:00.000001Z"):
            payload = job_payload("succeeded")
            payload["result"]["generated_at"] = generated_at
            self.assert_job_failure("RESULT_TIME_MISMATCH", payload)

    def test_timeout_requires_deadline_and_deadline_follows_acceptance(self):
        self.assert_job_failure("PREMATURE_TIMEOUT", {**job_payload("timed_out"), "completed_at": "2024-03-01T12:04:59Z"})
        for deadline in ("2024-03-01T12:00:00Z", "2024-03-01T11:59:59Z"):
            with self.subTest(deadline=deadline), self.assertRaises(ValidationError):
                validate_job({**job_payload(), "deadline_at": deadline})


class DecisionAndDeliveryTests(unittest.TestCase):
    def test_actor_is_server_record_only_and_has_no_extra_fields(self):
        with self.assertRaises(ValidationError):
            v1.DecisionRequest.model_validate({**decision_request_payload(), "actor": decision_payload()["actor"]})
        for patch in ({"admin": True}, {"role": "admin"}, {"subject_id": ""}):
            with self.subTest(patch=patch), self.assertRaises(ValidationError):
                v1.DecisionRecord.model_validate({**decision_payload(), "actor": {**decision_payload()["actor"], **patch}})

    def test_decision_revision_is_exact_successor(self):
        for revision in (1, 8):
            v1.DecisionRecord.model_validate({**decision_payload(), "previous_revision": revision - 1, "revision": revision})
        for previous, revision in ((0, 0), (1, 1), (1, 3), (2, 1)):
            with self.subTest(previous=previous, revision=revision), self.assertRaises(ValidationError):
                v1.DecisionRecord.model_validate({**decision_payload(), "previous_revision": previous, "revision": revision})

    def test_decision_actions_require_reason_and_strict_revision(self):
        for action in ("approve", "reject", "request_changes"):
            self.assertEqual(v1.DecisionRequest.model_validate({**decision_request_payload(), "action": action}).action, action)
        for field, value in (("action", "auto_approve"), ("reason", " "), ("expected_revision", "0"), ("expected_revision", False), ("expected_revision", -1)):
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                v1.DecisionRequest.model_validate({**decision_request_payload(), field: value})

    def test_models_are_frozen_after_validation(self):
        record = v1.DecisionRecord.model_validate(decision_payload())
        with self.assertRaises(ValidationError):
            record.revision = 2
        with self.assertRaises(ValidationError):
            record.actor.role = "supervisor"

    def test_delivery_receipt_matches_every_identity_dimension(self):
        replacements = {
            "delivery_id": "synthetic-delivery-2", "job_id": "synthetic-job-2",
            "destination": "synthetic-other-export", "operation": "manual_queue", "mode": "external",
            "report": {**report_ref_payload(), "report_version": 2},
        }
        v1.ADAPTERS["Delivery"].validate_python(delivery_payload())
        for field, value in replacements.items():
            with self.subTest(field=field), self.assertRaises(ValidationError):
                v1.ADAPTERS["Delivery"].validate_python(replace_path(delivery_payload(), ("receipt", field), value))
        for field, value in (("report_id", "synthetic-report-2"), ("content_sha256", "b" * 64)):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                v1.ADAPTERS["Delivery"].validate_python(replace_path(delivery_payload(), ("receipt", "report", field), value))

    def test_delivered_requires_receipt_and_positive_attempt_count(self):
        payload = delivery_payload()
        del payload["receipt"]
        with self.assertRaises(ValidationError):
            v1.ADAPTERS["Delivery"].validate_python(payload)
        for count in (0, -1, "1", True):
            with self.subTest(count=count), self.assertRaises(ValidationError):
                v1.ADAPTERS["Delivery"].validate_python({**delivery_payload(), "attempt_count": count})

    def test_refusal_can_enter_manual_queue_without_report_or_decision(self):
        request = {**delivery_request_payload(), "operation": "manual_queue", "report": None, "decision_id": None}
        v1.DeliveryRequest.model_validate(request)
        delivery = delivery_payload()
        delivery.update(request)
        delivery["receipt"].update(operation="manual_queue", report=None)
        v1.ADAPTERS["Delivery"].validate_python(delivery)
        for field in ("report", "decision_id"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                v1.DeliveryRequest.model_validate({**delivery_request_payload(), field: None})


class ArtifactAndParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = json.loads((ROOT / "contracts" / "v1" / "schema.json").read_text(encoding="utf-8"))

    def validator(self, adapter):
        return Draft202012Validator(
            {"$schema": "https://json-schema.org/draft/2020-12/schema", "$defs": self.bundle["$defs"], "$ref": f"#/$defs/{adapter}"},
            format_checker=FormatChecker(),
        )

    def test_exported_schema_uses_draft_2020_12_and_all_public_contracts(self):
        self.assertEqual(self.bundle["$schema"], "https://json-schema.org/draft/2020-12/schema")
        Draft202012Validator.check_schema(self.bundle)
        self.assertTrue(set(v1.ADAPTERS).issubset(self.bundle["$defs"]))

    def test_exported_schema_accepts_all_valid_public_contracts(self):
        for adapter, payload in valid_wire_fixtures():
            with self.subTest(adapter=adapter, status=payload.get("status")):
                self.validator(adapter).validate(payload)

    def test_exported_schema_and_pydantic_agree_on_shape_rejections(self):
        for index, (adapter, payload) in enumerate(shape_negative_fixtures()):
            with self.subTest(index=index, adapter=adapter):
                self.assertFalse(self.validator(adapter).is_valid(payload), "Schema accepted invalid wire shape")
                with self.assertRaises(ValidationError):
                    v1.ADAPTERS[adapter].validate_python(payload)

    def test_shape_schema_does_not_claim_to_check_source_resolution(self):
        report = draft_payload()
        report["found_facts"][0]["evidence"][0]["tx_id"] = "synthetic-nonexistent-tx"
        self.validator("InvestigationResult").validate(report)
        parsed = v1.DraftReport.model_validate(report)
        with self.assertRaises(ContractViolation):
            validate_report_context(parsed, v1.InvestigationContext.model_validate(context_payload()))

    def test_generated_artifacts_are_deterministic_and_current(self):
        from src.contracts.export import build_artifacts

        first, second = build_artifacts(), build_artifacts()
        self.assertEqual(first, second)
        self.assertEqual(set(first), {"schema.json", "openapi.json"})
        for filename, generated in first.items():
            with self.subTest(filename=filename):
                published = json.loads((ROOT / "contracts" / "v1" / filename).read_text(encoding="utf-8"))
                self.assertEqual(generated, published)

    def test_digest_and_replay_are_order_independent_but_content_sensitive(self):
        original = {"synthetic": {"b": 2, "a": "тест"}, "values": [1, 2]}
        reordered = {"values": [1, 2], "synthetic": {"a": "тест", "b": 2}}
        self.assertEqual(canonical_digest(original), canonical_digest(reordered))
        self.assertEqual(len(canonical_digest(original)), 64)
        check_idempotent_replay(original, reordered)
        for changed in ({**original, "values": [2, 1]}, {**original, "extra": None}):
            with self.subTest(changed=changed), self.assertRaises(ContractViolation) as failure:
                check_idempotent_replay(original, changed)
            self.assertEqual(str(failure.exception), "IDEMPOTENCY_CONFLICT")


if __name__ == "__main__":
    unittest.main()
