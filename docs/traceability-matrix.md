# Сквозная матрица трассировки требований (Traceability Matrix)

**Версия:** v0.1 (Draft)
**PoC: AML / Anti-Fraud Copilot**
**Базовые документы:** BRS v0.1, SRS v0.2, Data Model v0.1, Evaluation Plan v0.1, Risks & Security v0.1, ADR-0001…0005
**Дата:** Июль 2026 г.

---

## 1. Назначение и методология

Документ обеспечивает сквозной контроль качества проекта: каждое бизнес-требование прослеживается
до системных требований, артефактов проектирования (диаграммы, модель данных, ADR) и проверяющих
тестов (метрики Evaluation Plan, кейсы Golden Dataset). Это гарантирует, что:

* нет «висящих» требований без реализации и проверки;
* нет тестов, не привязанных к бизнес-ценности;
* любое изменение требования позволяет точно определить затронутые артефакты (impact analysis).

**Направление трассировки:**
м **Прямая (forward):** Бизнес-цель (Goal) → Бизнес-требование (BR) → Системное требование (SR) → Артефакт дизайна → Тест/метрика.
* **Обратная (backward):** Тест Golden Dataset → SR → BR → Goal (раздел 8).

**Условные обозначения:**
* **Design-артефакты:** BPMN (As-Is/To-Be), SEQ (UML Sequence ReAct), ER (Data Model), ADR, JSON (JSON Schema SRS), PROMPT (prompt-спецификация SRS).
* **Методы оценки:** CB = code-based, JUDGE = LLM-as-a-judge, REVIEW = ручной review.

---

## 2. Сквозная трассировка: Goal → BR → SR → Design → Test

### Goal-01 — Снижение OPEX и рост пропускной способности

#### BR-01 [Must] — Автоматическая консолидация данных (Avg Manual Search Time Reduction ≥ 70%)
* **Системные требования:** SR-01 (валидация incident_id), SR-02 (профиль клиента), SR-03 (транзакции), SR-04 (консолидация контекста).
* **Артефакты дизайна:**
  * BPMN To-Be (Фаза 1: Инициация и агрегация).
  * SEQ (Фаза 1: Orchestrator → SQL Tool).
  * ER (сущности Client, Transaction, Card).
  * ADR-0001 (оркестрация LangGraph).
* **Тесты / метрики:**
  * M-09 Latency агрегации (CB, замер) — NFR-08.
  * Golden Dataset: категория A (happy path), категория E (комплексные).

#### BR-02 [Must] — Стандартизация формата отчёта (унифицированные блоки)
* **Системные требования:** SR-05 (found_facts), SR-06 (suspicious_patterns), SR-07 (applicable_rules), SR-08 (валидация схемы).
* **Артефакты дизайна:**
  * JSON (AMLInvestigationDraft, SRS раздел 5).
  * ADR-0003 (Structured Output).
  * ER (сущность Report).
  * SEQ (Фаза 3: генерация черновика).
* **Тесты / метрики:**
  * M-01 Schema Match = 100% (CB) — NFR-01.
  * M-03 Answer Relevance ≥ 90% (JUDGE) — NFR-03.
  * Golden Dataset: категории A, B.

#### BR-06 [Should] — Визуальная подсветка фрагментов данных
* **Системные требования:** SR-15 (передача evidence в UI), SR-16 (UI-подсветка).
* **Артефакты дизайна:**
  * BPMN To-Be (Фаза 6: HITL).
  * ER (сущность Evidence).
  * JSON (evidence_ref).
* **Тесты / метрики:**
  * M-08 Draft Acceptance Rate ≥ 80% (REVIEW) — NFR-06.
  * M-06 Evidence Grounding Correctness = 100% (CB).

### Goal-02 — Качество и прослеживаемость расследований

#### BR-03 [Should] — Соответствие терминологии глоссарию AML
* **Системные требования:** SR-09 (шаблоны формулировок), SR-10 (контроль терминологии).
* **Артефакты дизайна:**
  - PROMPT (prompt-спецификация, SRS раздел 4).
* **Тесты / метрики:**
  * M-03 Answer Relevance ≥ 90% (JUDGE).
  * M-08 Draft Acceptance Rate ≥ 80% (REVIEW).

#### BR-04 [Must] — 100% прослеживаемость вывода до статьи НПА
* **Системные требования:** SR-11 (citation, source_ref), SR-12 (запрет несуществующих ссылок).
* **Артефакты дизайна:**
  * ADR-0004 (Retrieval-first RAG).
  * ER (сущность Rule, regulation_ref).
  * SEQ (Фаза 4: Guardrail сверяет source_ref).
  * JSON (source_ref).
