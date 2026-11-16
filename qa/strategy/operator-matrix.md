# Матрица QA: оператор, интеграция и модели

Дата: **07.10.2026**, Europe/Moscow. Шаг 1 [плана QA](../../docs/rebuild/qa-practice-plan.md). Машинная версия: [operator-matrix.json](operator-matrix.json).

Это исходная матрица на указанную дату. Актуальный статус последующих работ хранится в progress.json (локальный материал: `qa/progress.json`); новые результаты QA-022 и QA-023 — в отчёте шага 5 (локальный материал: `verification/qa-step-05-2026-10-17/summary.md`). Исторические `KNOWN_LIMITATION` и проекты контрольных кейсов ниже не обновляют текущий статус реализации.

**24 сценария. Новых прогонов здесь нет.** Сохранённые PASS относятся к 05.10.2026; наличие теста не означает текущую успешность или достаточность покрытия. Подмены, границы доказательства и пробелы перечислены отдельно. Файлы runtime, тесты, зависимости и службы этот артефакт не изменяет.

`P0` — критические инварианты данных/решения/доставки/изоляции и достоверности приёмки. `P1` — доступность, диагностика и качество. `SAVED_EVIDENCE_ONLY` — историческое свидетельство; `GAP_WITH_RELATED_TESTS` — новая практика ещё не подтверждена; `KNOWN_LIMITATION` — ограничение review, контрольный case пока только спроектирован. Ни один статус не означает текущий PASS.

Ссылки `test_refs` — pytest selectors существующих функций/методов. Для параметризованных функций selector выбирает всё семейство; отдельные `[param]` варианты не перечисляются. Статическая проверка существования не выполняет тесты.

Статическая сверка 07.10.2026: **24 строки, 69 уникальных selectors, 9 уникальных файлов свидетельств/контекста**. Все ссылки существуют, имена определений найдены в исходниках, selectors сопоставлены с сохранённым JUnit. Это проверка ссылок, а не новый прогон тестов.

