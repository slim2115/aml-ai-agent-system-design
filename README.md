# AML / Anti-Fraud Copilot (PoC)

![Python](https://img.shields.io/badge/Python-3.12-blue)
![On-Premise](https://img.shields.io/badge/Deployment-On--Premise-orange)
![Read-Only](https://img.shields.io/badge/Agent-Read--Only-lightgrey)
![Quality Gates](https://img.shields.io/badge/Quality%20Gates-100%25-brightgreen)
![CI](https://github.com/slim2115/aml-ai-agent-system-design/actions/workflows/ci.yml/badge.svg)

**Пет-проект для позиции Системный аналитик AI-Native (финтех, AML/anti-fraud).**
Проектирую требования к LLM-агентам так, чтобы их можно было проверить и измерить:
evaluation-наборы, метрики faithfulness/relevance, guardrails и refusal-политика.
Разрабатываю прототип интеллектуального ассистента первичного расследования комплаенс-инцидентов
на базе LLM + RAG, работающий строго в режиме **Read-Only** и на принципах **Human-in-the-Loop**.

---

## Дисклеймер (PoC-контур)

> Все данные в проекте являются **синтетическими** и сгенерированы исключительно для демонстрации
> работы прототипа. Схема учебная, не содержит реальных банковских данных или персональных данных
> клиентов. Совпадения с реальными лицами и транзакциями случайны.

### Состав синтетических данных

| Файл | Сущность | Описание |
|---|---|---|
| `data/synthetic/clients.json` | Client | Профили клиентов (риск-категории, KYC) |
| `data/synthetic/transactions.json` | Transaction | Транзакционный лог |
| `data/synthetic/cases.json` | Case | Кейсы/инциденты |
| `data/synthetic/cards.json` | Card | Карты клиентов |
| `data/synthetic/rules.json` | Rule | База знаний комплаенса для RAG |

Генерация выполняется скриптом `scripts/generate_synthetic_data.py` по схеме из
[`docs/data-model.md`](./docs/data-model.md).

### PII

Поля `full_name`, `inn`, `pan_masked` в синтетике являются вымышленными и дополнительно
маскируются в UI и трейсах (NFR-09, 152-ФЗ).

---

## Ключевые возможности

| Функция | Описание | Реализация |
|---|---|---|
| Агрегация контекста | Клиент + транзакции + кейс + карты в единый объект | `InvestigationContextBuilder` |
| RAG-поиск правил | Извлечение релевантных норм из базы знаний | `KnowledgeBase` + ChromaDB |
| Генерация черновика | Структурированный JSON с доказательствами | `ReportGenerator` + Ollama |
| Защитные проверки | Schema / traceability / evidence / injection | `GuardrailPipeline` |
| Умный retry | Повтор только для случайных артефактов, пропуск при injection | LangGraph conditional edges |
| Операторский UI | Подсветка evidence + HITL-кнопки | Streamlit |

---


## Аналитические артефакты

- [Бизнес-требования (BRS)](docs/brs.md) — цели, границы, BR-01…BR-06, BRULE-01…03, метрики успеха.
- [Системные требования (SRS)](docs/srs.md) — SR-01…SR-22, NFR-01…NFR-12, prompt-спецификация, JSON Schema.
- [Модель данных (Data Model)](docs/data-model.md) — ER, словарь данных, формат базы знаний для RAG, детализация evidence_ref.

## Диаграммы

- BPMN As-Is: [docs/diagrams/bpmn-as_is_process.bpmn](docs/diagrams/as_is_process.bpmn)
- BPMN To-Be: [docs/diagrams/to_be_process.bpmn](docs/diagrams/to_be_process.bpmn)
- UML Sequence (цикл ReAct): [docs/diagrams/sequence-react-loop.puml](docs/diagrams/sequence-react-loop.puml)
- ER-диаграмма: [docs/diagrams/ER-диаграммаER-диаграмма.puml](docs/diagrams/ER-диаграммаER-диаграмма.puml)

# Architecture Decision Records (ADR)
## PoC: AML / Anti-Fraud Copilot

Индекс архитектурных решений проекта. Формат записей — по шаблону Майкла Найгарда
(Context → Decision → Alternatives → Consequences).

## Реестр решений

- [ADR-0001](docs/adr/0001-langgraph.md) — Оркестрация агента на LangGraph. Статус: Accepted.
- [ADR-0002](docs/adr/0002-read-only.md) — Режим Read-Only как архитектурное ограничение. Статус: Accepted.
- [ADR-0003](docs/adr/0003-structured-output.md) — Structured Output (JSON Schema) вместо свободного текста. Статус: Accepted.
- [ADR-0004](docs/adr/0004-retrieval-first.md) — Retrieval-first (RAG) как основа генерации правил. Статус: Accepted.
- [ADR-0005](docs/adr/0005-on-premise-llm.md) — Локальная (on-premise) LLM и векторная БД. Статус: Accepted.

# Реестр системных промптов

Промпт является **версионируемым требованием** (QA.md P0; SRS v0.2, раздел 4).
Изменение промпта = изменение требования: требует новой версии файла, записи в changelog
и регрессионного прогона Golden Dataset (Evaluation Plan, раздел 7) до мержа.

## Активные версии

- **system_prompt_v0.2.md** — статус: **Active**. Соответствует SRS v0.2 (раздел 4).
  Добавлены: EVIDENCE MANDATORY (tx_id + field), явная структура JSON-вывода.

## Changelog

- **v0.2** (Июль 2026) — добавлено ограничение EVIDENCE MANDATORY (BR-05, SR-14);
  формализована структура JSON-вывода (ADR-0003); уточнена политика отказа (BRULE-03).
- **v0.1** (черновик) — базовая версия: роль, READ-ONLY, ONLY APPROVED SOURCES,
  NO HALLUCINATION, CITATION MANDATORY, refusal policy, защита от injection.

## Правила версионирования

1. Имя файла: `system_prompt_v{MAJOR}.{MINOR}.md`.
2. Любое изменение поведения промпта повышает версию (минимум MINOR).
3. Старые версии не удаляются — сохраняются для регрессионного сравнения (A/B, Evaluation Plan раздел 7.4).
4. Загрузка промпта в коде — только через `src/agent/prompt_loader.py` (без хардкода строк).
5. Метрики регрессии: Faithfulness, Answer Relevance, Refusal Correctness, Schema Match
   (Evaluation Plan, раздел 3) не должны деградировать относительно baseline.

## Связь с артефактами

- Спецификация промпта: `docs/srs.md`, раздел 4.
- Обоснование structured output: `docs/adr/0003-structured-output.md`.
- Защита от injection: `docs/risks-and-security.md` (RISK-SEC-01), `docs/evaluation-plan.md` (категория C).

## Правила ведения

- Каждое решение имеет уникальный номер и статус (Proposed / Accepted / Deprecated / Superseded).
- Решение не удаляется; при замене создаётся новый ADR со ссылкой на заменённый (Superseded by).
- Изменение принятого решения требует новой записи и обновления связей в SRS.

## Реализация прототипа

### Стек

Python + LangGraph (оркестрация) + Ollama (локальная LLM) + ChromaDB (векторная БД / RAG)
+ Streamlit (UI) + pytest (evaluation). Обоснование выбора — в [ADR](docs/adr/).

### Инструкции

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # заполнить при необходимости

### Структура кода

src/ - исходный код прототипа (модули трассируются на SR из SRS).
prompts/ - версионируемые системные промпты.
data/synthetic/ - синтетические данные (см. дисклеймер).
tests/ - evaluation-тесты и Golden Dataset (см. Evaluation Plan).

### Запуск тестов  
pytest tests/ -v

![CI](https://github.com/slim2115/aml-ai-agent-system-design/actions/workflows/ci.yml/badge.svg)

## Evaluation

Качество агента измеряется прогоном по Golden Dataset (code-based метрики:
Schema Match, Traceability, Refusal Correctness, Evidence Grounding, RAG Recall, Latency).

- Запуск детерминированных тестов в CI: `pytest -m "not llm"` (на каждый push).
- Полный прогон с генерацией отчёта (требуется Ollama):
  ```bash
  python -m scripts.run_evaluation

## Текущий статус

✅ Итерации 1–9 завершены: полный цикл от инцидента до валидного черновика.
- Аналитика: BRS, SRS, Data Model, Evaluation Plan, Risks, ADR, диаграммы.
- Реализация: data_access, retrieval (RAG), generation (Ollama), guardrails, LangGraph-агент.
- Happy-path кейс INC-000123 проходит все защитные проверки.

🔜 Итерация 10: Golden Dataset + evaluation-тесты (метрики M-01…M-09).

Слои pipeline:
Data Access — агрегация данных (клиенты, транзакции, кейсы, карты).
Retrieval — RAG-поиск в базе знаний (ChromaDB, multilingual embedding).
Generation — LLM-генерация черновика с нормализацией артефактов.
Guardrails — 4 уровня защитных проверок + умный retry.
UI — Streamlit с HITL и подсветкой доказательств.
Каждый модуль src/ трассируется на SR/NFR из SRS (матрица: docs/traceability-matrix.md

## Архитектура

```mermaid
graph LR
    A[Incident ID] --> B[build_context]
    B -->|полнота=full| C[generate<br/>RAG + LLM]
    B -->|partial / error| E[refusal]
    C --> D[run_guardrails]
    D -->|passed| F[finalize_draft]
    D -->|failed + schema/traceability| C
    D -->|failed + prompt_injection| E
    D -->|attempts exhausted| E
    F --> G[Draft → HITL]
    E --> H[Refusal / Error]