* **Тесты / метрики:**
  * M-04 Traceability Ratio = 100% (CB) — NFR-04.
  * M-07 RAG Precision/Recall = 100% (CB) — NFR-12.
  * Golden Dataset: категория A (expected_applicable_rules).

#### BR-05 [Must] — Фактическая обоснованность (grounding, evidence)
* **Системные требования:** SR-13 (структура evidence), SR-14 (привязка tx_id + field).
* **Артефакты дизайна:**
  * ER (сущность Evidence, evidence_ref).
  * JSON (evidence_ref: tx_id + field).
  * Data Model раздел 5 (детализация evidence_ref).
* **Тесты / метрики:**
  * M-06 Evidence Grounding Correctness = 100% (CB).
  * M-02 RAG Faithfulness ≥ 90% (JUDGE) — NFR-02.
  * Golden Dataset: категория A (expected_evidence_ref).

#### BR-07 [Must] — Read-Only (исключение модификации данных)
* **Системные требования:** SR-17 (read-only учётные записи), SR-18 (отсутствие write-инструментов), SR-19 (guardrail).
* **Артефакты дизайна:**
  * ADR-0002 (Read-Only mode).
  * BPMN To-Be (Lane AI маркирован Read-Only).
  * SEQ (Фаза 4: Read-only guard).
  * Risks (RISK-SEC-03).
* **Тесты / метрики:**
  * M-05 Refusal Correctness (CB) — NFR-05.
  * Adversarial: ADV-01 (запрос действия → refusal).
  * Аудит реестра инструментов (SR-18).

### Goal-03 — Масштабируемость операционной функции

* Трассируется через BR-01 и BR-06 (см. Goal-01): автоматизация рутины и ускорение верификации
  обеспечивают рост пропускной способности без пропорционального найма.
* **Метрики:** Target TTI Reduction ≥ 50% (BRS Operational), SME Escalation Rate ≤ 15%.

---

## 3. Трассировка бизнес-правил (BRULE)

#### BRULE-01 — Human-in-the-Loop (одобрение оператором перед действиями)
* **Системные требования:** SR-22 (автоэкспорт только после HITL-одобрения).
* **Артефакты дизайна:**
  * BPMN To-Be (Фаза 6: валидация; Фаза 7: экспорт после подписания).
  * SEQ (Фаза 6: HITL; Фаза 7: экспорт).
  * ADR-0002 (Read-Only как гарантия HITL).
* **Тесты / метрики:** M-08 Draft Acceptance Rate (REVIEW); ручной review одобрений.

#### BRULE-02 — Запрет генерации несуществующих правил
* **Системные требования:** SR-07 (правила только из базы знаний), SR-12 (запрет несуществующих ссылок).
* **Артефакты дизайна:** ADR-0004 (RAG); ER (Rule); Data Model раздел 4 (база знаний RAG).
* **Тесты / метрики:** M-04 Traceability Ratio = 100% (CB); M-07 RAG Precision/Recall = 100% (CB).

#### BRULE-03 — Явный отказ при недостатке данных
* **Системные требования:** SR-20 (детекция недостаточности), SR-21 (явный отказ + ручная очередь).
* **Артефакты дизайна:**
  * BPMN To-Be (Фаза 2: проверка полноты → отказ).
  * SEQ (Фаза 2: отказ и передача в ручную очередь).
  * JSON (status=refusal, refusal_reason).
* **Тесты / метрики:** M-05 Refusal Correctness ≥ 95% (CB + REVIEW); Golden Dataset категория D (insufficient-data → refusal).

---

## 4. Трассировка нефункциональных требований (NFR → метрика → метод)

* **NFR-01 Schema Match = 100%** → M-01 → CB (jsonschema/pydantic) → SR-08, BR-02.
* **NFR-02 RAG Faithfulness ≥ 90% / 95%** → M-02 → JUDGE (Ragas/DeepEval) → SR-05, SR-13, BR-05.
* **NFR-03 Answer Relevance ≥ 90%** → M-03 → JUDGE → SR-05…SR-07, BR-02, BR-03.
* **NFR-04 Traceability Ratio = 100%** → M-04 → CB (сверка source_ref) → SR-11, SR-12, BR-04.
* **NFR-05 Refusal Correctness ≥ 95% / 100%** → M-05 → CB + REVIEW → SR-19…SR-21, BR-07, BRULE-03.
* **NFR-06 Draft Acceptance Rate ≥ 80%** → M-08 → REVIEW → BR-01…BR-03, BR-06.
* **NFR-07 Latency генерации ≤ 3 мин (p95)** → M-09 → CB (замер) → BRS Constraints.
* **NFR-08 Latency агрегации (≥ 70% сокращение)** → M-09 → CB (замер) → SR-04, BR-01.
* **NFR-09 Маскирование PII = 100%** → CB (проверка логов) → 152-ФЗ, RISK-SEC-02.
* **NFR-10 On-premise / 0 внешних вызовов** → CB (аудит конфигурации) → BRS Constraints, RISK-SEC-04, ADR-0005.
* **NFR-11 Reasoning chain = 100%** → CB (проверка трейсов) → SR-22, RISK-SEC-05.
* **NFR-12 RAG Precision/Recall = 100%** → M-07 → CB → SR-07, SR-12, BR-04, BRULE-02.

