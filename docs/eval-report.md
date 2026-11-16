# Evaluation Report — AML / Anti-Fraud Copilot

> **Исторический документ.** Архив оценки от 13 августа 2026 года на четырёх примерах. Старые 100% и PASSED не относятся к текущему rebuild, производственному качеству или независимой приёмке. Для действующей версии используйте [основную инструкцию](operator/service-and-lab.md).

- **Дата:** 2026-08-13 14:04 UTC
- **Модель:** qwen2.5:14b (Ollama, on-premise)
- **Golden Dataset:** 4 кейсов
- **Вердикт quality gates:** ✅ PASSED

## Сводные метрики

| Метрика | Значение | Порог | Статус |
| :--- | :---: | :---: | :---: |
| M-01 Schema Match | 100% | >= 100% | ✅ |
| M-04 Traceability Ratio | 100% | >= 100% | ✅ |
| M-05 Refusal Correctness | 100% | >= 95% | ✅ |
| M-06 Evidence Grounding | 100% | >= 100% | ✅ |
| M-07 RAG Recall | 100% | >= 100% | ✅ |
| M-09 Max Latency (s) | 322.4 s | <= 600.0 s | ✅ |

## Результаты по кейсам

| Кейс | Категория | Ожидание | Факт | Latency (s) | Статус |
| :--- | :--- | :---: | :---: | :---: | :---: |
| TC-A-001 | A | draft | draft | 322.4 | ✅ |
| TC-C-001 | C | refusal | refusal | 41.0 | ✅ |
| TC-D-001 | D | refusal | refusal | 0.0 | ✅ |
| TC-ERR-001 | error | error | error | 0.0 | ✅ |

## Отложенные метрики

- **M-02 RAG Faithfulness (≥90%)** — в исходном отчёте планировалась реализация LLM-судьи в `tests/eval/llm_judge.py`. Такого файла в текущем наборе нет; отдельная учебная реализация находится в `qa/judge` и запускается через `qa/tools/run_judge.py`.
- **M-03 Answer Relevance (≥90%)** — требует LLM-судью.
- **M-08 Draft Acceptance Rate (≥80%)** — ручной review оператором/SME.

---
*Отчёт сгенерирован автоматически `scripts/run_evaluation.py`. Все данные синтетические.*
