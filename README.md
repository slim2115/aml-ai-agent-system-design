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

- BPMN As-Is: [docs/diagrams/bpmn-asis.bpmn](docs/diagrams/bpmn-asis.bpmn)
- BPMN To-Be: [docs/diagrams/bpmn-tobe.bpmn](docs/diagrams/bpmn-tobe.bpmn)
- UML Sequence (цикл ReAct): [docs/diagrams/sequence-react-loop.puml](docs/diagrams/sequence-react-loop.puml)
- ER-диаграмма: [docs/diagrams/er-data-model.puml](docs/diagrams/er-data-model.puml)

## Статус

Аналитическая фаза (BRS → SRS → Data Model → диаграммы) завершена.
Следующие шаги: evaluation-plan, risks-and-security, ADR, реализация прототипа.

> **Дисклеймер:** Все данные в проекте синтетические. Схема учебная и не содержит реальных банковских данных или ПДн.