---

## 5. Трассировка рисков (Risk → mitigation → тест)

* **RISK-AI-01 (галлюцинация правил, Critical)** → SR-07, SR-11, SR-12 → M-04, M-07 (CB).
* **RISK-AI-02 (галлюцинация фактов, High)** → SR-05, SR-13 → M-02 (JUDGE), M-06 (CB).
* **RISK-AI-03 (ошибочная интерпретация, Medium)** → SR-06, NFR-11 → REVIEW, анализ reasoning_trace.
* **RISK-SEC-01 (prompt injection, Critical)** → SR-19 → M-05 (CB), ADV-01…ADV-03.
* **RISK-SEC-02 (утечка PII, Critical)** → NFR-09 → CB (проверка логов), 152-ФЗ.
* **RISK-SEC-03 (выход за read-only, Critical)** → SR-17, SR-18, SR-19 → M-05 (CB), ADV-01, аудит инструментов.
* **RISK-SEC-04 (утечка через внешние API, High)** → NFR-10 → CB (аудит конфигурации), ADR-0005.
* **RISK-SEC-05 (нарушение аудируемости, Medium)** → NFR-11, SR-22 → CB (полнота трейсов).
* **RISK-BIZ-01 (User Adoption, High)** → SR-16, BR-06 → M-08 (REVIEW).
* **RISK-BIZ-02 (Automation Bias, High)** → BRULE-01 → REVIEW, HITL-аудит.
* **RISK-BIZ-03 (Scope Creep, Medium)** → BRS Out-of-Scope → ревью границ (процесс).

---

## 6. Трассировка архитектурных решений (ADR → требования)

* **ADR-0001 (LangGraph)** → реализует SR-01…SR-22 (узлы графа); обоснован BRS Out-of-Scope (без multi-agent).
* **ADR-0002 (Read-Only)** → BR-07, BRULE-01, SR-17…SR-19, SR-22; закрывает RISK-SEC-01, RISK-SEC-03.
* **ADR-0003 (Structured Output)** → BR-02, SR-08, JSON Schema; обеспечивает M-01 (Schema Match).
* **ADR-0004 (Retrieval-first RAG)** → BR-04, BRULE-02, SR-07/SR-11/SR-12; обеспечивает M-04, M-07; закрывает RISK-AI-01.
* **ADR-0005 (On-premise LLM)** → NFR-10, 152-ФЗ; закрывает RISK-SEC-04.

---

## 7. Покрытие требований (Coverage Summary)

### 7.1 Покрытие бизнес-требований

* **BR-01:** SR ✅ (SR-01…04) | Design ✅ (BPMN, SEQ, ER, ADR-0001) | Test ✅ (M-09, Golden A/E).
* **BR-02:** SR ✅ (SR-05…08) | Design ✅ (JSON, ADR-0003, ER) | Test ✅ (M-01, M-03).
* **BR-03:** SR ✅ (SR-09…10) | Design ✅ (PROMPT) | Test ✅ (M-03, M-08).
* **BR-04:** SR ✅ (SR-11…12) | Design ✅ (ADR-0004, ER, SEQ) | Test ✅ (M-04, M-07).
* **BR-05:** SR ✅ (SR-13…14) | Design ✅ (ER, JSON, Data Model) | Test ✅ (M-06, M-02).
* **BR-06:** SR ✅ (SR-15…16) | Design ✅ (BPMN, ER) | Test ✅ (M-08, M-06).
* **BR-07:** SR ✅ (SR-17…19) | Design ✅ (ADR-0002, BPMN, SEQ) | Test ✅ (M-05, ADV-01).
* **BRULE-01:** SR ✅ (SR-22) | Design ✅ (BPMN, SEQ, ADR-0002) | Test ✅ (M-08, REVIEW).
* **BRULE-02:** SR ✅ (SR-07, SR-12) | Design ✅ (ADR-0004, ER) | Test ✅ (M-04, M-07).
* **BRULE-03:** SR ✅ (SR-20…21) | Design ✅ (BPMN, SEQ, JSON) | Test ✅ (M-05, Golden D).

**Вывод:** 100% бизнес-требований и бизнес-правил имеют системные требования, артефакты дизайна и проверяющие тесты. «Висящих» требований нет.

### 7.2 Покрытие системных требований тестами

