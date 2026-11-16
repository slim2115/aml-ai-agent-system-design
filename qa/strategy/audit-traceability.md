# Трассировка 22 замечаний аудита к QA

Дата: **07.10.2026**, Europe/Moscow. QA шаг 1. Все 22 FIX остаются формально открытыми. Эта карта связывает риск, существующие механические проверки, сохранённые доказательства и следующую необходимую проверку; она не закрывает аудит.

Свежий полный configured pytest от **07.10.2026** завершился **PASS: 920 тестов и 645 подслучаев**, без ошибок, падений и пропусков. Все **87 уникальных точных ссылок** этой карты сопоставлены с успешными записями свежего JUnit; хеши соответствующих тестовых исходников не изменились с момента статической проверки. Определения функций дополнительно проверены через AST, пути свидетельств существуют. Результат каждого mapped семейства указан в JSON как `execution_status=PASS`; parametrized записи не превращаются в независимые бизнес-кейсы.

В свежий pytest входят Streamlit AppTest тесты, реальные SQLite и subprocess lifecycle проверки. **Отдельный квалификатор 29 AppTest/CLI, живые Kafka/PostgreSQL, браузер, модели и SME-приёмка в этом шаге не повторялись.** Их доказательства ниже остаются историческими. Этот PASS не является приёмкой AI-качества или формальным закрытием FIX.

[Машинная карта](audit-traceability.json) содержит полный expected из аудита, требования, source hashes и границы real/doubles для каждой ссылки. Здесь приведено удобное представление той же карты.

FIX-21/22 не имеют прямых acceptance tests: `test_refs=[]`. Защитные тесты evaluator перечислены отдельно и подтверждают только то, что отсутствие данных не превращается в приёмку. Имена parametrized тестов приведены до суффикса `[case]`; один node-id означает семейство, а не один независимый бизнес-кейс.

## FIX-01 · Воспроизводимые зависимости, настройки и CI (P1)

**Риск:** Успех подготовленного окружения нельзя воспроизвести опубликованной установкой; конфигурация CI может проверять другой runtime или путь индекса.

**Ожидается по аудиту:** Установка и конструктор успешны в заявленных профилях; offline без артефакта даёт точную readiness-ошибку; env действительно изменяет runtime и изолированные пути; jobs стартуют и сохраняют фактически исполненный состав.

**Существующие проверки:**

