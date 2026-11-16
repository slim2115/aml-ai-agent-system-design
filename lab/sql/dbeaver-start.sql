-- Select "AML — просмотр". Run each statement with Ctrl+Enter.
-- Read-only starter: no changes to the database.
SELECT current_database() AS database_name,
       current_user AS role_name,
       current_setting('TimeZone') AS timezone,
       current_setting('search_path') AS search_path;

SELECT client_id, risk_category, kyc_status
FROM practice.clients
ORDER BY client_id;

SELECT id, incident_id, body, revision
FROM practice.case_notes
ORDER BY created_at, id;

-- After preparing the Kafka lab:
SELECT event_id, incident_id, processed_at
FROM kafka_lab.processed_events
ORDER BY processed_at DESC
LIMIT 10;