| ID | Сценарий | Риск | Состояние доказательства | Следующий этап |
|---|---|---|---|---|
| [QA-001](#qa-001) | Подготовка поиска | P1 | SAVED_EVIDENCE_ONLY | Шаги 2–4: тестовый backend, конечное ожидание, trace ошибки. |
| [QA-002](#qa-002) | Ввод и границы данных | P1 | SAVED_EVIDENCE_ONLY | Шаги 3–4: общие ожидаемые коды и браузерная проверка подсказок. |
| [QA-003](#qa-003) | Приём задания и повтор нажатия | P0 | SAVED_EVIDENCE_ONLY | Шаг 4: submit/retry, сверить единственный job и отображаемое состояние. |
| [QA-004](#qa-004) | Отмена и поздний ответ | P0 | SAVED_EVIDENCE_ONLY | Шаги 4–5: E2E отмена, в БД неизменный terminal; Шаг 7: отдельный реальный model run. |
| [QA-005](#qa-005) | Дедлайн AML-задания | P0 | SAVED_EVIDENCE_ONLY | Шаги 4–5: timeout UI/API; Шаг 7: cold/warm и раздельные интервалы. |
| [QA-006](#qa-006) | Отказ вместо неподтверждённого отчёта | P0 | SAVED_EVIDENCE_ONLY | Шаги 3–4: refusal fixture; Шаг 7: включить реальные отказы в знаменатель. |
| [QA-007](#qa-007) | Факты, суммы и источники | P0 | SAVED_EVIDENCE_ONLY | Шаги 3–4: искажённые QA-копии; Шаги 6 и 8: отдельные числовые и семантические оценки. |
| [QA-008](#qa-008) | Конфликт версии решения | P0 | SAVED_EVIDENCE_ONLY | Шаг 4: два контекста, явная сверка revision и сохранности обоих черновиков. |
| [QA-009](#qa-009) | Осознанное решение оператора | P0 | SAVED_EVIDENCE_ONLY | Шаг 4: synthetic-only решение; Шаг 8: Judge read-only без права решения. |
| [QA-010](#qa-010) | Экспорт и его повтор | P0 | SAVED_EVIDENCE_ONLY | Шаг 4: expect_download, содержание/hash, запрет без одобрения; внешней отправки нет. |
| [QA-011](#qa-011) | Общая история и владение контроллером | P0 | SAVED_EVIDENCE_ONLY | Шаги 4–5: owning process fixture, restart, затем открыть тот же job_id. |
| [QA-012](#qa-012) | API/Swagger и полномочия | P0 | SAVED_EVIDENCE_ONLY | Шаги 2 и 4: Swagger smoke; секрет не включать в trace/evidence. |
| [QA-013](#qa-013) | Kafka → AML → проекция | P0 | SAVED_EVIDENCE_ONLY | Шаги 2 и 5: run_id/event/job/report/offset корреляция на собственных ресурсах. |
| [QA-014](#qa-014) | Дубликат события и конфликт UUID | P0 | SAVED_EVIDENCE_ONLY | Шаг 5: SQL количество/coordinates, DLQ conflicted UUID, исходный отчёт неизменен. |
| [QA-015](#qa-015) | Потеря ACK и восстановление | P0 | SAVED_EVIDENCE_ONLY | Шаг 5: отдельный fault adapter и проверка восстановления из сохранённой очереди. |
| [QA-016](#qa-016) | Poison message и DLQ | P0 | SAVED_EVIDENCE_ONLY | Шаг 5: poison event в собственной теме, offsets и DLQ-содержимое через API/SQL. |
| [QA-017](#qa-017) | Изоляция учебной истории и QA | P0 | GAP_WITH_RELATED_TESTS | Шаг 2: before/after fingerprints ресурсов пользователя, собственные PID/ownership checks. |
| [QA-018](#qa-018) | Качество retrieval и редакций | P1 | GAP_WITH_RELATED_TESTS | Шаг 6: corpus/model digest, размеченный top-k, Recall/Precision и отдельные metamorphic проверки. |
| [QA-019](#qa-019) | Реальная LLM и повторяемость | P1 | GAP_WITH_RELATED_TESTS | Шаг 7: runner с версиями и полными попытками; группировать по case, не считать повторы независимыми. |
| [QA-020](#qa-020) | LLM-as-a-Judge и калибровка | P1 | GAP_WITH_RELATED_TESTS | Шаг 8: отдельный adapter, дефекты, независимая human разметка, false negatives/abstain/agreement; advisory-only. |
| [QA-021](#qa-021) | SME и продуктовая приёмка | P0 | GAP_WITH_RELATED_TESTS | Шаги 3 и 8–10: предоставить формы/эталоны и зарегистрировать реальные независимые оценки. |
| [QA-022](#qa-022) | Повреждённый HTTP и бесконечное откладывание | P1 | KNOWN_LIMITATION | Отдельное проверяемое исправление после QA-профиля; сохранить идемпотентность и диагностическую причину. |
| [QA-023](#qa-023) | Общий дедлайн шага Kafka scheduler | P1 | KNOWN_LIMITATION | Шаг 5: измерить полный tick и fault case; отдельным изменением ввести согласованный бюджет до расширения live fault suite. |
| [QA-024](#qa-024) | Полнота результатов и CI | P1 | GAP_WITH_RELATED_TESTS | Шаг 9: отдельные профили, manifests/traces; push и коммиты делает владелец. |

<a id="qa-001"></a>

## QA-001 — Подготовка поиска

**Риск:** P1. **Ответственный:** QA automation. **Состояние:** SAVED_EVIDENCE_ONLY.

**Сценарий:** Оператор открывает приложение, запускает подготовку, видит ошибку и повторяет её.

**Ожидается:** Подготовка выполняется явно; повторная кнопка отключена в preparing; ошибка безопасна, повтор возможен, ввод сохраняется.

**Слой:** AppTest; worker lifecycle. **Реально:** Streamlit AppTest. **Подмены:** readiness service; store listing.

**Существующие проверки:**

- [`tests/workflow/test_ui_readiness.py::test_preparation_failure_displays_safe_code_and_allows_retry`](../../tests/workflow/test_ui_readiness.py)
- [`tests/workflow/test_ui_readiness.py::test_preparing_disables_duplicate_preparation_without_losing_consent`](../../tests/workflow/test_ui_readiness.py)
- [`tests/workflow/test_ui_readiness.py::test_retry_after_failure_can_reach_ready_and_clears_failure`](../../tests/workflow/test_ui_readiness.py)

**Сохранённые свидетельства/контекст:**

- verification/kafka-aml-2026-10-04/full/unit-tests.xml (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`)
- verification/kafka-aml-2026-10-04/live-cycle.json (локальный материал: `verification/kafka-aml-2026-10-04/live-cycle.json`)

**Граница подтверждения:** AppTest не проверяет браузерный таймер; saved live проверяет успешную реальную подготовку одного запуска.

**Пробел:** Нет Playwright-сценария ошибки подготовки с реальным UI.

**Далее:** Шаги 2–4: тестовый backend, конечное ожидание, trace ошибки.

<a id="qa-002"></a>

## QA-002 — Ввод и границы данных

**Риск:** P1. **Ответственный:** QA automation. **Состояние:** SAVED_EVIDENCE_ONLY.

**Сценарий:** Некорректный инцидент, изменённое окно, неизвестный инцидент и неполный источник.

**Ожидается:** Валидация отличает ошибку ввода, неизвестный инцидент и отсутствие данных; некорректный ввод не вызывает модель.

**Слой:** runtime unit. **Реально:** валидация; синтетические файлы. **Подмены:** embedding; LLM.

**Существующие проверки:**

- [`tests/runtime/test_service.py::test_invalid_input_never_touches_data_or_model`](../../tests/runtime/test_service.py)
- [`tests/runtime/test_service.py::test_missing_source_and_unknown_incident_are_distinct`](../../tests/runtime/test_service.py)
- [`tests/runtime/test_context.py::test_window_boundaries_are_inclusive_then_exclusive`](../../tests/runtime/test_context.py)

**Сохранённые свидетельства/контекст:**

- verification/kafka-aml-2026-10-04/full/unit-tests.xml (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`)

**Граница подтверждения:** Проверяется логика на fixtures, не удобство подсказок junior.

**Пробел:** Нет каталога QA-кейсов, объединяющего UI/API/модель.

**Далее:** Шаги 3–4: общие ожидаемые коды и браузерная проверка подсказок.

<a id="qa-003"></a>

## QA-003 — Приём задания и повтор нажатия

**Риск:** P0. **Ответственный:** QA automation. **Состояние:** SAVED_EVIDENCE_ONLY.

**Сценарий:** Оператор создаёт задание, повторяет запрос с тем же ключом либо создаёт новое при занятом worker.

**Ожидается:** Одно задание на исходный ключ; повтор возвращает исходное подтверждение; занятость не создаёт второй запуск.

**Слой:** controller; AppTest. **Реально:** SQLite; контроллер; AppTest. **Подмены:** worker / synthetic service.

**Существующие проверки:**

- [`tests/workflow/test_controller.py::test_admission_requires_readiness_and_accepted_request_is_replayed_without_rerun`](../../tests/workflow/test_controller.py)
- [`tests/workflow/test_controller.py::test_capacity_rejects_new_work_but_allows_idempotent_replay`](../../tests/workflow/test_controller.py)
- [`tests/workflow/test_ui_guided.py::test_real_submit_persists_new_job_once_after_preparation_and_consent`](../../tests/workflow/test_ui_guided.py)

**Сохранённые свидетельства/контекст:**

- verification/kafka-aml-2026-10-04/full/unit-tests.xml (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`)

**Граница подтверждения:** Повторяемость UI покрыта AppTest, реальные browser double-click и transport не проверены.

**Пробел:** Нет Playwright + SQL корреляции одного задания.

**Далее:** Шаг 4: submit/retry, сверить единственный job и отображаемое состояние.

<a id="qa-004"></a>

## QA-004 — Отмена и поздний ответ

**Риск:** P0. **Ответственный:** QA automation. **Состояние:** SAVED_EVIDENCE_ONLY.

**Сценарий:** Отменить работающий анализ; исполнитель после отмены пытается вернуть результат.

**Ожидается:** Отмена сохраняется до подтверждения; поздний ответ не заменяет terminal; чужой оператор отменять не может.

**Слой:** controller; subprocess. **Реально:** SQLite; дочерний процесс. **Подмены:** синтетический worker без модели.

**Существующие проверки:**

- [`tests/workflow/test_controller.py::test_cancel_is_durable_before_worker_stops_and_cross_user_is_forbidden`](../../tests/workflow/test_controller.py)
- [`tests/workflow/test_worker.py::test_active_cancel_kills_without_late_result`](../../tests/workflow/test_worker.py)

**Сохранённые свидетельства/контекст:**

- verification/kafka-aml-2026-10-04/full/unit-tests.xml (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`)

**Граница подтверждения:** Процессные границы реальны, вычислительная нагрузка модели заменена.

**Пробел:** Нет UI/API/live-model наблюдения отмены.

**Далее:** Шаги 4–5: E2E отмена, в БД неизменный terminal; Шаг 7: отдельный реальный model run.

<a id="qa-005"></a>

## QA-005 — Дедлайн AML-задания

**Риск:** P0. **Ответственный:** QA automation. **Состояние:** SAVED_EVIDENCE_ONLY.

**Сценарий:** Исполнитель зависает или отвечает после истечения времени анализа.

**Ожидается:** Сохраняется timed_out, процесс останавливается, поздний отчёт не публикуется.

**Слой:** controller; subprocess. **Реально:** SQLite; дочерний процесс; таймер. **Подмены:** синтетический исполнитель.

**Существующие проверки:**

- [`tests/workflow/test_controller.py::test_timer_persists_timeout_before_ignoring_worker_returns_and_late_result_cannot_replace_it`](../../tests/workflow/test_controller.py)
- [`tests/workflow/test_worker.py::test_stalled_execution_deadline_kills_and_invalidates`](../../tests/workflow/test_worker.py)

**Сохранённые свидетельства/контекст:**

- verification/kafka-aml-2026-10-04/full/unit-tests.xml (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`)

**Граница подтверждения:** Это дедлайн самого AML-задания, не сквозная очередь Kafka и не общий scheduler tick.

**Пробел:** Нет сквозного замера очереди/выполнения; отдельно открыт QA-023.

**Далее:** Шаги 4–5: timeout UI/API; Шаг 7: cold/warm и раздельные интервалы.

<a id="qa-006"></a>

## QA-006 — Отказ вместо неподтверждённого отчёта

**Риск:** P0. **Ответственный:** QA automation. **Состояние:** SAVED_EVIDENCE_ONLY.

**Сценарий:** Нет допустимых данных либо модель исчерпала корректирующую попытку.

**Ожидается:** Явный отказ/ошибка по контракту; отчёт отсутствует, решение/экспорт не доступны, ручная очередь объяснена.

**Слой:** runtime unit; AppTest. **Реально:** валидаторы; AppTest; SQLite. **Подмены:** LLM; synthetic execution.

**Существующие проверки:**

- [`tests/runtime/test_service.py::test_exhausted_retries_refuse_without_partial_draft`](../../tests/runtime/test_service.py)
- [`tests/workflow/test_ui_guided.py::test_terminal_job_without_report_explains_outcome_and_allows_only_manual_queue`](../../tests/workflow/test_ui_guided.py)

**Сохранённые свидетельства/контекст:**

- verification/kafka-aml-2026-10-04/full/unit-tests.xml (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`)

**Граница подтверждения:** Нет реальной серии отказов Ollama и browser-подтверждения текста.

**Пробел:** Отказ не включён в единый набор учебных browser кейсов.

**Далее:** Шаги 3–4: refusal fixture; Шаг 7: включить реальные отказы в знаменатель.

<a id="qa-007"></a>

## QA-007 — Факты, суммы и источники

**Риск:** P0. **Ответственный:** QA automation + AML SME. **Состояние:** SAVED_EVIDENCE_ONLY.

**Сценарий:** Оператор раскрывает источники; в фикстуре подменены сумма, ссылка или содержимое.

**Ожидается:** Полный точный каталог, Decimal без округления; hash/цитаты связывают отчёт со снимком; при недоступном источнике решение и экспорт закрыты.

**Слой:** runtime unit; AppTest; saved browser observation. **Реально:** код формирования; SQLite; AppTest; браузер в историческом открытии. **Подмены:** модель в unit/AppTest.

**Существующие проверки:**

- [`tests/runtime/test_generation.py::test_complete_catalog_has_exact_money_all_statuses_profile_and_bound_sources`](../../tests/runtime/test_generation.py)
- [`tests/runtime/test_generation.py::test_publication_guard_rejects_fabrication_even_with_valid_evidence_ids`](../../tests/runtime/test_generation.py)
- [`tests/workflow/test_ui_guided.py::test_unavailable_sources_block_review_and_export_but_allow_local_queue`](../../tests/workflow/test_ui_guided.py)

**Сохранённые свидетельства/контекст:**

- verification/kafka-aml-2026-10-04/full/unit-tests.xml (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`)
- verification/kafka-aml-2026-10-04/browser-kafka-report.txt (локальный материал: `verification/kafka-aml-2026-10-04/browser-kafka-report.txt`)
- verification/kafka-aml-2026-10-04/browser-kafka-facts.jpg (локальный материал: `verification/kafka-aml-2026-10-04/browser-kafka-facts.jpg`)

**Граница подтверждения:** Исторический браузер проверял раскрытие сохранённых источников; не доказывает экспертную достаточность.

**Пробел:** Нет Playwright negative fixture и эталонов эксперта.

**Далее:** Шаги 3–4: искажённые QA-копии; Шаги 6 и 8: отдельные числовые и семантические оценки.

<a id="qa-008"></a>

## QA-008 — Конфликт версии решения

**Риск:** P0. **Ответственный:** QA automation. **Состояние:** SAVED_EVIDENCE_ONLY.

**Сценарий:** Два окна читают одну ревизию; другое окно сохраняет решение первым.

**Ожидается:** Устаревшая запись отклоняется; черновик текста сохраняется, требуется явное перечитывание; чужое решение не перезаписано.

**Слой:** AppTest; SQLite CAS. **Реально:** SQLite; AppTest. **Подмены:** исполнитель анализа; искусственная гонка.

**Существующие проверки:**

- [`tests/workflow/test_ui_guided.py::test_changed_revision_requires_explicit_reread_and_keeps_draft`](../../tests/workflow/test_ui_guided.py)
- [`tests/workflow/test_ui_guided.py::test_decision_commit_race_is_rejected_without_overwriting_other_operator`](../../tests/workflow/test_ui_guided.py)
- [`tests/workflow/test_store.py::test_decision_race_one_revision_only_and_changed_replay_conflicts`](../../tests/workflow/test_store.py)

**Сохранённые свидетельства/контекст:**

- verification/kafka-aml-2026-10-04/full/unit-tests.xml (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`)

**Граница подтверждения:** Проверяется конфликт CAS, а не реальная работа двух browser contexts.

**Пробел:** Нет браузерного двухоконного сценария.

**Далее:** Шаг 4: два контекста, явная сверка revision и сохранности обоих черновиков.

<a id="qa-009"></a>

## QA-009 — Осознанное решение оператора

**Риск:** P0. **Ответственный:** QA automation + оператор. **Состояние:** SAVED_EVIDENCE_ONLY.

**Сценарий:** Оператор выбирает решение с основанием; запрос пытается подменить actor/hash.

**Ожидается:** Без явного выбора и причины решение не сохраняется; actor/time задаёт сервис; hash отчёта связан с решением; генератор и Judge не одобряют.

**Слой:** AppTest; store authorization. **Реально:** SQLite; AppTest; trusted principal. **Подмены:** synthetic report.

**Существующие проверки:**

- [`tests/workflow/test_ui_guided.py::test_decision_requires_explicit_choice_and_nonblank_reason`](../../tests/workflow/test_ui_guided.py)
- [`tests/workflow/test_store.py::test_decision_request_cannot_supply_server_fields`](../../tests/workflow/test_store.py)
- [`tests/workflow/test_store.py::test_forged_report_hash_rejected`](../../tests/workflow/test_store.py)

**Сохранённые свидетельства/контекст:**

- verification/kafka-aml-2026-10-04/full/unit-tests.xml (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`)

**Граница подтверждения:** Проверено управление решением, не квалификация оператора и не AML-верность.

**Пробел:** Нет browser полной записи решения; Judge пока отсутствует.

**Далее:** Шаг 4: synthetic-only решение; Шаг 8: Judge read-only без права решения.

<a id="qa-010"></a>

## QA-010 — Экспорт и его повтор

**Риск:** P0. **Ответственный:** QA automation. **Состояние:** SAVED_EVIDENCE_ONLY.

**Сценарий:** Скачать отчёт до/после одобрения, повторить доставку после потери подтверждения.

**Ожидается:** Экспорт требует актуального одобрения и явного подтверждения; байты соответствуют сохранённому отчёту; повтор сохраняет квитанцию.

**Слой:** AppTest; delivery integration. **Реально:** SQLite; локальный симулятор доставки. **Подмены:** исполнитель анализа; граница сбоя.

**Существующие проверки:**

- [`tests/workflow/test_ui_guided.py::test_export_requires_confirmation_and_returns_exact_saved_report`](../../tests/workflow/test_ui_guided.py)
- [`tests/workflow/test_delivery.py::test_export_requires_approval_not_just_any_decision`](../../tests/workflow/test_delivery.py)
- [`tests/workflow/test_delivery.py::test_crash_after_accept_before_publish_recovers_exact_receipt`](../../tests/workflow/test_delivery.py)
- [`tests/workflow/test_delivery.py::test_corrupted_export_bytes_are_detected`](../../tests/workflow/test_delivery.py)

**Сохранённые свидетельства/контекст:**

- verification/kafka-aml-2026-10-04/full/unit-tests.xml (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`)

**Граница подтверждения:** Сохранённый browser run не скачивал файл и не выполнял решение.

**Пробел:** Нет Playwright download и хеш-сверки скачанного файла.

**Далее:** Шаг 4: expect_download, содержание/hash, запрет без одобрения; внешней отправки нет.

<a id="qa-011"></a>

## QA-011 — Общая история и владение контроллером

**Риск:** P0. **Ответственный:** QA automation. **Состояние:** SAVED_EVIDENCE_ONLY.

**Сценарий:** Открыть Kafka-задание в UI после рестарта, затем потерять API.

**Ожидается:** UI читает ту же SQLite историю; не создаёт контроллер, не отменяет активные задания; сохранённый отчёт доступен.

**Слой:** AppTest; saved browser observation. **Реально:** SQLite; AppTest; браузер в исторической проверке. **Подмены:** API в AppTest.

**Существующие проверки:**

- [`tests/workflow/test_ui_integrated.py::test_integrated_page_reads_shared_report_and_never_recovers_active_job`](../../tests/workflow/test_ui_integrated.py)
- [`tests/workflow/test_ui_integrated.py::test_history_and_human_decision_remain_available_with_api_offline`](../../tests/workflow/test_ui_integrated.py)
- [`tests/workflow/test_ui_integrated.py::test_kafka_panel_opens_linked_report_without_model_preparation`](../../tests/workflow/test_ui_integrated.py)

**Сохранённые свидетельства/контекст:**

- verification/kafka-aml-2026-10-04/full/unit-tests.xml (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`)
- verification/kafka-aml-2026-10-04/browser-kafka-report.txt (локальный материал: `verification/kafka-aml-2026-10-04/browser-kafka-report.txt`)
- verification/kafka-aml-2026-10-04/ui-close-check.json (локальный материал: `verification/kafka-aml-2026-10-04/ui-close-check.json`)

**Граница подтверждения:** Исторически открытие после рестарта подтверждено; browser offline сценарий не автоматизирован.

**Пробел:** Нет Playwright истории после управляемого restart.

**Далее:** Шаги 4–5: owning process fixture, restart, затем открыть тот же job_id.

<a id="qa-012"></a>

## QA-012 — API/Swagger и полномочия

**Риск:** P0. **Ответственный:** QA automation. **Состояние:** SAVED_EVIDENCE_ONLY.

**Сценарий:** Неавторизованный запрос, валидный Swagger example, SQL-фильтр с метасимволами.

**Ожидается:** Данные требуют X-Lab-Key до I/O; пример соответствует контракту; параметры связаны безопасно; docs не запускают модель.

**Слой:** in-process HTTP. **Реально:** FastAPI router; валидация; локальные Swagger assets. **Подмены:** PostgreSQL; worker.

**Существующие проверки:**

- [`tests/lab/test_api.py::test_every_documented_data_operation_is_authenticated_before_io`](../../tests/lab/test_api.py)
- [`tests/lab/test_api.py::test_swagger_examples_are_valid_and_match_demo_window`](../../tests/lab/test_api.py)
- [`tests/lab/test_api.py::test_parameterized_filters_do_not_turn_input_into_sql`](../../tests/lab/test_api.py)
- [`tests/lab/test_api.py::test_docs_liveness_and_sql_reads_never_construct_worker`](../../tests/lab/test_api.py)

**Сохранённые свидетельства/контекст:**

- verification/kafka-aml-2026-10-04/full/unit-tests.xml (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`)

**Граница подтверждения:** TestClient не проверяет живой сетевой порт или клики Swagger.

**Пробел:** Нет browser Authorize → Execute с отдельным QA-ключом.

**Далее:** Шаги 2 и 4: Swagger smoke; секрет не включать в trace/evidence.

<a id="qa-013"></a>

## QA-013 — Kafka → AML → проекция

**Риск:** P0. **Ответственный:** QA automation. **Состояние:** SAVED_EVIDENCE_ONLY.

**Сценарий:** Публикация нового события и чтение окончательного отчёта через общий UI.

**Ожидается:** Kafka ACK означает публикацию; offset после durable inbox; job принят отдельно; report/hash совпадают; автоматического одобрения нет.

**Слой:** unit boundaries; saved live integration. **Реально:** в saved live: HTTP; Kafka; PostgreSQL; SQLite; embedding; template. **Подмены:** broker/PG/HTTP в unit.

**Существующие проверки:**

- [`tests/lab/test_kafka_analysis_api.py::test_publication_is_ack_only_and_has_no_aml_side_effects`](../../tests/lab/test_kafka_analysis_api.py)
- [`tests/lab/test_kafka_analysis_consumer.py::test_intake_commits_durable_queue_before_offset_without_http`](../../tests/lab/test_kafka_analysis_consumer.py)

**Сохранённые свидетельства/контекст:**

- verification/kafka-aml-2026-10-04/full/unit-tests.xml (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`)
- verification/kafka-aml-2026-10-04/live-cycle.json (локальный материал: `verification/kafka-aml-2026-10-04/live-cycle.json`)
- verification/kafka-aml-2026-10-04/live-request.json (локальный материал: `verification/kafka-aml-2026-10-04/live-request.json`)

**Граница подтверждения:** Один исторический live путь с template; не серия аварий и не Ollama.

**Пробел:** Нет изолированного повторяемого live QA-профиля.

**Далее:** Шаги 2 и 5: run_id/event/job/report/offset корреляция на собственных ресурсах.

<a id="qa-014"></a>

## QA-014 — Дубликат события и конфликт UUID

**Риск:** P0. **Ответственный:** QA automation. **Состояние:** SAVED_EVIDENCE_ONLY.

**Сценарий:** Повторить тот же payload, затем тот же UUID с изменённым содержимым.

**Ожидается:** Точный повтор не создаёт второе задание; новая Kafka coordinate учтена; изменённый payload не заменяет исходный inbox.

**Слой:** store unit; consumer unit; saved live integration. **Реально:** в saved live: Kafka; PostgreSQL; API; SQLite. **Подмены:** PG/broker в unit.

**Существующие проверки:**

- [`tests/lab/test_kafka_analysis_store.py::test_redelivery_and_process_restart_keep_one_request_and_record_new_coordinates`](../../tests/lab/test_kafka_analysis_store.py)
- [`tests/lab/test_kafka_analysis_store.py::test_changed_uuid_body_is_rejected_before_any_submission`](../../tests/lab/test_kafka_analysis_store.py)
- [`tests/lab/test_kafka_analysis_consumer.py::test_uuid_changed_payload_is_dead_lettered_without_replacing_queued_request`](../../tests/lab/test_kafka_analysis_consumer.py)

**Сохранённые свидетельства/контекст:**

- verification/kafka-aml-2026-10-04/full/unit-tests.xml (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`)
- verification/kafka-aml-2026-10-04/live-cycle.json (локальный материал: `verification/kafka-aml-2026-10-04/live-cycle.json`)

**Граница подтверждения:** Live подтвердил точный повтор и один job; конфликт payload живым брокером отдельно не проверен.

**Пробел:** Нет live негативного UUID conflict.

**Далее:** Шаг 5: SQL количество/coordinates, DLQ conflicted UUID, исходный отчёт неизменен.

<a id="qa-015"></a>

## QA-015 — Потеря ACK и восстановление

**Риск:** P0. **Ответственный:** QA automation. **Состояние:** SAVED_EVIDENCE_ONLY.

**Сценарий:** API приняло job, но ответ потерян; worker перезапущен без новых сообщений.

**Ожидается:** Тот же idempotency key, один job; очередь продолжает polling; terminal не регенерируется и ревизия не откатывается.

**Слой:** component integration; store unit. **Реально:** SQLite; реальный WorkflowClient поверх транспорта-подмены. **Подмены:** HTTP transport; PG queue; Kafka; execution.

**Существующие проверки:**

- [`tests/lab/test_kafka_analysis_consumer.py::test_real_sqlite_admission_survives_ambiguous_ack_and_projects_without_new_kafka`](../../tests/lab/test_kafka_analysis_consumer.py)
- [`tests/lab/test_kafka_analysis_consumer.py::test_terminal_job_outcomes_are_projected_without_regeneration`](../../tests/lab/test_kafka_analysis_consumer.py)
- [`tests/lab/test_kafka_analysis_store.py::test_delayed_acceptance_from_one_observer_cannot_regress_another_observers_result`](../../tests/lab/test_kafka_analysis_store.py)

**Сохранённые свидетельства/контекст:**

- verification/kafka-aml-2026-10-04/full/unit-tests.xml (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`)

**Граница подтверждения:** Контроль сбоя детерминированный, реальные аварии процессов/сети не выполнялись.

**Пробел:** Нет живого управляемого падения между ACK/PG projection.

**Далее:** Шаг 5: отдельный fault adapter и проверка восстановления из сохранённой очереди.

<a id="qa-016"></a>

## QA-016 — Poison message и DLQ

**Риск:** P0. **Ответственный:** QA automation. **Состояние:** SAVED_EVIDENCE_ONLY.

**Сценарий:** Повреждённое событие, неверный key, огромный payload или недоступная DLQ.

**Ожидается:** DLQ содержит ограниченную диагностическую копию; input offset только после DLQ ACK; при ошибке offset не продвигается.

**Слой:** consumer unit. **Реально:** контрактная валидация; код consumer. **Подмены:** Kafka; PostgreSQL.

**Существующие проверки:**

- [`tests/lab/test_kafka_analysis_consumer.py::test_invalid_event_dlq_ack_precedes_input_commit`](../../tests/lab/test_kafka_analysis_consumer.py)
- [`tests/lab/test_kafka_analysis_consumer.py::test_dlq_is_bounded_even_for_huge_key_and_value`](../../tests/lab/test_kafka_analysis_consumer.py)
- [`tests/lab/test_kafka_analysis_consumer.py::test_dlq_failure_does_not_commit_input`](../../tests/lab/test_kafka_analysis_consumer.py)
- [`tests/lab/test_kafka_analysis_consumer.py::test_database_failure_stops_before_later_offset_and_never_uses_http`](../../tests/lab/test_kafka_analysis_consumer.py)

**Сохранённые свидетельства/контекст:**

- verification/kafka-aml-2026-10-04/full/unit-tests.xml (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`)

**Граница подтверждения:** Порядок ACK/commit проверен трассой подмен, не живым consumer group.

**Пробел:** Нет live DLQ/error recovery упражнения в QA namespace.

**Далее:** Шаг 5: poison event в собственной теме, offsets и DLQ-содержимое через API/SQL.

<a id="qa-017"></a>

## QA-017 — Изоляция учебной истории и QA

**Риск:** P0. **Ответственный:** QA automation + разработчик. **Состояние:** GAP_WITH_RELATED_TESTS.

**Сценарий:** Запустить и очистить QA, сохранив пользовательские БД, offset и процессы.

**Ожидается:** QA имеет отдельные DB/role/SQLite/порты/topics/groups/namespace; очищает и останавливает только свои ресурсы.

**Слой:** boundary controls; planned QA lifecycle. **Реально:** валидация путей/профилей. **Подмены:** PostgreSQL; Kafka client.

**Существующие проверки:**

- [`tests/lab/test_kafka_analysis_api.py::test_broker_profile_isolates_topic_and_group`](../../tests/lab/test_kafka_analysis_api.py)
- [`tests/lab/test_kafka_analysis_store.py::test_foreign_or_partial_namespace_is_never_overwritten_or_granted`](../../tests/lab/test_kafka_analysis_store.py)
- [`tests/workflow/test_store.py::test_path_escape_rejected_before_writing`](../../tests/workflow/test_store.py)

**Сохранённые свидетельства/контекст:**

- verification/kafka-aml-2026-10-04/full/unit-tests.xml (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`)

**Граница подтверждения:** Существующие защиты разделяют bookkeeping/AML и запрещают чужой namespace; полноценного QA-профиля нет.

**Пробел:** Полная изоляция QA не доказана и ещё не реализована.

**Далее:** Шаг 2: before/after fingerprints ресурсов пользователя, собственные PID/ownership checks.

<a id="qa-018"></a>

## QA-018 — Качество retrieval и редакций

**Риск:** P1. **Ответственный:** ML QA + AML SME. **Состояние:** GAP_WITH_RELATED_TESTS.

**Сценарий:** Изменить дату действия правила, corpus order; измерить top-k на размеченных запросах.

**Ожидается:** Версия/цитата соответствуют окну; ranking качество измеряется отдельно от полноты получения всех активных правил.

**Слой:** retrieval unit; evaluator. **Реально:** код поиска/редакций; расчёт метрик. **Подмены:** embedding vectors; сохранённые ответы.

**Существующие проверки:**

- [`tests/runtime/test_retrieval.py::test_explicit_edition_boundary_and_timezone_are_applied_before_top_k`](../../tests/runtime/test_retrieval.py)
- [`tests/runtime/test_retrieval.py::test_ranking_is_not_an_applicability_or_legal_approval_claim`](../../tests/runtime/test_retrieval.py)
- [`tests/evaluator/test_core.py::EvaluatorControls::test_retrieval_is_independent_of_answer_citations`](../../tests/evaluator/test_core.py)

**Сохранённые свидетельства/контекст:**

- verification/kafka-aml-2026-10-04/full/unit-tests.xml (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`)
- verification/kafka-aml-2026-10-04/live-cycle.json (локальный материал: `verification/kafka-aml-2026-10-04/live-cycle.json`)

**Граница подтверждения:** Fake vectors не измеряют релевантность; один live embedding run не benchmark ranking.

**Пробел:** Нет набора релевантности и повторяемого реального retrieval benchmark.

**Далее:** Шаг 6: corpus/model digest, размеченный top-k, Recall/Precision и отдельные metamorphic проверки.

<a id="qa-019"></a>

## QA-019 — Реальная LLM и повторяемость

**Риск:** P1. **Ответственный:** ML QA. **Состояние:** GAP_WITH_RELATED_TESTS.

**Сценарий:** Прогнать применимые кейсы template/local-model, по 3 повтора на 10 кейсах.

**Ожидается:** Факты/правила неизменны; допустим порядок; учитываются все первые ошибки, retries, отказы, timeout, cold/warm latency.

**Слой:** generation unit; planned model evaluation. **Реально:** валидаторы; готовые факты. **Подмены:** ответы LLM; embedding.

**Существующие проверки:**

- [`tests/runtime/test_generation.py::test_model_may_reorder_all_facts_but_cannot_select_a_subset`](../../tests/runtime/test_generation.py)
- [`tests/runtime/test_generation.py::test_untrusted_source_text_is_not_a_command_and_does_not_change_draft`](../../tests/runtime/test_generation.py)
- [`tests/runtime/test_service.py::test_invalid_selection_retries_once_with_safe_feedback_and_shared_deadline`](../../tests/runtime/test_service.py)

**Сохранённые свидетельства/контекст:**

- verification/kafka-aml-2026-10-04/full/unit-tests.xml (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`)

**Граница подтверждения:** Unit доказывает ограничения генерации, не статистическое качество Qwen/Ollama; temperature=0 не доказательство воспроизводимости.

**Пробел:** Нет реального пилота повторов и статистики.

**Далее:** Шаг 7: runner с версиями и полными попытками; группировать по case, не считать повторы независимыми.

<a id="qa-020"></a>

## QA-020 — LLM-as-a-Judge и калибровка

**Риск:** P1. **Ответственный:** ML QA + AML SME. **Состояние:** GAP_WITH_RELATED_TESTS.

**Сценарий:** Judge оценивает эталон и копии с дефектом, затем варианты в порядке A/B и B/A.

**Ожидается:** JSON/ссылки валидны; pass/fail/abstain; известные ошибки выявляются; Judge без инструментов/решения; числовой FAIL нельзя компенсировать баллом.

**Слой:** evaluator guards; planned Judge. **Реально:** проверка completeness внешних измерений. **Подмены:** готовые external metric records; Judge отсутствует.

**Существующие проверки:**

- [`tests/evaluator/test_core.py::EvaluatorControls::test_external_missing_evidence_or_denominator_is_not_measured`](../../tests/evaluator/test_core.py)
- [`tests/evaluator/test_core.py::EvaluatorControls::test_free_text_grounding_limitation_is_disclosed`](../../tests/evaluator/test_core.py)

**Сохранённые свидетельства/контекст:**

- verification/kafka-aml-2026-10-04/full/unit-tests.xml (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`)

**Граница подтверждения:** Связанные тесты проверяют evaluator, не Judge. Действующего Judge и его результатов нет.

**Пробел:** Нет adapter/rubric/калибровки/holdout/оценки смещений.

**Далее:** Шаг 8: отдельный adapter, дефекты, независимая human разметка, false negatives/abstain/agreement; advisory-only.

<a id="qa-021"></a>

## QA-021 — SME и продуктовая приёмка

**Риск:** P0. **Ответственный:** AML SME + владелец продукта. **Состояние:** GAP_WITH_RELATED_TESTS.

**Сценарий:** Сопоставить отчёты с человеческой разметкой и полнотой acceptance dataset.

**Ожидается:** M-08 остаётся человеческой оценкой; обязательны >=30 независимых случаев A–E и подтверждение SME точного состава; недостающие данные → INCOMPLETE.

**Слой:** acceptance gate. **Реально:** код evaluator. **Подмены:** synthetic evaluation bundle; метаданные в fixtures.

**Существующие проверки:**

- [`tests/evaluator/test_core.py::EvaluatorControls::test_synthetic_baseline_never_proves_acceptance`](../../tests/evaluator/test_core.py)
- [`tests/evaluator/test_core.py::EvaluatorControls::test_independence_and_category_coverage_are_required`](../../tests/evaluator/test_core.py)
- [`tests/evaluator/test_core.py::EvaluatorControls::test_sme_metadata_is_required_and_never_fabricated`](../../tests/evaluator/test_core.py)

**Сохранённые свидетельства/контекст:**

- verification/kafka-aml-2026-10-04/full/unit-tests.xml (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`)

**Граница подтверждения:** Правила gate проверены; это не подпись SME и не реальное принятие оператором.

**Пробел:** Экспертная приёмка/полный acceptance набор текущим QA этапом не подтверждены.

**Далее:** Шаги 3 и 8–10: предоставить формы/эталоны и зарегистрировать реальные независимые оценки.

<a id="qa-022"></a>

## QA-022 — Повреждённый HTTP и бесконечное откладывание

**Риск:** P1. **Ответственный:** разработчик + QA automation. **Состояние:** KNOWN_LIMITATION.

**Сценарий:** API стабильно возвращает HTTP 200 с malformed JSON либо неверной формой job.

**Ожидается:** Целевое поведение: ошибка протокола отличима от временной неготовности, retry ограничен/эскалирован без потери запроса и без нового job.

**Слой:** review finding; existing unit boundaries; planned regression. **Реально:** WorkflowClient parser; scheduler code. **Подмены:** HTTP response; queue.

**Существующие проверки:**

- [`tests/workflow/test_remote_workflow.py::test_untrusted_responses_fail_closed`](../../tests/workflow/test_remote_workflow.py)
- [`tests/workflow/test_remote_workflow.py::test_poll_rejects_invalid_result_shape_or_semantics`](../../tests/workflow/test_remote_workflow.py)
- [`tests/lab/test_kafka_analysis_consumer.py::test_temporary_workflow_failure_keeps_queue_and_rotates_fairly`](../../tests/lab/test_kafka_analysis_consumer.py)

**Сохранённые свидетельства/контекст:**

- verification/kafka-aml-2026-10-04/full/unit-tests.xml (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`)
- verification/kafka-aml-2026-10-04/final-code-review.md (локальный материал: `verification/kafka-aml-2026-10-04/final-code-review.md`)

**Граница подтверждения:** Текущие тесты принимают NOT_READY и deferred как безопасную реакцию; общего лимита повторов нет. Новая воспроизводящая проверка не запускалась.

**Пробел:** KNOWN LIMITATION: нарушение протокола не выделено, запрос может откладываться без конца.

**Далее:** Отдельное проверяемое исправление после QA-профиля; сохранить идемпотентность и диагностическую причину.

**Проект контрольного кейса DESIGN-QA-022 — DESIGN_ONLY_NOT_IMPLEMENTED_NOT_RUN.**

Подготовка: Изолированная Queue и настоящий WorkflowClient с Transport, возвращающим HTTP 200/raw b"{" для submit; затем вариант с валидным JSON, но некорректным job.

1. Заморозить AnalysisEvent и enqueue один раз.
2. Вызвать scheduler_tick фиксированное число раз (например, 4), без sleep и внешней сети.
3. Сохранить число HTTP попыток, исходные key/event_id, status/last_error_code; отдельный healthy ряд проверяет fairness.

Ожидаемое текущее наблюдение: Все 4 попытки классифицируются NOT_READY; строка остаётся queued/deferred и повторяется. Конечное число шагов не доказывает бесконечность — отсутствие лимита устанавливается чтением кода.

Критерий после будущего исправления: После отдельно согласованной политики протокольная ошибка диагностически выделена и число повторов ограничено; исходный event/job не потерян и не заменён.

Ограничения выполнения: Не исправлять runtime здесь, не добавлять xfail как зелёное доказательство; целевая политика retry ещё не утверждена.

<a id="qa-023"></a>

## QA-023 — Общий дедлайн шага Kafka scheduler

**Риск:** P1. **Ответственный:** разработчик + QA automation. **Состояние:** KNOWN_LIMITATION.

**Сценарий:** Медленные последовательные чтения тела HTTP и операции PG удерживают scheduler дольше допустимого бюджета poll.

**Ожидается:** Целевое поведение: общий monotonic deadline ограничивает всю итерацию; остаточный бюджет распространяется на I/O; queue/idempotency сохраняются.

**Слой:** review finding; existing call-count checks; planned timing regression. **Реально:** scheduler / remote client code. **Подмены:** часы; HTTP; PostgreSQL.

**Существующие проверки:**

- [`tests/lab/test_kafka_analysis_consumer.py::test_tick_reconciles_then_submits_at_most_two_requests`](../../tests/lab/test_kafka_analysis_consumer.py)
- [`tests/workflow/test_remote_workflow.py::test_timeout_has_finite_small_bound`](../../tests/workflow/test_remote_workflow.py)

**Сохранённые свидетельства/контекст:**

- verification/kafka-aml-2026-10-04/full/unit-tests.xml (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`)
- verification/kafka-aml-2026-10-04/final-code-review.md (локальный материал: `verification/kafka-aml-2026-10-04/final-code-review.md`)

**Граница подтверждения:** <=2 HTTP вызова и socket timeout <=10с не ограничивают время всего tick. Новый timing case ещё не реализован/не выполнен.

**Пробел:** KNOWN LIMITATION: нет общего wall-clock deadline; возможен выход за Kafka max.poll.interval.ms.

**Далее:** Шаг 5: измерить полный tick и fault case; отдельным изменением ввести согласованный бюджет до расширения live fault suite.

**Проект контрольного кейса DESIGN-QA-023 — DESIGN_ONLY_NOT_IMPLEMENTED_NOT_RUN.**

Подготовка: Fake monotonic clock + controlled store/client: каждая последовательная DB/HTTP операция сдвигает часы; отдельный response double читает несколько chunks, каждый быстрее socket timeout.

1. Подготовить одну submitted и одну queued строку.
2. Задать иллюстративный общий бюджет B=10с только для тест-дизайна; последовательно накопить >B через допустимые отдельные операции.
3. Обработать одну итерацию; собрать время/sequence/call count и факт сохранения queue.
4. После решения по runtime добавить slow-drip чтение с конечной защитой теста и отдельный изолированный live rebalance case.

Ожидаемое текущее наблюдение: scheduler_tick не получает общий deadline и завершает последовательность после B; тест количества вызовов остаётся зелёным. Это проверка отсутствия гарантии, не измерение частоты реальных rebalances.

Критерий после будущего исправления: После принятия B тик прекращает новые операции по бюджету, не теряет уже принятое задание; API повтор сохраняет ключ и terminal.

Ограничения выполнения: Не ждать настоящий max.poll.interval в unit-тесте и не замедлять рабочую БД; B=10с не объявлять SLA продукта.

<a id="qa-024"></a>

## QA-024 — Полнота результатов и CI

**Риск:** P1. **Ответственный:** QA automation + разработчик. **Состояние:** GAP_WITH_RELATED_TESTS.

**Сценарий:** Обязательный case не выполнен, измерение отсутствует или первый сбой скрыт успешным повтором.

**Ожидается:** Неизмеренное не становится PASS; denominator и отсутствующие случаи сохранены; отдельные функциональные/model/Judge/SME итоги; результаты привязаны к версиям.

**Слой:** evaluator; planned reporting/CI. **Реально:** gates evaluator; сохранённый JUnit. **Подмены:** evaluation bundles.

**Существующие проверки:**

- [`tests/evaluator/test_core.py::EvaluatorControls::test_missing_outcome_cannot_disappear`](../../tests/evaluator/test_core.py)
- [`tests/evaluator/test_core.py::EvaluatorControls::test_runner_failure_code_cannot_pass_with_valid_result`](../../tests/evaluator/test_core.py)
- [`tests/evaluator/test_core.py::EvaluatorControls::test_external_failure_blocks_even_with_incomplete_coverage`](../../tests/evaluator/test_core.py)

**Сохранённые свидетельства/контекст:**

- verification/kafka-aml-2026-10-04/full/unit-tests.xml (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`)
- verification/kafka-aml-2026-10-04/full/unit-verification.json (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-verification.json`)
- [.github/workflows/ci.yml](../../.github/workflows/ci.yml)

**Граница подтверждения:** Сохранённый локальный PASS 05.10.2026 не доказывает текущий запуск и успешный удалённый CI. Browser/model/Judge CI не реализован.

**Пробел:** Нет единого run_id отчёта и подтверждённого удалённого run на QA-изменениях.

**Далее:** Шаг 9: отдельные профили, manifests/traces; push и коммиты делает владелец.

## Как использовать

Выберите строку и сначала уточните её границу доказательства. В следующих шагах добавляйте конкретный case и результат запуска с run_id, сохраняя первоначальный сбой и версии кода/данных/моделей. Программный PASS, модельное измерение и человеческая приёмка остаются разными итогами. При недоступном обязательном компоненте результат неполный, а не успешный.

Первый приоритет реализации — QA-017 (изоляция). Браузерные сценарии QA-001–012 затем получают Playwright; QA-013–016 — изолированные live/fault-проверки; QA-018–021 — реальные измерения и человеческую разметку. QA-022/023 требуют отдельных исправлений с контролируемым воспроизведением. QA-024 связывает эти доказательства, не подменяя их числом тестов.
