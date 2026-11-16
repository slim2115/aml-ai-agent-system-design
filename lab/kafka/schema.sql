-- Executed once inside KafkaStore.initialize's locked transaction.
-- New namespace only; reference/practice are never altered.
CREATE SCHEMA kafka_lab AUTHORIZATION aml_lab_admin;
COMMENT ON SCHEMA kafka_lab IS
    'aml-ai-agent-system-rebuild:kafka-lab:v1:synthetic-only';

CREATE TABLE kafka_lab.processed_events (
    event_id uuid PRIMARY KEY,
    schema_version integer NOT NULL CHECK (schema_version = 1),
    event_type text NOT NULL CHECK (event_type = 'incident.review.requested.v1'),
    incident_id text NOT NULL CHECK (incident_id ~ '^INC-[0-9]{6}$'),
    occurred_at timestamptz NOT NULL,
    canonical_sha256 text NOT NULL CHECK (canonical_sha256 ~ '^[0-9a-f]{64}$'),
    result_json jsonb NOT NULL CHECK (jsonb_typeof(result_json) = 'object'),
    topic text NOT NULL CHECK (char_length(topic) BETWEEN 1 AND 249),
    partition integer NOT NULL CHECK (partition >= 0),
    offset_value bigint NOT NULL CHECK (offset_value >= 0),
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX ON kafka_lab.processed_events (processed_at DESC, event_id);
COMMENT ON TABLE kafka_lab.processed_events IS
    'Synthetic snapshot bookkeeping; not an AML risk decision, approval or workflow report.';

CREATE TABLE kafka_lab.deliveries (
    topic text NOT NULL CHECK (char_length(topic) BETWEEN 1 AND 249),
    partition integer NOT NULL CHECK (partition >= 0),
    offset_value bigint NOT NULL CHECK (offset_value >= 0),
    event_id uuid NOT NULL REFERENCES kafka_lab.processed_events(event_id),
    canonical_sha256 text NOT NULL CHECK (canonical_sha256 ~ '^[0-9a-f]{64}$'),
    duplicate boolean NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (topic, partition, offset_value)
);
CREATE INDEX ON kafka_lab.deliveries (event_id);
COMMENT ON TABLE kafka_lab.deliveries IS
    'One row per Kafka coordinate; replaying the same coordinate does not count another delivery.';
