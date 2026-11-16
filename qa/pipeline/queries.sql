-- Шаг 5. Только чтение QA PostgreSQL на 127.0.0.1:56432.
-- Подключение: БД и роль reader/student из приватного lab-connection.json своего run_id.
-- Все запросы — SELECT; выполняйте их по одному в SQL-клиенте.
-- В запросах 2–7 замените нулевой UUID на event_id из API или QA-P*.json / PF-*.json.
-- Он намеренно не совпадает ни с одним учебным событием.
-- practice.* — отдельные SQL-упражнения, не источник неизменяемого AML-снимка.
-- PostgreSQL хранит очередь и проекцию. Задания/отчёты/решения принадлежат SQLite workflow.

-- 1. Убедиться, что подключились к своей QA-БД и ожидаемому порту.
SELECT current_database() AS database_name,
       current_user AS role_name,
       inet_server_addr() AS server_address,
       inet_server_port() AS server_port;

-- 2. Запрос: статус очереди, связанное задание и версия отчёта.
-- Пустой результат: очередь ещё не приняла событие, выбран иной UUID/стенд
-- либо сообщение отклонено в DLQ. Одного пустого SELECT недостаточно для диагноза.
SELECT event_id, incident_id, engine, status,
       job_id, job_revision, report_id, report_version, content_sha256,
       last_error_code, created_at, updated_at
FROM kafka_lab.analysis_requests
WHERE event_id = '00000000-0000-0000-0000-000000000000'::uuid;

-- 3. Координаты сохранённых доставок и исходный хеш события.
-- offset уникален только внутри пары (topic, partition).
-- Для повторной публикации того же тела появляются новые координаты,
-- duplicate=true и прежний event_id. Повтор той же позиции Kafka
-- не создаёт дополнительную строку: PK=(topic, partition, offset_value).
SELECT topic, partition, offset_value AS kafka_offset,
       event_id, duplicate, canonical_sha256, recorded_at
FROM kafka_lab.analysis_deliveries
WHERE event_id = '00000000-0000-0000-0000-000000000000'::uuid
ORDER BY topic, partition, offset_value;

-- 4. JOIN запроса с доставками: одна строка запроса может иметь много доставок.
-- LEFT JOIN сохраняет запрос и при отсутствии доставки: поля d.* будут NULL.
-- canonical_sha256 — SHA256 канонического события; это НЕ хеш отчёта.
SELECT r.event_id, r.status, r.job_id,
       d.topic, d.partition, d.offset_value AS kafka_offset, d.duplicate,
       d.canonical_sha256 = r.canonical_sha256 AS event_hash_matches,
       r.report_id, r.report_version, r.content_sha256 AS report_sha256
FROM kafka_lab.analysis_requests AS r
LEFT JOIN kafka_lab.analysis_deliveries AS d ON d.event_id = r.event_id
WHERE r.event_id = '00000000-0000-0000-0000-000000000000'::uuid
ORDER BY d.topic, d.partition, d.offset_value;

-- 5. Агрегация: доставки нельзя считать новыми заданиями.
-- COUNT(d.event_id), а не COUNT(*): LEFT JOIN может содержать пустую правую строку.
-- Этот SELECT видит проекцию PG. Наличие ровно одной admission/report в SQLite
-- отдельно подтверждается тестом и API, а не этой агрегацией.
SELECT r.event_id, r.job_id, r.status,
       COUNT(d.event_id) AS stored_kafka_positions,
       COUNT(d.event_id) FILTER (WHERE d.duplicate) AS duplicate_publications,
       COUNT(DISTINCT r.job_id) AS projected_job_count,
       r.report_id, r.report_version, r.content_sha256
FROM kafka_lab.analysis_requests AS r
LEFT JOIN kafka_lab.analysis_deliveries AS d ON d.event_id = r.event_id
WHERE r.event_id = '00000000-0000-0000-0000-000000000000'::uuid
GROUP BY r.event_id, r.job_id, r.status,
         r.report_id, r.report_version, r.content_sha256;

-- 6. JSONB: извлечь договорённые поля события и задания.
-- #>> извлекает вложенное значение как text; ->> — поле верхнего уровня как text.
-- Для queued job_json ещё NULL. Для refused/failed result имеет другую структуру.
SELECT event_id,
       event_json ->> 'event_type' AS event_type,
       event_json ->> 'occurred_at' AS occurred_at,
       event_json #>> '{window,start_inclusive}' AS window_start,
       event_json #>> '{window,end_exclusive}' AS window_end,
       job_json ->> 'job_id' AS projected_job_id,
       job_json ->> 'status' AS projected_workflow_status,
       job_json ->> 'revision' AS projected_revision,
       job_json #>> '{result,report_id}' AS nested_report_id,
       job_json #>> '{result,report_version}' AS nested_report_version
FROM kafka_lab.analysis_requests
WHERE event_id = '00000000-0000-0000-0000-000000000000'::uuid;

-- 7. Проверка внутренних ссылок проекции для готового отчёта.
-- NULL при отсутствии строки/полей не равно успешной проверке.
-- content_sha256 нужно сопоставить с GET /v1/reports/{report_id}/reference?version=...
-- Не хешируйте job_json::text: формат JSONB PostgreSQL не равен канонизации приложения.
SELECT event_id,
       job_id IS NOT DISTINCT FROM (job_json ->> 'job_id') AS job_id_matches,
       job_revision::text IS NOT DISTINCT FROM (job_json ->> 'revision') AS revision_matches,
       report_id IS NOT DISTINCT FROM (job_json #>> '{result,report_id}') AS report_id_matches,
       report_version::text IS NOT DISTINCT FROM (job_json #>> '{result,report_version}') AS report_version_matches,
       report_id, report_version, content_sha256
FROM kafka_lab.analysis_requests
WHERE event_id = '00000000-0000-0000-0000-000000000000'::uuid
  AND status = 'succeeded';

-- 8. Очередь, ожидающая передачи или обновления состояния задания.
-- submitted означает принятие workflow, но не готовность отчёта.
-- Возраст строки — наблюдение, не SLA и не доказательство зависания.
SELECT event_id, status, job_id, job_revision, last_error_code,
       clock_timestamp() - updated_at AS time_since_projection_update
FROM kafka_lab.analysis_requests
WHERE status IN ('queued', 'submitted')
ORDER BY updated_at, event_id;

-- 9. Последние запросы и их диагностические коды.
-- Отсутствие строки с конфликтом/невалидным JSON ожидаемо: такие сообщения
-- ищут в AML DLQ по исходным topic/partition/offset, а не в этой таблице.
SELECT event_id, incident_id, status, job_id, last_error_code, updated_at
FROM kafka_lab.analysis_requests
ORDER BY created_at DESC, event_id
LIMIT 20;

-- 10. Найти несоответствие хеша события между запросом и доставкой.
-- Для согласованных данных ожидается пустой результат.
-- Это проверка одного инварианта, а не доказательство корректности всей цепочки.
SELECT r.event_id, d.topic, d.partition, d.offset_value,
       r.canonical_sha256 AS request_sha256,
       d.canonical_sha256 AS delivery_sha256
FROM kafka_lab.analysis_requests AS r
JOIN kafka_lab.analysis_deliveries AS d ON d.event_id = r.event_id
WHERE r.canonical_sha256 IS DISTINCT FROM d.canonical_sha256;
