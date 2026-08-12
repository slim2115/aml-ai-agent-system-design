"""Unit-тесты контрактов данных (schemas.py).

Проверяют JSON Schema-инварианты (SR-08, NFR-01) и валидацию evidence_ref (SR-14).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.schemas import (
    AMLInvestigationDraft,
    EvidenceRef,
    TransactionField,
    validate_draft,
)


def _valid_draft_payload() -> dict:
    return {
        "incident_id": "INC-000123",
        "status": "draft",
        "generated_at": "2026-07-21T10:15:00Z",
        "data_completeness": "full",
        "found_facts": [
            {
                "fact": "12 транзакций по 95000 RUB",
                "evidence_ref": {"tx_id": "TX-7700", "field": "amount"},
            }
        ],
        "applicable_rules": [
            {
                "rule_id": "R-115-002",
                "rule_text": "Контроль структурирования",
                "source_ref": "115-ФЗ, ст.6, п.2",
            }
        ],
        "confidence": 0.87,
    }


def test_valid_draft_passes():
    """Валидный черновик проходит проверку схемы (AC-08.1)."""
    draft = validate_draft(_valid_draft_payload())
    assert draft.status.value == "draft"
    assert draft.incident_id == "INC-000123"


def test_draft_without_found_facts_fails():
    """status=draft без found_facts нарушает инвариант (AC-08.2)."""
    payload = _valid_draft_payload()
    payload["found_facts"] = []
    with pytest.raises(ValidationError):
        validate_draft(payload)


def test_refusal_requires_reason():
    """status=refusal требует непустой refusal_reason (условный инвариант)."""
    payload = {
        "incident_id": "INC-000999",
        "status": "refusal",
        "generated_at": "2026-07-21T10:20:00Z",
        "data_completeness": "insufficient",
        "refusal_reason": "Недостаточно данных",
        "confidence": 0.0,
    }
    draft = validate_draft(payload)
    assert draft.status.value == "refusal"


def test_refusal_without_reason_fails():
    """status=refusal без refusal_reason отклоняется."""
    payload = {
        "incident_id": "INC-000999",
        "status": "refusal",
        "generated_at": "2026-07-21T10:20:00Z",
        "confidence": 0.0,
    }
    with pytest.raises(ValidationError):
        validate_draft(payload)


def test_invalid_incident_id_pattern_fails():
    """incident_id вне формата INC-NNNNNN отклоняется (SR-01)."""
    payload = _valid_draft_payload()
    payload["incident_id"] = "BAD-ID"
    with pytest.raises(ValidationError):
        validate_draft(payload)


def test_evidence_ref_field_enum():
    """evidence_ref.field принимает только допустимые значения (SR-14)."""
    valid = EvidenceRef(tx_id="TX-7700", field=TransactionField.AMOUNT)
    assert valid.field is TransactionField.AMOUNT

    with pytest.raises(ValidationError):
        EvidenceRef(tx_id="TX-7700", field="недопустимое_поле")


def test_confidence_range():
    """confidence ограничен диапазоном [0, 1]."""
    payload = _valid_draft_payload()
    payload["confidence"] = 1.5
    with pytest.raises(ValidationError):
        validate_draft(payload)