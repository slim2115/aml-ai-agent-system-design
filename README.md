# AML / Anti-Fraud Copilot (PoC)

Пет-проект для позиции **Системный аналитик AI-Native** (финтех, AML/anti-fraud).
Интеллектуальный ассистент первичного расследования комплаенс-инцидентов на базе LLM + RAG,
работающий строго в режиме **Read-Only** и на принципах **Human-in-the-Loop**.

## Позиционирование

Системный аналитик AI-решений в финтехе с инженерным бэкграундом QA-автоматизации.
Проектирую требования к LLM-агентам так, чтобы их можно было проверить и измерить:
evaluation-наборы, метрики faithfulness/relevance, guardrails и refusal-политика.

## Аналитические артефакты

- [Бизнес-требования (BRS)](docs/brs.md) — цели, границы, BR-01…BR-06, BRULE-01…03, метрики успеха.
- [Системные требования (SRS)](docs/srs.md) — SR-01…SR-22, NFR-01…NFR-12, prompt-спецификация, JSON Schema.
- [Модель данных (Data Model)](docs/data-model.md) — ER, словарь данных, формат базы знаний для RAG, детализация evidence_ref.

## Диаграммы

- BPMN As-Is: [docs/diagrams/bpmn-as_is_process.bpmn](docs/diagrams/as_is_process.bpmn)
- BPMN To-Be: [docs/diagrams/to_be_process.bpmn](docs/diagrams/to_be_process.bpmn)
- UML Sequence (цикл ReAct): [docs/diagrams/sequence-react-loop.puml](docs/diagrams/sequence-react-loop.puml)
- ER-диаграмма: [docs/diagrams/ER-диаграммаER-диаграмма.puml](docs/diagrams/ER-диаграммаER-диаграмма.puml)

## Статус

Аналитическая фаза (BRS → SRS → Data Model → диаграммы) завершена.
Следующие шаги: evaluation-plan, risks-and-security, ADR, реализация прототипа.

> **Дисклеймер:** Все данные в проекте синтетические. Схема учебная и не содержит реальных банковских данных или ПДн.

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

## Правила ведения

- Каждое решение имеет уникальный номер и статус (Proposed / Accepted / Deprecated / Superseded).
- Решение не удаляется; при замене создаётся новый ADR со ссылкой на заменённый (Superseded by).
- Изменение принятого решения требует новой записи и обновления связей в SRS.