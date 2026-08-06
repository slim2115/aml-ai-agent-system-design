"""Запуск evaluation и генерация отчёта (Evaluation Plan, раздел 6).

Прогоняет агента по Golden Dataset, вычисляет code-based метрики
(M-01, M-04, M-05, M-06, M-07, M-09) и генерирует eval-report.md
с фактическими значениями и статусами quality gates.

LLM-as-a-judge метрики (M-02 Faithfulness, M-03 Relevance) и ручной review
(M-08 Draft Acceptance Rate) требуют LLM-судью/SME и в отчёте помечены
как отложенные.

Запуск:
    python -m scripts.run_evaluation              # полный прогон (нужна Ollama)
    python -m scripts.run_evaluation --no-llm     # только детерминированные кейсы
    python -m scripts.run_evaluation --output docs/eval-report.md
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from src.agent import AMLAgent
from src.config import get_settings
from src.data_access import TransactionRepository
from src.data_access.rule_repository import RuleRepository
from src.retrieval import KnowledgeBase
from src.schemas import validate_draft

GOLDEN_DATASET_PATH = Path("tests/golden_dataset.json")


# =============================================================================
# Прогон и метрики
# =============================================================================

def load_golden_dataset(no_llm: bool) -> list[dict]:
    cases = json.loads(GOLDEN_DATASET_PATH.read_text(encoding="utf-8"))
    if no_llm:
        cases = [c for c in cases if not c.get("requires_llm")]
    return cases


def run_case(agent: AMLAgent, case: dict) -> dict:
    start = time.perf_counter()
    result = agent.run(case["input"]["incident_id"])
    latency = time.perf_counter() - start
    return {"case": case, "result": result, "latency": latency}


def compute_metrics(outcomes: list[dict], kb: KnowledgeBase, tx_repo) -> dict:
    """Вычисляет code-based метрики (Evaluation Plan, раздел 3)."""
    schema_total = schema_ok = 0
    trace_total = trace_ok = 0
    refusal_total = refusal_ok = 0
    evidence_total = evidence_ok = 0
    rag_total = rag_ok = 0
    latencies: list[float] = []

    for o in outcomes:
        case, result = o["case"], o["result"]
        latencies.append(o["latency"])
        status = result.get("status")

        # M-05: корректность отказа / поведения.
        if case["expected_behavior"] in ("refusal", "error"):
            refusal_total += 1
            refusal_ok += int(status == case["expected_behavior"])
            continue

        # M-01: Schema Match.
        schema_total += 1
        try:
            draft = validate_draft(result)
            schema_ok += 1
        except Exception:  # noqa: BLE001
            continue  # невалидная схема — остальные метрики неприменимы

        # M-04: Traceability.
        refs = [r.source_ref for r in draft.applicable_rules]
        refs += [f.source_ref for f in draft.found_facts if f.source_ref]
        trace_total += len(refs)
        trace_ok += sum(1 for r in refs if kb.validate_source_ref(r))

        # M-06: Evidence Grounding.
        evidences = [f.evidence_ref for f in draft.found_facts]
        evidences += [p.evidence_ref for p in draft.suspicious_patterns if p.evidence_ref]
        evidence_total += len(evidences)
        evidence_ok += sum(1 for e in evidences if tx_repo.get_by_id(e.tx_id) is not None)

        # M-07: RAG Recall.
        expected = set(case.get("expected_applicable_rules", []))
        if expected:
            actual = {r.source_ref for r in draft.applicable_rules}
            rag_total += len(expected)
            rag_ok += len(expected & actual)

    def ratio(ok: int, total: int) -> float:
        return (ok / total) if total else 1.0

    return {
        "M-01 Schema Match": ratio(schema_ok, schema_total),
        "M-04 Traceability Ratio": ratio(trace_ok, trace_total),
        "M-05 Refusal Correctness": ratio(refusal_ok, refusal_total),
        "M-06 Evidence Grounding": ratio(evidence_ok, evidence_total),
        "M-07 RAG Recall": ratio(rag_ok, rag_total),
        "M-09 Max Latency (s)": round(max(latencies), 1) if latencies else 0.0,
    }


def quality_gates(metrics: dict, settings) -> list[dict]:
    """Сопоставляет метрики с порогами (quality gates)."""
    th = settings.thresholds
    spec = [
        ("M-01 Schema Match", th.schema_match, ">=", False),
        ("M-04 Traceability Ratio", th.traceability, ">=", False),
        ("M-05 Refusal Correctness", th.refusal_correctness, ">=", False),
        ("M-06 Evidence Grounding", 1.0, ">=", False),
        ("M-07 RAG Recall", 1.0, ">=", False),
        ("M-09 Max Latency (s)", settings.performance.generation_timeout_sec, "<=", True),
    ]
    gates = []
    for name, threshold, op, is_time in spec:
        value = metrics[name]
        passed = value <= threshold if op == "<=" else value >= threshold
        gates.append(
            {"name": name, "value": value, "threshold": threshold,
             "op": op, "passed": passed, "is_time": is_time}
        )
    return gates


# =============================================================================
# Генерация отчёта
# =============================================================================

def _fmt(value: float, is_time: bool) -> str:
    return f"{value:.1f} s" if is_time else f"{value:.0%}"


def generate_report(outcomes: list[dict], gates: list[dict], settings, no_llm: bool) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    all_passed = all(g["passed"] for g in gates)
    verdict = "✅ PASSED" if all_passed else "❌ FAILED"

    lines: list[str] = []
    lines.append("# Evaluation Report — AML / Anti-Fraud Copilot")
    lines.append("")
    lines.append(f"- **Дата:** {now}")
    lines.append(f"- **Модель:** {settings.llm.model_name} (Ollama, on-premise)")
    lines.append(f"- **Golden Dataset:** {len(outcomes)} кейсов"
                 + (" (только детерминированные, --no-llm)" if no_llm else ""))
    lines.append(f"- **Вердикт quality gates:** {verdict}")
    lines.append("")

    lines.append("## Сводные метрики")
    lines.append("")
    lines.append("| Метрика | Значение | Порог | Статус |")
    lines.append("| :--- | :---: | :---: | :---: |")
    for g in gates:
        status = "✅" if g["passed"] else "❌"
        lines.append(
            f"| {g['name']} | {_fmt(g['value'], g['is_time'])} "
            f"| {g['op']} {_fmt(g['threshold'], g['is_time'])} | {status} |"
        )
    lines.append("")

    lines.append("## Результаты по кейсам")
    lines.append("")
    lines.append("| Кейс | Категория | Ожидание | Факт | Latency (s) | Статус |")
    lines.append("| :--- | :--- | :---: | :---: | :---: | :---: |")
    for o in outcomes:
        case, result = o["case"], o["result"]
        expected = case["expected_behavior"]
        actual = result.get("status", "?")
        status = "✅" if actual == expected else "❌"
        lines.append(
            f"| {case['id']} | {case['category']} | {expected} | {actual} "
            f"| {o['latency']:.1f} | {status} |"
        )
    lines.append("")

    lines.append("## Отложенные метрики")
    lines.append("")
    lines.append("- **M-02 RAG Faithfulness (≥90%)** — требует LLM-судью (LLM-as-a-judge); "
                 "реализуется в `tests/eval/llm_judge.py`.")
    lines.append("- **M-03 Answer Relevance (≥90%)** — требует LLM-судью.")
    lines.append("- **M-08 Draft Acceptance Rate (≥80%)** — ручной review оператором/SME.")
    lines.append("")
    lines.append("---")
    lines.append("*Отчёт сгенерирован автоматически `scripts/run_evaluation.py`. "
                 "Все данные синтетические.*")
    return "\n".join(lines)


# =============================================================================
# main
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Запуск evaluation и генерация отчёта")
    parser.add_argument("--no-llm", action="store_true",
                        help="Пропустить кейсы, требующие генерации (Ollama)")
    parser.add_argument("--output", default="docs/eval-report.md",
                        help="Путь к файлу отчёта")
    args = parser.parse_args()

    settings = get_settings()
    cases = load_golden_dataset(args.no_llm)

    print(f"Запуск агента по {len(cases)} кейсам Golden Dataset...")
    agent = AMLAgent()
    outcomes = [run_case(agent, case) for case in cases]

    kb = KnowledgeBase(RuleRepository().find_all())
    kb.index()
    tx_repo = TransactionRepository()

    metrics = compute_metrics(outcomes, kb, tx_repo)
    gates = quality_gates(metrics, settings)
    report = generate_report(outcomes, gates, settings, args.no_llm)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")

    all_passed = all(g["passed"] for g in gates)
    print(f"\nОтчёт сохранён: {output_path}")
    print(f"Quality gates: {'PASSED' if all_passed else 'FAILED'}")
    for g in gates:
        mark = "✓" if g["passed"] else "✗"
        print(f"  [{mark}] {g['name']}: {_fmt(g['value'], g['is_time'])} "
              f"({g['op']} {_fmt(g['threshold'], g['is_time'])})")


if __name__ == "__main__":
    main()