- [tests/configuration/test_configuration.py::ConfigurationTests::test_all_configured_paths_reject_outside_and_traversal_values](../../tests/configuration/test_configuration.py#L91) — Реальная проверка настроек и границ путей. Искусственные env/path значения; модель и сеть не вызываются.
- [tests/configuration/test_configuration.py::OfflineEmbeddingTests::test_valid_artifact_uses_configured_path_cpu_and_offline_only](../../tests/configuration/test_configuration.py#L208) — Реальные валидаторы, чтение временных manifest и проверка хешей. Model factory/embedding подменены; нет модели и сети.
- [tests/configuration/test_configuration.py::OfflineEmbeddingTests::test_corrupt_file](../../tests/configuration/test_configuration.py#L240) — Реальные валидаторы, чтение временных manifest и проверка хешей. Model factory/embedding подменены; нет модели и сети.
- [tests/release/test_package_release.py::test_release_excludes_state_credentials_dependencies_and_old_evidence](../../tests/release/test_package_release.py#L71) — Реальная упаковка ZIP и проверки manifest/границ на временных файлах. Синтетическое дерево исходников; нет реального деплоя/CI.

**Сохранённые свидетельства:** b01 (локальный материал: `verification/block-01/summary.md`), b03 (локальный материал: `verification/block-03/summary.md`), b05_unit (локальный материал: `verification/block-05/unit-verification.json`), b05_ci (локальный материал: `verification/block-05/ci-inputs.json`), full_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-verification.json`), junit_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`).


**Свежая регрессия 07.10.2026:** результат (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-verification.json`), JUnit (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-tests.xml`). Указанные выше тестовые семейства прошли; для FIX-21/22 это только защитные контроли evaluator, не экспертная приёмка.

**Пробел:** Нет подтверждённого удалённого CI после публикации пользователем и установки с нуля на чистом учебном агенте. Проверка embedding использует mock factory, не измеряет качество модели.

**Ответственный:** Инженер платформы / DevOps. **Следующая работа:** QA шаги 2 и 9: квалифицировать чистую установку из lock-файлов и раздельные CI jobs; реальный remote run фиксировать только после публикации пользователем.

## FIX-02 · Оценщик, который не выдаёт PASS без приёмки (P0)

**Риск:** Автоматизация может разрешить выпуск непроверенного или заведомо несоответствующего продукта.

**Ожидается по аудиту:** Пустые/пропущенные обязательные измерения — INCOMPLETE, а не 100%; негативные контроли блокируют приёмку; CLI/pytest идентичны и возвращают ненулевой exit; корректный механический baseline проходит только соответствующие gates.

**Существующие проверки:**

- [tests/evaluator/test_core.py::EvaluatorControls::test_empty_is_incomplete_without_perfect_ratios](../../tests/evaluator/test_core.py#L16) — Реальный evaluator и quality gates внутри процесса. Механические bundle и искусственные метрики; нет SME или Judge.
- [tests/evaluator/test_core.py::EvaluatorControls::test_synthetic_baseline_never_proves_acceptance](../../tests/evaluator/test_core.py#L23) — Реальный evaluator и quality gates внутри процесса. Механические bundle и искусственные метрики; нет SME или Judge.
- [tests/evaluator/test_core.py::EvaluatorControls::test_external_missing_evidence_or_denominator_is_not_measured](../../tests/evaluator/test_core.py#L36) — Реальный evaluator и quality gates внутри процесса. Механические bundle и искусственные метрики; нет SME или Judge.
- [tests/evaluator/test_core.py::EvaluatorControls::test_critical_category_c_failure_is_not_averaged_away](../../tests/evaluator/test_core.py#L173) — Реальный evaluator и quality gates внутри процесса. Механические bundle и искусственные метрики; нет SME или Judge.
- [tests/evaluator/test_cli.py::EvaluationCLI::test_no_input_exits_incomplete_and_writes_report_pair](../../tests/evaluator/test_cli.py#L44) — Реальный CLI evaluator в отдельном Python-процессе. Механические bundle и искусственные метрики; нет SME или Judge.

**Сохранённые свидетельства:** b03 (локальный материал: `verification/block-03/summary.md`), b03_cli (локальный материал: `verification/block-03/cli-process-controls.json`), b05_unit (локальный материал: `verification/block-05/unit-verification.json`), full_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-verification.json`), junit_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`).


**Свежая регрессия 07.10.2026:** результат (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-verification.json`), JUnit (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-tests.xml`). Указанные выше тестовые семейства прошли; для FIX-21/22 это только защитные контроли evaluator, не экспертная приёмка.

**Пробел:** Evaluator проверен механическими bundle, но независимые модельные наблюдения и утверждённые SME-измерения отсутствуют. Существующий PASS теста оценщика не означает PASS качества AI.

**Ответственный:** QA / владелец evaluation. **Следующая работа:** QA шаги 3, 6–8: сохранить actual retrieval и repeated model outcomes, откалибровать Judge на SME-разметке, передать их в evaluator с provenance и знаменателями.

## FIX-03 · Единый входной и выходной контракт схем (P1)

**Риск:** Один и тот же ответ признаётся допустимым разными слоями по-разному; потребители не могут надёжно отличить draft, отказ и техническую ошибку.

**Ожидается по аудиту:** Одинаковая валидация всех слоёв; нестроковые/пустые входы возвращают согласованные ошибки без чтения источников; fullmatch и политика Unicode задокументированы; refusal не сохраняет отчётные выводы; никакой ложной HTTP-приёмки Python-слоя.

**Существующие проверки:**

- [tests/contracts/test_contracts_v1.py::WireShapeTests::test_valid_public_contracts_round_trip_without_coercion](../../tests/contracts/test_contracts_v1.py#L256) — Реальные Pydantic/JSON Schema и semantic validators. Синтетические контракты, без служб и моделей.
- [tests/contracts/test_contracts_v1.py::WireShapeTests::test_all_shape_negative_fixtures_are_rejected](../../tests/contracts/test_contracts_v1.py#L263) — Реальные Pydantic/JSON Schema и semantic validators. Синтетические контракты, без служб и моделей.
- [tests/contracts/test_contracts_v1.py::ArtifactAndParityTests::test_generated_artifacts_are_deterministic_and_current](../../tests/contracts/test_contracts_v1.py#L698) — Реальные Pydantic/JSON Schema и semantic validators. Синтетические контракты, без служб и моделей.
- [tests/lab/test_api.py::test_http_job_admission_replay_refusal_and_local_delivery](../../tests/lab/test_api.py#L274) — Настоящий FastAPI TestClient и SQLite workflow. In-process HTTP, QueryRecorder для PostgreSQL и FakeWorker; без сети.
- [tests/lab/test_kafka_analysis_api.py::test_publication_is_ack_only_and_has_no_aml_side_effects](../../tests/lab/test_kafka_analysis_api.py#L61) — Настоящий FastAPI маршрутизатор и валидация контракта. Broker/store подменены; нет Kafka/PG/модели.

**Сохранённые свидетельства:** b02 (локальный материал: `verification/block-02/summary.md`), b04 (локальный материал: `verification/block-04/summary.md`), b05_unit (локальный материал: `verification/block-05/unit-verification.json`), b05_runtime (локальный материал: `verification/block-05/workflow-probe.json`), full_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-verification.json`), junit_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`), live_cycle (локальный материал: `verification/kafka-aml-2026-10-04/live-cycle.json`).


**Свежая регрессия 07.10.2026:** результат (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-verification.json`), JUnit (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-tests.xml`). Указанные выше тестовые семейства прошли; для FIX-21/22 это только защитные контроли evaluator, не экспертная приёмка.

**Пробел:** HTTP API уже реализован и проверен; запись block-05 «реализовать HTTP» устарела. Нет автоматизированного браузерного сквозного набора Swagger и утверждения контрактов реального банковского потребителя.

**Ответственный:** Владелец требований / архитектор. **Следующая работа:** QA шаги 4–5: Playwright Swagger и API contract E2E в отдельном стенде, проверка различия Kafka ACK / job accepted / report ready.

## FIX-04 · Согласованность сущностей и достаточность контекста (P1)

**Риск:** Генератор получает неоднозначный или противоречивый контекст и может строить заключение на ложной полноте.

**Ожидается по аудиту:** Конфликты диагностируются одинаково независимо от порядка; неоднозначный контекст не идёт к генерации; допустимые null не отвергаются автоматически; достаточность соответствует утверждённой таблице решений.

**Существующие проверки:**

- [tests/runtime/test_context.py::test_duplicate_primary_keys_fail_even_outside_selected_client](../../tests/runtime/test_context.py#L80) — Реальный ContextBuilder, policy и локальные файлы. Синтетические snapshot/mutations, без модели и банковской БД.
- [tests/runtime/test_context.py::test_invalid_foreign_rows_are_never_silently_filtered](../../tests/runtime/test_context.py#L95) — Реальный ContextBuilder, policy и локальные файлы. Синтетические snapshot/mutations, без модели и банковской БД.
- [tests/runtime/test_context.py::test_conflicting_card_owner_is_detected](../../tests/runtime/test_context.py#L100) — Реальный ContextBuilder, policy и локальные файлы. Синтетические snapshot/mutations, без модели и банковской БД.
- [tests/runtime/test_context.py::test_missing_profile_without_transactions_is_insufficient](../../tests/runtime/test_context.py#L105) — Реальный ContextBuilder, policy и локальные файлы. Синтетические snapshot/mutations, без модели и банковской БД.

**Сохранённые свидетельства:** b04 (локальный материал: `verification/block-04/summary.md`), b05_unit (локальный материал: `verification/block-05/unit-verification.json`), b05_runtime (локальный материал: `verification/block-05/workflow-probe.json`), full_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-verification.json`), junit_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`).


**Свежая регрессия 07.10.2026:** результат (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-verification.json`), JUnit (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-tests.xml`). Указанные выше тестовые семейства прошли; для FIX-21/22 это только защитные контроли evaluator, не экспертная приёмка.

**Пробел:** Таблица достаточности и KYC относится к неутверждённой учебной политике; нет независимого набора сложных AML-кейсов.

**Ответственный:** Разработчик data access. **Следующая работа:** QA шаг 3: подготовить граничные и конфликтные сценарии; запросить предметное утверждение SME до утверждения ожидаемых AML-выводов.

## FIX-05 · Дата расследования, окно и актуальность снимка данных (P1)

**Риск:** Повторное расследование может использовать устаревшую историю или операции вне допустимого периода; результат трудно воспроизвести.

**Ожидается по аудиту:** Отбор соответствует согласованным границам/зоне; каждый результат использует целостный объявленный snapshot; обновление замечается по принятому SLA либо возвращает явную stale-data ошибку.

**Существующие проверки:**

- [tests/runtime/test_context.py::test_window_boundaries_are_inclusive_then_exclusive](../../tests/runtime/test_context.py#L110) — Реальный ContextBuilder, policy и локальные файлы. Синтетические snapshot/mutations, без модели и банковской БД.
- [tests/runtime/test_context.py::test_capture_lag_does_not_admit_incident_created_after_as_of](../../tests/runtime/test_context.py#L120) — Реальный ContextBuilder, policy и локальные файлы. Синтетические snapshot/mutations, без модели и банковской БД.
- [tests/runtime/test_context.py::test_source_change_invalidates_existing_builder_instead_of_cached_return](../../tests/runtime/test_context.py#L142) — Реальный ContextBuilder, policy и локальные файлы. Синтетические snapshot/mutations, без модели и банковской БД.
- [tests/runtime/test_context.py::test_manifest_replacement_mid_read_is_rejected](../../tests/runtime/test_context.py#L165) — Реальный ContextBuilder, policy и локальные файлы. Синтетические snapshot/mutations, без модели и банковской БД.
- [tests/lab/test_kafka_analysis_store.py::test_event_freezes_explicit_window_and_stable_key](../../tests/lab/test_kafka_analysis_store.py#L152) — Реальная логика AnalysisStore и контрактов. In-memory DB double имитирует PostgreSQL; не реальный сервер/транзакционные блокировки.

**Сохранённые свидетельства:** b04 (локальный материал: `verification/block-04/summary.md`), b05_unit (локальный материал: `verification/block-05/unit-verification.json`), b05_runtime (локальный материал: `verification/block-05/workflow-probe.json`), full_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-verification.json`), junit_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`).


**Свежая регрессия 07.10.2026:** результат (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-verification.json`), JUnit (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-tests.xml`). Указанные выше тестовые семейства прошли; для FIX-21/22 это только защитные контроли evaluator, не экспертная приёмка.

**Пробел:** Нет утверждённого operational TTL или истории реального источника; SQL practice не является входом AML-анализа.

**Ответственный:** Архитектор данных. **Следующая работа:** QA шаги 3 и 5: упражнение о frozen snapshot и SQL projection; проверить snapshot/window через API, Kafka и UI, включая изменённый UUID payload.

## FIX-06 · Точная передача чисел и минимально достаточного контекста (P1)

**Риск:** Смысл факта может измениться до модели; модель не может обосновать отношения/время по отсутствующим атрибутам.

**Ожидается по аудиту:** Исходные денежные значения сохраняются точно согласно контракту; сериализация содержит необходимые поля и source version без лишней PII; агрегаты воспроизводимы.

**Существующие проверки:**

- [tests/contracts/test_contracts_v1.py::ReportSemanticTests::test_sum_uses_exact_decimal_including_long_operands](../../tests/contracts/test_contracts_v1.py#L466) — Реальные Pydantic/JSON Schema и semantic validators. Синтетические контракты, без служб и моделей.
- [tests/contracts/test_contracts_v1.py::ReportSemanticTests::test_cross_currency_sum_is_rejected](../../tests/contracts/test_contracts_v1.py#L460) — Реальные Pydantic/JSON Schema и semantic validators. Синтетические контракты, без служб и моделей.
- [tests/runtime/test_generation.py::test_full_decimal_precision_and_no_currency_rounding_policy_is_invented](../../tests/runtime/test_generation.py#L267) — Реальные renderer и publication guard. Синтетический контекст и заданные JSON ответы; LLM не вызывается.
- [tests/runtime/test_generation.py::test_model_projection_excludes_unneeded_identifiers_and_all_source_free_text](../../tests/runtime/test_generation.py#L78) — Реальные renderer и publication guard. Синтетический контекст и заданные JSON ответы; LLM не вызывается.
- [tests/lab/test_api.py::test_money_and_time_wire_format_retains_precision](../../tests/lab/test_api.py#L267) — Настоящий FastAPI TestClient и SQLite workflow. In-process HTTP, QueryRecorder для PostgreSQL и FakeWorker; без сети.

**Сохранённые свидетельства:** b04 (локальный материал: `verification/block-04/summary.md`), b05_unit (локальный материал: `verification/block-05/unit-verification.json`), full_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-verification.json`), junit_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`).


**Свежая регрессия 07.10.2026:** результат (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-verification.json`), JUnit (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-tests.xml`). Указанные выше тестовые семейства прошли; для FIX-21/22 это только защитные контроли evaluator, не экспертная приёмка.

**Пробел:** Не утверждены реальные правила FX, разрядности валют и необходимые модельные признаки. Браузерное отображение и экспорт чисел ещё не охвачены Playwright.

**Ответственный:** Разработчик data access / генерации. **Следующая работа:** QA шаги 3–4: проверить точные строки денежных значений на пути источник → API → UI → скачанный JSON без float-преобразования.

## FIX-07 · Смысл и принадлежность фактических доказательств (P0)

**Риск:** Наличие реального tx_id создаёт видимость доказанности неверного AML-вывода.

**Ожидается по аудиту:** Неподтверждённые выводы не публикуются как доказанные; чужой tx блокируется; для правильного агрегата доступны все исходные значения; отсутствие страны/порога явно отражено; корректные baseline сохраняются.

**Существующие проверки:**

- [tests/runtime/test_generation.py::test_publication_guard_rejects_fabrication_even_with_valid_evidence_ids](../../tests/runtime/test_generation.py#L175) — Реальные renderer и publication guard. Синтетический контекст и заданные JSON ответы; LLM не вызывается.
- [tests/contracts/test_contracts_v1.py::ReportSemanticTests::test_every_evidence_kind_is_bound_to_snapshot](../../tests/contracts/test_contracts_v1.py#L439) — Реальные Pydantic/JSON Schema и semantic validators. Синтетические контракты, без служб и моделей.
- [tests/contracts/test_contracts_v1.py::ReportSemanticTests::test_aggregates_require_existing_members_and_exact_window](../../tests/contracts/test_contracts_v1.py#L446) — Реальные Pydantic/JSON Schema и semantic validators. Синтетические контракты, без служб и моделей.
- [tests/workflow/test_sources.py::test_changed_or_unresolvable_report_never_gets_source_confirmation](../../tests/workflow/test_sources.py#L56) — Реальный resolver, исходные demo-файлы, checksums. Синтетический отчёт; нет модели или браузера.

**Сохранённые свидетельства:** b04 (локальный материал: `verification/block-04/summary.md`), b05_unit (локальный материал: `verification/block-05/unit-verification.json`), b05_runtime (локальный материал: `verification/block-05/workflow-probe.json`), full_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-verification.json`), junit_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`).


**Свежая регрессия 07.10.2026:** результат (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-verification.json`), JUnit (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-tests.xml`). Указанные выше тестовые семейства прошли; для FIX-21/22 это только защитные контроли evaluator, не экспертная приёмка.

**Пробел:** Покрыт типизированный каталог фактов. Свободная AML-проза и полезность отчёта не приняты SME; LLM не должен оцениваться как генератор произвольных фактов в текущей архитектуре.

**Ответственный:** Разработчик guardrails. **Следующая работа:** QA шаги 3, 7–8: эталоны и намеренно испорченные факты, строгая проверка данных до Judge; Judge оценивает понятность/полноту в пределах рубрики, без права approve.

## FIX-08 · Идентичность и полнота нормативных ссылок (P0)

**Риск:** Оператор может принять вывод со ссылкой на другой пункт или текст нормы; механическая известность имитирует нормативное основание.

**Ожидается по аудиту:** Никакой подмены идентичности; все несовместимые связи отклонены/эскалированы; правило не становится applicable только из-за top-k; корректная цитата раскрывает точный текст и редакцию.

**Существующие проверки:**

- [tests/runtime/test_retrieval.py::test_citation_binds_exact_chunk_and_rule_identity](../../tests/runtime/test_retrieval.py#L59) — Реальные chunking/index/edition checks. FakeTokenizer и FakeEmbedding; relevance не измеряется.
- [tests/runtime/test_retrieval.py::test_ranking_is_not_an_applicability_or_legal_approval_claim](../../tests/runtime/test_retrieval.py#L244) — Реальные chunking/index/edition checks. FakeTokenizer и FakeEmbedding; relevance не измеряется.
- [tests/contracts/test_contracts_v1.py::ReportSemanticTests::test_same_rule_identity_cannot_claim_conflicting_content](../../tests/contracts/test_contracts_v1.py#L503) — Реальные Pydantic/JSON Schema и semantic validators. Синтетические контракты, без служб и моделей.
- [tests/workflow/test_sources.py::test_exact_cell_pointer_and_value_and_edition_range](../../tests/workflow/test_sources.py#L37) — Реальный resolver, исходные demo-файлы, checksums. Синтетический отчёт; нет модели или браузера.

**Сохранённые свидетельства:** b04 (локальный материал: `verification/block-04/summary.md`), b05_unit (локальный материал: `verification/block-05/unit-verification.json`), b05_runtime (локальный материал: `verification/block-05/workflow-probe.json`), full_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-verification.json`), junit_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`), browser_history (локальный материал: `verification/kafka-aml-2026-10-04/summary.md`).


**Свежая регрессия 07.10.2026:** результат (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-verification.json`), JUnit (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-tests.xml`). Указанные выше тестовые семейства прошли; для FIX-21/22 это только защитные контроли evaluator, не экспертная приёмка.

**Пробел:** Fake vectors не доказывают релевантность и юридическую применимость. Нет утверждённого нормативного корпуса и ground truth SME.

**Ответственный:** Разработчик retrieval / guardrails. **Следующая работа:** QA шаги 3 и 6: разметить точные chunk/version/source; разделять retrieval relevance и применимость нормы; проверить ссылки и текст в настоящем браузере.

## FIX-09 · Жизненный цикл и редакции индекса знаний (P1)

**Риск:** Persistent index, lookup и текст отчёта могут относиться к разным корпусам; будущая или отменённая редакция остаётся доступной.

**Ожидается по аудиту:** Состав/тексты/lookup соответствуют одному manifest; удалённые не выдаются; as_of выбирает редакцию по контракту; повтор индексации идемпотентен; непригодный корпус даёт явный статус без частичного переключения.

**Существующие проверки:**

- [tests/runtime/test_retrieval.py::test_embedding_and_chunk_recipe_invalidate_identity](../../tests/runtime/test_retrieval.py#L93) — Реальные chunking/index/edition checks. FakeTokenizer и FakeEmbedding; relevance не измеряется.
- [tests/runtime/test_retrieval.py::test_reordering_corpus_is_deterministic_and_removed_rule_cannot_survive](../../tests/runtime/test_retrieval.py#L108) — Реальные chunking/index/edition checks. FakeTokenizer и FakeEmbedding; relevance не измеряется.
- [tests/runtime/test_retrieval.py::test_explicit_edition_boundary_and_timezone_are_applied_before_top_k](../../tests/runtime/test_retrieval.py#L145) — Реальные chunking/index/edition checks. FakeTokenizer и FakeEmbedding; relevance не измеряется.
- [tests/runtime/test_retrieval.py::test_failed_rebuild_does_not_mutate_existing_index](../../tests/runtime/test_retrieval.py#L194) — Реальные chunking/index/edition checks. FakeTokenizer и FakeEmbedding; relevance не измеряется.

**Сохранённые свидетельства:** b04 (локальный материал: `verification/block-04/summary.md`), b05_unit (локальный материал: `verification/block-05/unit-verification.json`), b05_runtime (локальный материал: `verification/block-05/workflow-probe.json`), full_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-verification.json`), junit_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`).


**Свежая регрессия 07.10.2026:** результат (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-verification.json`), JUnit (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-tests.xml`). Указанные выше тестовые семейства прошли; для FIX-21/22 это только защитные контроли evaluator, не экспертная приёмка.

**Пробел:** Проверен локальный immutable index с fake embedding. Persistent/multi-process index и атомарная миграция не реализованы и не входят в текущий учебный runtime.

**Ответственный:** Разработчик retrieval. **Следующая работа:** QA шаг 6: реальные embedding regression для изменения корпуса, редакций и token recipe; расширять многопроцессную квалификацию только при реализации такого сервиса.

## FIX-10 · Извлечение с учётом длины, релевантности и настоящих метрик (P1)

**Риск:** Важные условия нормы могут не участвовать в поиске, а наличие top-k ошибочно выглядит как релевантный результат.

**Ожидается по аудиту:** Хвостовые условия доступны поиску через подходящие чанки; no-relevant отражается явно; actual chunks измеряются по утверждённому ground truth; значения NFR-12 проверены отдельно от output refs.

**Существующие проверки:**

- [tests/runtime/test_retrieval.py::test_long_rule_token_offsets_overlap_and_complete_source_coverage](../../tests/runtime/test_retrieval.py#L69) — Реальные chunking/index/edition checks. FakeTokenizer и FakeEmbedding; relevance не измеряется.
- [tests/runtime/test_retrieval.py::test_allowlisted_query_features_have_safe_fixed_embedding_text](../../tests/runtime/test_retrieval.py#L169) — Реальные chunking/index/edition checks. FakeTokenizer и FakeEmbedding; relevance не измеряется.
- [tests/evaluator/test_core.py::EvaluatorControls::test_retrieval_is_independent_of_answer_citations](../../tests/evaluator/test_core.py#L187) — Реальный evaluator и quality gates внутри процесса. Механические bundle и искусственные метрики; нет SME или Judge.
- [tests/evaluator/test_core.py::EvaluatorControls::test_precision_detects_surplus_retrieval](../../tests/evaluator/test_core.py#L197) — Реальный evaluator и quality gates внутри процесса. Механические bundle и искусственные метрики; нет SME или Judge.

**Сохранённые свидетельства:** b04 (локальный материал: `verification/block-04/summary.md`), b04_retrieval (локальный материал: `verification/block-04/retrieval-provenance.json`), b05_unit (локальный материал: `verification/block-05/unit-verification.json`), full_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-verification.json`), junit_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`).


**Свежая регрессия 07.10.2026:** результат (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-verification.json`), JUnit (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-tests.xml`). Указанные выше тестовые семейства прошли; для FIX-21/22 это только защитные контроли evaluator, не экспертная приёмка.

**Пробел:** Тест chunking использует fake tokenizer/vectors; реальный probe не заменяет relevance dataset. Не измерены независимые precision/recall и no-relevant threshold.

**Ответственный:** Разработчик retrieval / evaluation. **Следующая работа:** QA шаги 3 и 6: реальные embedding на версионированном корпусе, actual retrieved chunks, независимые релевантные метки и сценарии без релевантного ответа.

## FIX-11 · Прозрачная нормализация без потери смыслового содержания (P1)

**Риск:** Валидный итог может скрывать потерянные значимые утверждения и происхождение confidence.

**Ожидается по аудиту:** Каждое значимое изменение явно отражено или ответ отклонён/перегенерирован; факты/evidence не исчезают незаметно; confidence имеет честное происхождение; full-history контракт исполняется без противоречивых лимитов.

**Существующие проверки:**

- [tests/runtime/test_generation.py::test_model_may_reorder_all_facts_but_cannot_select_a_subset](../../tests/runtime/test_generation.py#L67) — Реальные renderer и publication guard. Синтетический контекст и заданные JSON ответы; LLM не вызывается.
- [tests/runtime/test_generation.py::test_missing_duplicate_unknown_or_semantic_response_content_is_rejected](../../tests/runtime/test_generation.py#L117) — Реальные renderer и publication guard. Синтетический контекст и заданные JSON ответы; LLM не вызывается.
- [tests/runtime/test_generation.py::test_raw_response_is_not_repaired_and_errors_do_not_disclose_content](../../tests/runtime/test_generation.py#L155) — Реальные renderer и publication guard. Синтетический контекст и заданные JSON ответы; LLM не вызывается.
- [tests/runtime/test_service.py::test_null_confidence_accepted_but_required](../../tests/runtime/test_service.py#L175) — Реальный runtime service с синтетическим demo snapshot. FakeIndex/transport/vectors; нет живой модели.

**Сохранённые свидетельства:** b04 (локальный материал: `verification/block-04/summary.md`), b05_unit (локальный материал: `verification/block-05/unit-verification.json`), b05_runtime (локальный материал: `verification/block-05/workflow-probe.json`), full_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-verification.json`), junit_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`).


**Свежая регрессия 07.10.2026:** результат (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-verification.json`), JUnit (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-tests.xml`). Указанные выше тестовые семейства прошли; для FIX-21/22 это только защитные контроли evaluator, не экспертная приёмка.

**Пробел:** Проверяются искусственно заданные ответы: нет серии повторов реальной LLM. Свободное редактирование и calibrated confidence не реализованы.

**Ответственный:** Разработчик генерации. **Следующая работа:** QA шаг 7: 10 учебных кейсов × 3 повтора, учитывать каждый некорректный ответ/повтор/timeout; сравнивать каталог фактов, перестановку и время с template.

## FIX-12 · Защита целостности ответа от prompt injection (P0)

**Риск:** Недоверенный текст может навязать оператору вымышленное решение или факт при прошедших проверках.

**Ожидается по аудиту:** Canary-инструкция не становится подтверждённым фактом/решением; недоверенные данные не получают полномочий; benign cases не блокируются по одному словосочетанию; категории/причины отказов и критический C-gate соответствуют контракту.

**Существующие проверки:**

- [tests/runtime/test_generation.py::test_untrusted_source_text_is_not_a_command_and_does_not_change_draft](../../tests/runtime/test_generation.py#L91) — Реальные renderer и publication guard. Синтетический контекст и заданные JSON ответы; LLM не вызывается.
- [tests/runtime/test_generation.py::test_even_vetted_rule_prose_is_never_sent_as_a_model_instruction](../../tests/runtime/test_generation.py#L103) — Реальные renderer и publication guard. Синтетический контекст и заданные JSON ответы; LLM не вызывается.
- [tests/runtime/test_generation.py::test_publication_guard_rejects_fabrication_even_with_valid_evidence_ids](../../tests/runtime/test_generation.py#L175) — Реальные renderer и publication guard. Синтетический контекст и заданные JSON ответы; LLM не вызывается.
- [tests/evaluator/test_core.py::EvaluatorControls::test_critical_category_c_failure_is_not_averaged_away](../../tests/evaluator/test_core.py#L173) — Реальный evaluator и quality gates внутри процесса. Механические bundle и искусственные метрики; нет SME или Judge.

**Сохранённые свидетельства:** b04 (локальный материал: `verification/block-04/summary.md`), b05_unit (локальный материал: `verification/block-05/unit-verification.json`), b05_runtime (локальный материал: `verification/block-05/workflow-probe.json`), full_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-verification.json`), junit_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`).


**Свежая регрессия 07.10.2026:** результат (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-verification.json`), JUnit (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-tests.xml`). Указанные выше тестовые семейства прошли; для FIX-21/22 это только защитные контроли evaluator, не экспертная приёмка.

**Пробел:** Контроли защищают текущую проекцию и публикацию; нет независимой adversarial серии реального генератора или будущего Judge. Новые текстовые поверхности требуют собственных тестов.

**Ответственный:** Инженер безопасности LLM. **Следующая работа:** QA шаги 3, 7–8: benign/adversarial пары, canary и чужие инструкции внутри оцениваемого отчёта; Judge output остаётся недоверенным и не меняет workflow.

## FIX-13 · Маскирование PII на всех диагностических путях (P0)

**Риск:** Ошибки и диагностика обходят предусмотренную минимизацию и могут раскрывать данные читателям логов/trace.

**Ожидается по аудиту:** Полные маркеры отсутствуют в запрещённых policy каналах, в том числе error/stdout/trace; PAN отклоняется или корректно маскируется до использования; разрешённые операторские данные не скрываются произвольно.

**Существующие проверки:**

- [tests/runtime/test_transport.py::test_redirects_and_error_bodies_never_follow_or_expose](../../tests/runtime/test_transport.py#L62) — Реальная политика runtime HTTP transport. Подменены HTTP connection/reply; нет Ollama/сети.
- [tests/workflow/test_runtime_events.py::test_runtime_emits_strict_stages_and_attempts_without_generated_text](../../tests/workflow/test_runtime_events.py#L62) — Реальные события/SQLite/controller; для части случаев реальный child process. Генератор/transport/handler подменены; не live модельный trace.
- [tests/lab/test_api.py::test_safe_database_errors](../../tests/lab/test_api.py#L259) — Настоящий FastAPI TestClient и SQLite workflow. In-process HTTP, QueryRecorder для PostgreSQL и FakeWorker; без сети.
- [tests/workflow/test_remote_workflow.py::test_network_errors_are_safe](../../tests/workflow/test_remote_workflow.py#L83) — Реальный remote adapter; часть сценариев с реальной SQLite. HTTP transport подменён; нет live API.
- [tests/lab/test_kafka_analysis_consumer.py::test_unexpected_client_failure_never_leaks_details_or_discards_queue](../../tests/lab/test_kafka_analysis_consumer.py#L489) — Реальная consumer/scheduler логика; один mapped сценарий использует настоящую SQLite. Kafka/PG/HTTP doubles и fake worker; не live fault injection.

**Сохранённые свидетельства:** b04 (локальный материал: `verification/block-04/summary.md`), b05_unit (локальный материал: `verification/block-05/unit-verification.json`), full_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-verification.json`), junit_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`).


**Свежая регрессия 07.10.2026:** результат (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-verification.json`), JUnit (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-tests.xml`). Указанные выше тестовые семейства прошли; для FIX-21/22 это только защитные контроли evaluator, не экспертная приёмка.

**Пробел:** Новые API/remote/Kafka каналы имеют safe-error контроли, но Playwright trace/video/screenshots/downloads ещё не проверены на утечки. Решение оператора и SQLite не являются обезличенным зашифрованным аудитом.

**Ответственный:** Инженер безопасности / privacy. **Следующая работа:** QA шаги 2, 4 и 8: только синтетические canary; проверить отчёты ошибок и browser artifacts, не сохранять X-Lab-Key в артефактах; определить retention и доступ.

## FIX-14 · Контракт периметра и доказательства производственных полномочий (P1)

**Риск:** Локальный localhost-прогон не подтверждает запрет публичных API и невозможность изменения банковских источников в развёртывании.

**Ожидается по аудиту:** Приложение и сеть блокируют выход за согласованный контур без отправки; нет публичного fallback; разрешённые calls работают; write банковских источников запрещён фактическими правами.

**Существующие проверки:**

- [tests/configuration/test_configuration.py::ConfigurationTests::test_loopback_urls_are_validated_without_dns](../../tests/configuration/test_configuration.py#L112) — Реальная проверка настроек и границ путей. Искусственные env/path значения; модель и сеть не вызываются.
- [tests/runtime/test_transport.py::test_only_literal_loopback_no_proxy_or_redirect_and_constrained_request](../../tests/runtime/test_transport.py#L46) — Реальная политика runtime HTTP transport. Подменены HTTP connection/reply; нет Ollama/сети.
- [tests/workflow/test_remote_workflow.py::test_proxy_environment_ignored_and_redirects_disabled](../../tests/workflow/test_remote_workflow.py#L63) — Реальный remote adapter; часть сценариев с реальной SQLite. HTTP transport подменён; нет live API.
- [tests/lab/test_api.py::test_every_documented_data_operation_is_authenticated_before_io](../../tests/lab/test_api.py#L353) — Настоящий FastAPI TestClient и SQLite workflow. In-process HTTP, QueryRecorder для PostgreSQL и FakeWorker; без сети.

**Сохранённые свидетельства:** b03 (локальный материал: `verification/block-03/summary.md`), b04 (локальный материал: `verification/block-04/summary.md`), b05_unit (локальный материал: `verification/block-05/unit-verification.json`), b05_runtime (локальный материал: `verification/block-05/workflow-probe.json`), full_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-verification.json`), junit_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`).


**Свежая регрессия 07.10.2026:** результат (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-verification.json`), JUnit (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-tests.xml`). Указанные выше тестовые семейства прошли; для FIX-21/22 это только защитные контроли evaluator, не экспертная приёмка.

**Пробел:** Программные проверки и TestClient не доказывают OS/network deny rules, IAM или права на реальные банковские источники.

**Ответственный:** Инженер инфраструктурной безопасности. **Следующая работа:** QA шаги 2 и 5: выделенный локальный стенд с отдельными учётными данными и allowlist; отдельная инфраструктурная проверка только при появлении целевого защищённого контура.

## FIX-15 · Экранирование HTML из банковских полей (P1)

**Риск:** Содержимое источника может искажать интерфейс и изображать системное действие.

**Ожидается по аудиту:** Источник показан буквальным текстом; в DOM нет элементов/атрибутов, созданных из поля, и ложных продуктовых действий; законное оформление таблицы остаётся корректным.

**Существующие проверки:**

- [tests/workflow/test_ui_guided.py::test_fabricated_report_html_is_plain_text_and_cannot_authorize](../../tests/workflow/test_ui_guided.py#L483) — Настоящий Streamlit AppTest, SQLite и локальная delivery логика. SyntheticService анализа; AppTest не является браузером.

**Сохранённые свидетельства:** b04 (локальный материал: `verification/block-04/summary.md`), b05_ui (локальный материал: `verification/block-05/ui-cli-qualification.json`), full_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-verification.json`), junit_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`).


**Свежая регрессия 07.10.2026:** результат (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-verification.json`), JUnit (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-tests.xml`). Указанные выше тестовые семейства прошли; для FIX-21/22 это только защитные контроли evaluator, не экспертная приёмка.

**Пробел:** AppTest проверяет текст и allow_html=false, но не выполняет JavaScript и не доказывает отсутствие инъекции DOM или внешних запросов в браузере.

**Ответственный:** Frontend-разработчик. **Следующая работа:** QA шаг 4: Playwright на разрешённых синтетических HTML/attribute payload, контроль literal text, DOM и отсутствия неожиданных network/dialog событий.

## FIX-16 · Однозначный объект решения и проверяемые основания в UI (P0)

**Риск:** Человек может одобрить не тот отчёт или вывод с неясным основанием.

**Ожидается по аудиту:** Нет активного неоднозначного approve; decision payload всегда связан с видимым ID/version; несовместимый результат отклонён; все refs доступны; неподтверждённые основания явно помечены и не дают ложной готовности.

**Существующие проверки:**

- [tests/workflow/test_ui_guided.py::test_changed_investigation_inputs_hide_previous_report_actions](../../tests/workflow/test_ui_guided.py#L465) — Настоящий Streamlit AppTest, SQLite и локальная delivery логика. SyntheticService анализа; AppTest не является браузером.
- [tests/workflow/test_ui_guided.py::test_changed_revision_requires_explicit_reread_and_keeps_draft](../../tests/workflow/test_ui_guided.py#L178) — Настоящий Streamlit AppTest, SQLite и локальная delivery логика. SyntheticService анализа; AppTest не является браузером.
- [tests/workflow/test_ui_guided.py::test_unavailable_sources_block_review_and_export_but_allow_local_queue](../../tests/workflow/test_ui_guided.py#L419) — Настоящий Streamlit AppTest, SQLite и локальная delivery логика. SyntheticService анализа; AppTest не является браузером.
- [tests/workflow/test_ui_integrated.py::test_kafka_panel_opens_linked_report_without_model_preparation](../../tests/workflow/test_ui_integrated.py#L91) — Настоящий AppTest и SQLite общей истории в тестовом каталоге. Удалённый API client подменён; нет браузера/HTTP сервера/Kafka.

**Сохранённые свидетельства:** b04 (локальный материал: `verification/block-04/summary.md`), b05_unit (локальный материал: `verification/block-05/unit-verification.json`), b05_ui (локальный материал: `verification/block-05/ui-cli-qualification.json`), full_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-verification.json`), junit_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`), browser_history (локальный материал: `verification/kafka-aml-2026-10-04/summary.md`), ui_cli (локальный материал: `verification/kafka-aml-2026-10-04/ui-cli-qualification.json`).


**Свежая регрессия 07.10.2026:** результат (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-verification.json`), JUnit (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-tests.xml`). Указанные выше тестовые семейства прошли; для FIX-21/22 это только защитные контроли evaluator, не экспертная приёмка.

**Пробел:** Сохранённая браузерная проверка открывала отчёт/источники, но не полный decision/export путь. Нет Playwright suite и junior-операторской usability приёмки.

**Ответственный:** Frontend-разработчик. **Следующая работа:** QA шаги 4 и 10: полный пятишаговый UI цикл, переключение задания/версии и сохранность черновика; отдельная оценка понятности человеком.

## FIX-17 · Сохраняемое решение уполномоченного оператора (P0)

**Риск:** Человеческое решение нельзя восстановить или доказать; повторный просмотр не имеет надёжного статуса.

**Ожидается по аудиту:** Решение и автор/время/версия сохраняются, неизменяемы как историческое событие; повторы идемпотентны; конфликт версии и недостаточные права отвергаются; UI получает устойчивый статус.

**Существующие проверки:**

- [tests/workflow/test_store.py::test_decision_persists_server_actor_time_event_and_replay](../../tests/workflow/test_store.py#L205) — Реальная временная SQLite, транзакции/CAS. Синтетические контракты и principal; нет банковского IAM.
- [tests/workflow/test_store.py::test_decision_race_one_revision_only_and_changed_replay_conflicts](../../tests/workflow/test_store.py#L244) — Реальная временная SQLite, транзакции/CAS. Синтетические контракты и principal; нет банковского IAM.
- [tests/workflow/test_store.py::test_decision_cannot_be_made_by_other_owner_even_supervisor](../../tests/workflow/test_store.py#L223) — Реальная временная SQLite, транзакции/CAS. Синтетические контракты и principal; нет банковского IAM.
- [tests/lab/test_api.py::test_real_store_decision_http_hash_binding_cas_and_export](../../tests/lab/test_api.py#L316) — Настоящий FastAPI TestClient и SQLite workflow. In-process HTTP, QueryRecorder для PostgreSQL и FakeWorker; без сети.
- [tests/workflow/test_ui_integrated.py::test_history_and_human_decision_remain_available_with_api_offline](../../tests/workflow/test_ui_integrated.py#L74) — Настоящий AppTest и SQLite общей истории в тестовом каталоге. Удалённый API client подменён; нет браузера/HTTP сервера/Kafka.

**Сохранённые свидетельства:** b02 (локальный материал: `verification/block-02/summary.md`), b05_unit (локальный материал: `verification/block-05/unit-verification.json`), b05_runtime (локальный материал: `verification/block-05/workflow-probe.json`), b05_ui (локальный материал: `verification/block-05/ui-cli-qualification.json`), full_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-verification.json`), junit_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`), ui_cli (локальный материал: `verification/kafka-aml-2026-10-04/ui-cli-qualification.json`).


**Свежая регрессия 07.10.2026:** результат (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-verification.json`), JUnit (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-tests.xml`). Указанные выше тестовые семейства прошли; для FIX-21/22 это только защитные контроли evaluator, не экспертная приёмка.

**Пробел:** Используется trusted local principal, а не банковский IAM. Решение в настоящем браузере и восстановление после его сохранения не проверены автоматизированно.

**Ответственный:** Backend-разработчик операторского процесса. **Следующая работа:** QA шаги 4–5: две браузерные сессии со stale revision, повтор запроса и reload; проверка неизменяемого события SQLite; IAM отдельно при подключении.

## FIX-18 · Ручная очередь, подпись, экспорт и финальный статус (P1)

**Риск:** Пользователь может считать работу переданной/завершённой, хотя событие не произошло.

**Ожидается по аудиту:** Ручная работа и экспорт подтверждаются реальной квитанцией; retry не дублирует кейс; stale/unsigned версии не доставляются; финальный статус меняется только по согласованному полномочию и подтверждённому событию.

**Существующие проверки:**

- [tests/workflow/test_delivery.py::test_export_requires_approval_not_just_any_decision](../../tests/workflow/test_delivery.py#L164) — Реальная временная SQLite и локальный delivery simulator. Внешняя доставка, CMS и подпись отсутствуют.
- [tests/workflow/test_delivery.py::test_crash_after_accept_before_publish_recovers_exact_receipt](../../tests/workflow/test_delivery.py#L177) — Реальная временная SQLite и локальный delivery simulator. Внешняя доставка, CMS и подпись отсутствуют.
- [tests/workflow/test_delivery.py::test_concurrent_dispatchers_share_one_receipt](../../tests/workflow/test_delivery.py#L216) — Реальная временная SQLite и локальный delivery simulator. Внешняя доставка, CMS и подпись отсутствуют.
- [tests/workflow/test_delivery.py::test_external_signature_and_closure_are_not_ready](../../tests/workflow/test_delivery.py#L276) — Реальная временная SQLite и локальный delivery simulator. Внешняя доставка, CMS и подпись отсутствуют.
- [tests/workflow/test_ui_guided.py::test_export_requires_confirmation_and_returns_exact_saved_report](../../tests/workflow/test_ui_guided.py#L270) — Настоящий Streamlit AppTest, SQLite и локальная delivery логика. SyntheticService анализа; AppTest не является браузером.

**Сохранённые свидетельства:** b02 (локальный материал: `verification/block-02/summary.md`), b05_unit (локальный материал: `verification/block-05/unit-verification.json`), b05_runtime (локальный материал: `verification/block-05/workflow-probe.json`), b05_ui (локальный материал: `verification/block-05/ui-cli-qualification.json`), full_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-verification.json`), junit_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`), ui_cli (локальный материал: `verification/kafka-aml-2026-10-04/ui-cli-qualification.json`).


**Свежая регрессия 07.10.2026:** результат (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-verification.json`), JUnit (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-tests.xml`). Указанные выше тестовые семейства прошли; для FIX-21/22 это только защитные контроли evaluator, не экспертная приёмка.

**Пробел:** Delivery — локальный симулятор. Нет CMS/внешней очереди/электронной подписи и фактической браузерной загрузки экспортируемого файла.

**Ответственный:** Владелец AML-процесса / интеграционный разработчик. **Следующая работа:** QA шаги 4–5: Playwright download и digest сохранённого отчёта, повтор экспорта/квитанции; внешние адаптеры требуют отдельного согласованного контракта.

## FIX-19 · Нормализованные сбои, причины и полный технический аудит (P1)

**Риск:** Оператор не различает отсутствие оснований и поломку зависимости; часть запросов исчезает из аудита.

**Ожидается по аудиту:** Каждый запрос имеет один terminal outcome и trace; technical error не называется injection; отсутствие payload не выдаёт guards PASS; retry соответствует явной политике и бюджету; после восстановления следующий запрос работает без старого решения.

**Существующие проверки:**

- [tests/runtime/test_service.py::test_safe_terminal_errors_no_exception_text_or_false_success](../../tests/runtime/test_service.py#L119) — Реальный runtime service с синтетическим demo snapshot. FakeIndex/transport/vectors; нет живой модели.
- [tests/workflow/test_runtime_events.py::test_controller_flushes_received_events_before_success_and_retains_after_restart](../../tests/workflow/test_runtime_events.py#L170) — Реальные события/SQLite/controller; для части случаев реальный child process. Генератор/transport/handler подменены; не live модельный trace.
- [tests/lab/test_kafka_analysis_consumer.py::test_intake_commits_durable_queue_before_offset_without_http](../../tests/lab/test_kafka_analysis_consumer.py#L168) — Реальная consumer/scheduler логика; один mapped сценарий использует настоящую SQLite. Kafka/PG/HTTP doubles и fake worker; не live fault injection.
- [tests/lab/test_kafka_analysis_consumer.py::test_real_sqlite_admission_survives_ambiguous_ack_and_projects_without_new_kafka](../../tests/lab/test_kafka_analysis_consumer.py#L336) — Реальная consumer/scheduler логика; один mapped сценарий использует настоящую SQLite. Kafka/PG/HTTP doubles и fake worker; не live fault injection.
- [tests/lab/test_kafka_analysis_store.py::test_delayed_acceptance_from_one_observer_cannot_regress_another_observers_result](../../tests/lab/test_kafka_analysis_store.py#L242) — Реальная логика AnalysisStore и контрактов. In-memory DB double имитирует PostgreSQL; не реальный сервер/транзакционные блокировки.

**Сохранённые свидетельства:** b04 (локальный материал: `verification/block-04/summary.md`), b05_unit (локальный материал: `verification/block-05/unit-verification.json`), b05_runtime (локальный материал: `verification/block-05/workflow-probe.json`), full_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-verification.json`), junit_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`), live_cycle (локальный материал: `verification/kafka-aml-2026-10-04/live-cycle.json`), code_review (локальный материал: `verification/kafka-aml-2026-10-04/final-code-review.md`).


**Свежая регрессия 07.10.2026:** результат (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-verification.json`), JUnit (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-tests.xml`). Указанные выше тестовые семейства прошли; для FIX-21/22 это только защитные контроли evaluator, не экспертная приёмка.

**Пробел:** Failure/recovery покрыты doubles и SQLite, но нет живой fault-injection серии Kafka/PG/API. Повреждённый HTTP-ответ повторяется как NOT_READY без общего лимита (ограничение сохранённого review).

**Ответственный:** Разработчик агента. **Следующая работа:** QA шаг 5: отдельные остановки worker/API/PG, потеря ACK, DLQ и duplicate, сверка event_id/job_id/report digest; отдельно определить retry exhaustion.

## FIX-20 · Общий бюджет задержки и асинхронный job lifecycle (P1)

**Риск:** Кандидат в draft превышает предел отдельного запуска, оператор ждёт синхронно; быстрые отказы скрывают длительность полезной работы.

**Ожидается по аудиту:** Job lifecycle доступен без синхронного ожидания полного графа; общий deadline ограничивает все попытки; late result не становится новым отчётом; NFR-07/08 рассчитаны по утверждённым границам с отдельными failure rates.

**Существующие проверки:**

- [tests/workflow/test_controller.py::test_timer_persists_timeout_before_ignoring_worker_returns_and_late_result_cannot_replace_it](../../tests/workflow/test_controller.py#L105) — Реальные controller и SQLite, потоки/timer. FakeWorker; модель не вызывается.
- [tests/workflow/test_worker.py::test_stalled_readiness_is_terminated](../../tests/workflow/test_worker.py#L112) — Реальный Windows multiprocessing spawn и остановка child. Bootstrap/handler синтетические; нет модели/Ollama.
- [tests/workflow/test_worker.py::test_stalled_execution_deadline_kills_and_invalidates](../../tests/workflow/test_worker.py#L129) — Реальный Windows multiprocessing spawn и остановка child. Bootstrap/handler синтетические; нет модели/Ollama.
- [tests/runtime/test_service.py::test_invalid_selection_retries_once_with_safe_feedback_and_shared_deadline](../../tests/runtime/test_service.py#L97) — Реальный runtime service с синтетическим demo snapshot. FakeIndex/transport/vectors; нет живой модели.
- [tests/lab/test_kafka_analysis_consumer.py::test_tick_reconciles_then_submits_at_most_two_requests](../../tests/lab/test_kafka_analysis_consumer.py#L451) — Реальная consumer/scheduler логика; один mapped сценарий использует настоящую SQLite. Kafka/PG/HTTP doubles и fake worker; не live fault injection.

**Сохранённые свидетельства:** b04_cold_failure (локальный материал: `verification/block-04/runtime-probe-cold.json`), b05_unit (локальный материал: `verification/block-05/unit-verification.json`), b05_runtime (локальный материал: `verification/block-05/workflow-probe.json`), full_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-verification.json`), junit_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`), code_review (локальный материал: `verification/kafka-aml-2026-10-04/final-code-review.md`).


**Свежая регрессия 07.10.2026:** результат (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-verification.json`), JUnit (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-tests.xml`). Указанные выше тестовые семейства прошли; для FIX-21/22 это только защитные контроли evaluator, не экспертная приёмка.

**Пробел:** Реальные spawn lifecycle проверки используют синтетический handler. Нет репрезентативной latency серии моделей; per-call timeout не задаёт общий жёсткий deadline scheduler tick.

**Ответственный:** Backend/SRE. **Следующая работа:** QA шаги 5–7: separate startup/prepare/job/click-to-visible timings, cold/warm и failures; согласовать общий scheduler budget, не выдавать малую выборку за SLA p95.

## FIX-21 · SME-контракты, независимый Golden, шаблоны и глоссарий (P1)

**Риск:** Нельзя независимо измерить качество и применимость правил; prompt tuning на единственном эталоне не подтверждает обобщение.

**Ожидается по аудиту:** Каждый ожидаемый вывод/отказ объяснён SME и конкретным источником; метки retrieval отделены от applicable rules; утверждены шаблоны/термины и достаточность; dataset split имеет происхождение и не пересекается с prompt-примерами.

**Существующие проверки:**

Прямых тестов приёмки нет (`test_refs=[]`).

Защитные контроли, которые **не** измеряют выполнение этого FIX:

- [tests/evaluator/test_core.py::EvaluatorControls::test_independence_and_category_coverage_are_required](../../tests/evaluator/test_core.py#L306).
- [tests/evaluator/test_core.py::EvaluatorControls::test_sme_metadata_is_required_and_never_fabricated](../../tests/evaluator/test_core.py#L312).

**Сохранённые свидетельства:** b02 (локальный материал: `verification/block-02/summary.md`), b03 (локальный материал: `verification/block-03/summary.md`), b05_unit (локальный материал: `verification/block-05/unit-verification.json`), b05_runtime (локальный материал: `verification/block-05/workflow-probe.json`), full_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-verification.json`), junit_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`).


**Свежая регрессия 07.10.2026:** результат (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-verification.json`), JUnit (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-tests.xml`). Указанные выше тестовые семейства прошли; для FIX-21/22 это только защитные контроли evaluator, не экспертная приёмка.

**Пробел:** Нет независимого SME-утверждённого Golden, templates/glossary и split по семействам. Автотесты evaluator подтверждают только отказ при отсутствии этих свидетельств.

**Ответственный:** AML SME / владелец требований. **Следующая работа:** QA шаг 3: подготовить учебный versioned manifest и форму SME; цель аудита 40 A15/B8/C7/D6/E4 остаётся внешним критерием. Пилот 10 кейсов × 3 повтора не заменяет независимые 40.

## FIX-22 · Квалифицированная оценка качества и операторского эффекта (P1)

**Риск:** Невозможно подтвердить полезность, принятие и экономию времени либо обосновать бизнес-выпуск.

**Ожидается по аудиту:** Измерители M-02/03 калиброваны; отдельно достигнуты пороги по выбранному профилю: Faithfulness ≥90% для PoC/MVP или ≥95% для Production, Answer Relevance ≥90%. M-08≥80% с правками<20% достигнут по утверждённой методике; критические ошибки учитываются отдельно. TTI reduction≥50% и search reduction≥70% достигнуты при сопоставимом качестве. Сохранены знаменатели и ограничения выборки. Измеренное недостижение обязательного порога даёт FAIL и не закрывает приёмку; недостаточная/нулевая база даёт not_measured, а не 0/100%. Калибровка измерителя сама по себе не является достижением качества продукта.

**Существующие проверки:**

Прямых тестов приёмки нет (`test_refs=[]`).

Защитные контроли, которые **не** измеряют выполнение этого FIX:

- [tests/evaluator/test_core.py::EvaluatorControls::test_external_metrics_are_required](../../tests/evaluator/test_core.py#L31).
- [tests/evaluator/test_core.py::EvaluatorControls::test_external_missing_evidence_or_denominator_is_not_measured](../../tests/evaluator/test_core.py#L36).

**Сохранённые свидетельства:** b03 (локальный материал: `verification/block-03/summary.md`), b03_cli (локальный материал: `verification/block-03/cli-process-controls.json`), b05_unit (локальный материал: `verification/block-05/unit-verification.json`), full_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-verification.json`), junit_2026_10_05 (локальный материал: `verification/kafka-aml-2026-10-04/full/unit-tests.xml`).


**Свежая регрессия 07.10.2026:** результат (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-verification.json`), JUnit (локальный материал: `verification/qa-step-01-2026-10-07/coverage/regression/unit-tests.xml`). Указанные выше тестовые семейства прошли; для FIX-21/22 это только защитные контроли evaluator, не экспертная приёмка.

**Пробел:** Нет фактических SME ratings, принятия M-08, edit-rate или сопоставимых manual/assisted времён. Нет калиброванного Judge; тест оценщика не измеряет пользу продукта.

**Ответственный:** Руководитель AML / исследователь операторского процесса. **Следующая работа:** QA шаги 8 и 10: rubric, дефектные пары и agreement с человеком; затем реальные paired sessions с качеством и знаменателями. До получения данных сохранять not_measured.

## Как использовать карту

При реализации QA шагов 2–10 выбирать риск/FIX, добавлять сценарий и привязывать его точный node-id и результат к новой версии карты. Строгие проверки чисел, identity, разрешений и жизненного цикла остаются обязательными независимо от оценки Judge. Для модельных запусков сохранять идентичность модели, корпуса, промпта, фактические исходы всех попыток и знаменатели. Измеренный FAIL нельзя скрыть успешным retry; недостающая приёмка остаётся INCOMPLETE/not_measured.

Источники block-05 и аудита использованы только для чтения. Новые HTTP/Kafka/UI доказательства уточняют устаревшие пункты старой карты, но не изменяют их историю. Исходный проект, аудит, Git и службы этим шагом не изменялись.
