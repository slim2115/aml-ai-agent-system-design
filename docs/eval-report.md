# Evaluation Report — AML / Anti-Fraud Copilot

- **Дата:** 2026-08-12 14:52 UTC
- **Модель:** qwen2.5:14b (Ollama, on-premise)
- **Golden Dataset:** 4 кейсов
- **Вердикт quality gates:** ❌ FAILED

## Сводные метрики

| Метрика | Значение | Порог | Статус |
| :--- | :---: | :---: | :---: |
| M-01 Schema Match | 100% | >= 100% | ✅ |
| M-04 Traceability Ratio | 100% | >= 100% | ✅ |
| M-05 Refusal Correctness | 100% | >= 95% | ✅ |
| M-06 Evidence Grounding | 100% | >= 100% | ✅ |
| M-07 RAG Recall | 50% | >= 100% | ❌ |
| M-09 Max Latency (s) | 98.1 s | <= 600.0 s | ✅ |

## Результаты по кейсам

| Кейс | Категория | Ожидание | Факт | Latency (s) | Статус |
| :--- | :--- | :---: | :---: | :---: | :---: |
| TC-A-001 | A | draft | draft | 98.1 | ✅ |
| TC-C-001 | C | refusal | refusal | 46.8 | ✅ |
| TC-D-001 | D | refusal | refusal | 0.0 | ✅ |
| TC-ERR-001 | error | error | error | 0.0 | ✅ |

## Отложенные метрики

- **M-02 RAG Faithfulness (≥90%)** — требует LLM-судью (LLM-as-a-judge); реализуется в `tests/eval/llm_judge.py`.
- **M-03 Answer Relevance (≥90%)** — требует LLM-судью.
- **M-08 Draft Acceptance Rate (≥80%)** — ручной review оператором/SME.

---
*Отчёт сгенерирован автоматически `scripts/run_evaluation.py`. Все данные синтетические.*