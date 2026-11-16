-- Решения для восстановленного исходного набора. Все изменения 11–12 откатываются.
-- Запуск целого файла не должен менять таблицы practice.
-- Не запускайте внутри уже открытой пользовательской транзакции.

-- 01: TX-8900 = 600000.00 RUB, затем TX-7700 и TX-7701 = 95000.00 RUB.
SELECT tx_id, amount, currency
FROM practice.transactions
WHERE currency = 'RUB'
ORDER BY amount DESC, tx_id
LIMIT 3;

-- 02: 5 строк, каждый инцидент имеет ровно одного клиента.
SELECT i.incident_id, c.client_id, c.risk_category, c.kyc_status, i.alert_type
FROM practice.incidents AS i
JOIN practice.clients AS c ON c.client_id = i.client_id
ORDER BY i.incident_id;

-- 03: 12, 1, 1, 1, 0 для C-00045/55/66/77/99.
-- COUNT(tx_id) пропускает NULL, добавленный LEFT JOIN при отсутствии операции.
SELECT c.client_id, count(t.tx_id) AS transaction_count
FROM practice.clients AS c
LEFT JOIN practice.transactions AS t ON t.client_id = c.client_id
GROUP BY c.client_id
ORDER BY c.client_id;

-- 04: RUB 14 строк / 1790000.00; USD 1 строка / 1500000.00.
SELECT currency, count(*) AS transaction_count, sum(amount) AS total_amount
FROM practice.transactions
WHERE status = 'completed'
GROUP BY currency
ORDER BY currency;

-- 05: C-00045 / RUB / 12 / 1140000.00; это учебное условие SQL.
SELECT client_id, currency, count(*) AS transaction_count, sum(amount) AS total_amount
FROM practice.transactions
WHERE status = 'completed'
GROUP BY client_id, currency
HAVING count(*) > 3
ORDER BY client_id, currency;

-- 06: 4 строки; UTC задан явно, конец интервала не включён.
SELECT tx_id, timestamp, amount, currency
FROM practice.transactions
WHERE client_id = 'C-00045'
  AND timestamp >= timestamptz '2026-07-11 00:00:00+00'
  AND timestamp < timestamptz '2026-07-12 00:00:00+00'
ORDER BY timestamp, tx_id;

-- 07: 12 строк; последняя накопленная сумма RUB = 1140000.00.
SELECT tx_id, timestamp, amount, currency,
       row_number() OVER (PARTITION BY currency ORDER BY timestamp, tx_id) AS sequence_number,
       sum(amount) OVER (PARTITION BY currency ORDER BY timestamp, tx_id
                         ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_amount
FROM practice.transactions
WHERE client_id = 'C-00045'
ORDER BY currency, timestamp, tx_id;

-- 08: 5 строк, включая C-00099 с NULL.
WITH ranked AS (
    SELECT tx_id, client_id, timestamp, amount, currency,
           row_number() OVER (PARTITION BY client_id ORDER BY timestamp DESC, tx_id DESC) AS rn
    FROM practice.transactions
)
SELECT c.client_id, r.tx_id, r.timestamp, r.amount, r.currency
FROM practice.clients AS c
LEFT JOIN ranked AS r ON r.client_id = c.client_id AND r.rn = 1
ORDER BY c.client_id;

-- 09: INC-000999 / C-00099. Отсутствие операций означает только отсутствие строк.
SELECT i.incident_id, i.client_id
FROM practice.incidents AS i
WHERE NOT EXISTS (
    SELECT 1 FROM practice.transactions AS t WHERE t.client_id = i.client_id
)
ORDER BY i.incident_id;

-- 10: 5 строк. При одинаковой дате используем version как стабильный tie-breaker;
-- для реальных редакций понадобилась бы отдельно согласованная семантика версий.
WITH ranked AS (
    SELECT rule_id, version, title,
           row_number() OVER (PARTITION BY rule_id ORDER BY effective_from DESC, version DESC) AS rn
    FROM practice.rules
    WHERE effective_from <= date '2026-07-20'
)
SELECT rule_id, version, title FROM ranked WHERE rn = 1 ORDER BY rule_id;

-- 11: первое изменение вернёт revision=2, повторное — 0 строк.
BEGIN;
INSERT INTO practice.case_notes (id, incident_id, body)
VALUES ('00000000-0000-4000-8000-000000000011', 'INC-000123', 'Учебная заметка');
UPDATE practice.case_notes
SET body = 'Учебная заметка: проверен JOIN', revision = revision + 1,
    updated_at = clock_timestamp()
WHERE id = '00000000-0000-4000-8000-000000000011' AND revision = 1
RETURNING id, body, revision;
UPDATE practice.case_notes
SET body = 'Этот устаревший запрос ничего не изменит', revision = revision + 1,
    updated_at = clock_timestamp()
WHERE id = '00000000-0000-4000-8000-000000000011' AND revision = 1
RETURNING id, revision;
ROLLBACK;
SELECT count(*) AS remaining_exercise_notes FROM practice.case_notes
WHERE id = '00000000-0000-4000-8000-000000000011';

-- 12: четыре ожидаемых ограничения; каждый EXCEPTION откатывает внутренний блок.
-- Если ограничение вдруг отсутствует, RAISE EXCEPTION остановит упражнение.
BEGIN;
DO $$
BEGIN
    BEGIN
        INSERT INTO practice.case_notes (id, incident_id, body)
        VALUES ('00000000-0000-4000-8000-000000000012', 'INC-999999', 'Проверка FK');
        RAISE EXCEPTION 'FK incident_id unexpectedly accepted an unknown incident';
    EXCEPTION WHEN foreign_key_violation THEN NULL;
    END;
    BEGIN
        INSERT INTO practice.case_notes (id, incident_id, body)
        VALUES ('00000000-0000-4000-8000-000000000012', 'INC-000123', '   ');
        RAISE EXCEPTION 'CHECK unexpectedly accepted an empty body';
    EXCEPTION WHEN check_violation THEN NULL;
    END;
    BEGIN
        INSERT INTO practice.clients (client_id, risk_category, kyc_status, client_since)
        VALUES ('C-00045', 'low', 'pending', date '2026-01-01');
        RAISE EXCEPTION 'PK unexpectedly accepted a duplicate client';
    EXCEPTION WHEN unique_violation THEN NULL;
    END;
    BEGIN
        UPDATE practice.transactions SET card_id = 'CARD-1002' WHERE tx_id = 'TX-7700';
        RAISE EXCEPTION 'Composite FK unexpectedly accepted another clients card';
    EXCEPTION WHEN foreign_key_violation THEN NULL;
    END;
END $$;
ROLLBACK;
