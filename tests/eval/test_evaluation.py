"""Evaluation-тесты: прогон агента по Golden Dataset и расчёт метрик.

Реализует code-based метрики Evaluation Plan (раздел 3):
  M-01 Schema Match, M-04 Traceability, M-05 Refusal Correctness,
  M-06 Evidence Grounding, M-07 RAG Precision/Recall, M-09 Latency.

LLM-as-a-judge метрики (M-02 Faithfulness, M-03 Relevance) и ручной review
(M-08 Draft Acceptance Rate) требуют LLM-судью/SME и вынесены в отдельный шаг.

Тесты с генерацией (draft) помечены маркером `llm` и требуют запущенной Ollama.
Запуск без LLM: pytest -m "not llm".
"""
from __future__ import annotations

import time
from typing import Optional

import pytest

from src.config import get_settings
from src.schemas import validate_draft

# Порог соответствия expected_applicable_rules (доля совпавших source_ref).

def _load_golden_dataset():
    """Загружает Golden Dataset с защитой от отсутствия файла.

    Возвращает пустой список, если файл отсутствует, чтобы не падать
    на этапе collection всего тестового прогона.
    """
    path = __import__("pathlib").Path(__file__).parent.parent / "golden_dataset.json"
    if not path.exists():
        return []
    return __import__("json").loads(path.read_text(encoding="utf-8"))


_GOLDEN_DATASET = _load_golden_dataset()

def _run_case(agent, case: dict) -> dict:
    """Прогоняет кейс через агента и возвращает результат с замером времени."""
    start = time.perf_counter()
    result = agent.run(case["input"]["incident_id"])
    elapsed = time.perf_counter() - start
    return {"case": case, "result": result, "latency": elapsed}


# =============================================================================
# Поведенческие тесты по кейсам (expected_behavior)
# =============================================================================

def _is_llm_case(case: dict) -> bool:
    return case.get("requires_llm", False)


@pytest.mark.parametrize(
    "case",
    argvalues=_GOLDEN_DATASET,
    ids=lambda c: c["id"],
)
def test_case_expected_behavior(agent, case):
    """Каждый кейс даёт ожидаемый тип ответа: draft / refusal / error (M-05)."""
    if _is_llm_case(case):
        pytest.skip("Кейс требует Ollama; запустите с -m llm")

    outcome = agent.run(case["input"]["incident_id"])
    expected = case["expected_behavior"]
    assert outcome["status"] == expected, (
        f"{case['id']}: ожидался {expected}, получен {outcome['status']}"
    )

    # Для refusal проверяем наличие ожидаемой подстроки в причине.
    if expected == "refusal" and "expected_refusal_reason_contains" in case:
        reason = outcome.get("refusal_reason", "")
        assert case["expected_refusal_reason_contains"] in reason


@pytest.mark.llm
@pytest.mark.parametrize(
    "case",
    argvalues=[c for c in _GOLDEN_DATASET if c.get("requires_llm")],
    ids=lambda c: c["id"],
)
def test_case_behavior_with_llm(agent, case):
    """Поведенческий тест для кейсов, требующих генерации (draft, injection)."""
    outcome = agent.run(case["input"]["incident_id"])
    assert outcome["status"] == case["expected_behavior"]

    # Для adversarial-кейсов проверяем отсутствие запрещённых выводов.
    if case["expected_behavior"] == "refusal":
        raw = str(outcome).lower()
        for forbidden in case.get("forbidden_outputs", []):
            assert forbidden.lower() not in raw, f"Запрещённый вывод: {forbidden}"


# =============================================================================
# Quality gates: агрегированные code-based метрики
# =============================================================================

@pytest.mark.llm
@pytest.mark.eval
def test_quality_gates(agent, golden_dataset, knowledge_base, transaction_repository):
    """Расчёт агрегированных метрик и сравнение с порогами (quality gates).

    Прогоняет все кейсы Golden Dataset и вычисляет:
      M-01 Schema Match, M-04 Traceability, M-05 Refusal Correctness,
      M-06 Evidence Grounding, M-07 RAG Recall, M-09 Latency.
    """
    settings = get_settings()
    outcomes = [_run_case(agent, case) for case in golden_dataset]

    schema_total = schema_ok = 0
    trace_total = trace_ok = 0
    refusal_total = refusal_ok = 0
    evidence_total = evidence_ok = 0
    rag_total = rag_ok = 0
    max_latency = 0.0

    for item in outcomes:
        case = item["case"]
        result = item["result"]
        max_latency = max(max_latency, item["latency"])
        status = result.get("status")

        # M-05: Refusal / behavior correctness.
        if case["expected_behavior"] in ("refusal", "error"):
            refusal_total += 1
            if status == case["expected_behavior"]:
                refusal_ok += 1
            continue  # для refusal/error метрики черновика не применяются

        # Далее — только draft-кейсы.
        # M-01: Schema Match.
        schema_total += 1
        try:
            draft = validate_draft(result)
            schema_ok += 1
        except Exception:  # noqa: BLE001
            continue  # невалидная схема — дальнейшие метрики неприменимы

        # M-04: Traceability (все source_ref существуют).
        refs = [r.source_ref for r in draft.applicable_rules]
        refs += [f.source_ref for f in draft.found_facts if f.source_ref]
        trace_total += len(refs)
        trace_ok += sum(1 for ref in refs if knowledge_base.validate_source_ref(ref))

        # M-06: Evidence Grounding (все tx_id существуют).
        evidences = [f.evidence_ref for f in draft.found_facts]
        evidences += [
            p.evidence_ref for p in draft.suspicious_patterns if p.evidence_ref
        ]
        evidence_total += len(evidences)
        evidence_ok += sum(
            1 for e in evidences if transaction_repository.get_by_id(e.tx_id) is not None
        )

        # M-07: RAG Recall (ожидаемые правила присутствуют в ответе).
        expected_rules = set(case.get("expected_applicable_rules", []))
        if expected_rules:
            actual_refs = {r.source_ref for r in draft.applicable_rules}
            rag_total += len(expected_rules)
            rag_ok += len(expected_rules & actual_refs)

    # --- Отчёт и quality gates ---
    def _ratio(ok: int, total: int) -> float:
        return (ok / total) if total else 1.0

    report = {
        "M-01 Schema Match": _ratio(schema_ok, schema_total),
        "M-04 Traceability": _ratio(trace_ok, trace_total),
        "M-05 Refusal Correctness": _ratio(refusal_ok, refusal_total),
        "M-06 Evidence Grounding": _ratio(evidence_ok, evidence_total),
        "M-07 RAG Recall": _ratio(rag_ok, rag_total),
        "M-09 Max Latency (s)": round(max_latency, 2),
    }
    print("\n=== EVALUATION REPORT ===")
    for metric, value in report.items():
        print(f"  {metric}: {value}")

    # Quality gates (пороги из config / Evaluation Plan).
    assert report["M-01 Schema Match"] >= settings.thresholds.schema_match
    assert report["M-04 Traceability"] >= settings.thresholds.traceability
    assert report["M-05 Refusal Correctness"] >= settings.thresholds.refusal_correctness
    assert report["M-06 Evidence Grounding"] == 1.0
    assert report["M-07 RAG Recall"] >= settings.thresholds.rag_recall
    assert report["M-09 Max Latency (s)"] <= settings.performance.generation_timeout_sec