* **SR-01…SR-04 (агрегация):** M-09, Golden A/E.
* **SR-05…SR-08 (генерация):** M-01, M-02, M-03, Golden A/B.
* **SR-09…SR-10 (стандартизация):** M-03, M-08.
* **SR-11…SR-12 (нормативная трассировка):** M-04, M-07.
* **SR-13…SR-14 (grounding):** M-06, M-02.
* **SR-15…SR-16 (UI-подсветка):** M-08, M-06.
* **SR-17…SR-19 (read-only):** M-05, ADV-01, аудит.
* **SR-20…SR-21 (отказ):** M-05, Golden D.
* **SR-22 (экспорт):** M-08, REVIEW, NFR-11.

**Вывод:** 100% системных требований (SR-01…SR-22) покрыты хотя бы одной метрикой или тестом.

### 7.3 Покрытие категорий Golden Dataset требованиями

* **Категория A (happy path, 15):** BR-01, BR-02, BR-04, BR-05.
* **Категория B (edge cases, 8):** BR-02, BR-05, BRULE-03.
* **Категория C (adversarial, 7):** BR-07, BRULE-03, RISK-SEC-01.
* **Категория D (insufficient-data → refusal, 6):** BRULE-03, SR-20…21.
* **Категория E (комплексные, 4):** BR-02, NFR-11.

---

## 8. Обратная трассировка (Test → BR → Goal)

Примеры обратных цепочек от тестов Golden Dataset к бизнес-ценности:

* **TC-A-001 (структурирование, draft)** → SR-05…08, SR-11, SR-14 → BR-02, BR-04, BR-05 → Goal-01, Goal-02.
* **TC-C-003 (injection, refusal)** → SR-19 → BR-07 → Goal-02 (защита от регуляторных рисков).
* **TC-D-001 (нет транзакций, refusal)** → SR-20…21 → BRULE-03 → Goal-02 (качество расследования).
* **TC-E-001 (несколько паттернов, draft)** → SR-06, NFR-11 → BR-02 → Goal-01.

---

## 9. Связь с артефактами проекта

* **BRS (docs/brs.md):** источник Goal и BR (разделы 3, 9, 13).
* **SRS (docs/srs.md):** источник SR, NFR, JSON Schema, prompt-спецификации.
* **Data Model (docs/data-model.md):** источник ER-сущностей и формата evidence_ref / RAG.
* **Evaluation Plan (docs/evaluation-plan.md):** источник метрик M-01…M-09 и Golden Dataset.
* **Risks & Security (docs/risks-and-security.md):** источник реестра рисков и adversarial-тестов.
* **ADR (docs/adr/):** источник архитектурных решений и их обоснований.
* **Diagrams (docs/diagrams/):** BPMN As-Is/To-Be, UML Sequence (ReAct), ER.

---

## 10. Статус аналитической фазы

Аналитическая фаза (Фаза 1 по QA.md) завершена. Комплект артефактов:

* **brs.md** — бизнес-требования (Goal-01…03, BR-01…07, BRULE-01…03, метрики).
* **srs.md** — системные требования (SR-01…22, NFR-01…12, prompt-spec, JSON Schema).
* **data-model.md** — ER + data dictionary + формат RAG + evidence_ref.
* **evaluation-plan.md** — Golden Dataset (40 кейсов, категории A–E), метрики, пороги, регрессия.
* **risks-and-security.md** — модель угроз, реестр рисков, adversarial-тесты, 152-ФЗ/115-ФЗ.
* **adr/** — 5 архитектурных решений + индекс.
* **diagrams/** — BPMN As-Is, BPMN To-Be, UML Sequence (ReAct), ER.
* **traceability-matrix.md** — сквозная трассировка (настоящий документ).

**Покрытие:** 100% BR и SR трассированы на дизайн и тесты. Артефакт закрывает требование
QA.md раздел 5.2 P0 (матрица трассировки) и раздел 5.4 (структура docs/).

---

## 11. Открытые вопросы (консолидировано из всех артефактов)

* **Период выборки транзакций** (SRS №2) — фиксированный или настраиваемый; решение SME.
* **Контракт Case Management System** (SRS №5) — формат экспорта и API закрытия кейса (SR-22).
* **Глоссарий AML и шаблоны** (SRS №6) — формат предоставления SME (SR-09/SR-10).
* **Калибровка LLM-судьи** (Evaluation Plan) — выбор модели-судьи и стабильность оценок.
* **Разметка Golden Dataset** (Evaluation Plan) — выделение времени AML SME на 40 кейсов.
* **Классификация критичности данных** (Risks) — перечень полей в LLM-контуре, утверждается Compliance.
* **Порог confidence для приоритизации HITL** (SRS) — при каком значении кейс приоритетный.