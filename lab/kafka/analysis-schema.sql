-- Additive initialization only after checking the existing kafka_lab owner.
CREATE TABLE kafka_lab.analysis_requests (
    event_id uuid PRIMARY KEY,
    incident_id text NOT NULL CHECK (incident_id ~ '^INC-[0-9]{6}$'),
    engine text NOT NULL CHECK (engine IN ('template', 'local-model')),
    canonical_sha256 text NOT NULL CHECK (canonical_sha256 ~ '^[0-9a-f]{64}$'),
    event_json jsonb NOT NULL CHECK (jsonb_typeof(event_json) = 'object'),
    status text NOT NULL DEFAULT 'queued' CHECK (status IN
        ('queued', 'submitted', 'succeeded', 'refused', 'failed', 'cancelled', 'timed_out', 'rejected')),
    job_id text UNIQUE,
    job_revision integer CHECK (job_revision >= 0),
    job_json jsonb CHECK (jsonb_typeof(job_json) = 'object'),
    report_id text,
    report_version integer CHECK (report_version >= 1),
    content_sha256 text CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    last_error_code text CHECK (last_error_code ~ '^[A-Z][A-Z0-9_]{0,63}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((status IN ('queued', 'rejected') AND job_id IS NULL AND job_revision IS NULL AND job_json IS NULL)
        OR (status NOT IN ('queued', 'rejected') AND job_id IS NOT NULL AND job_revision IS NOT NULL AND job_json IS NOT NULL)),
    CHECK ((status = 'succeeded' AND report_id IS NOT NULL AND report_version IS NOT NULL AND content_sha256 IS NOT NULL)
        OR (status <> 'succeeded' AND report_id IS NULL AND report_version IS NULL AND content_sha256 IS NULL))
);
CREATE INDEX ON kafka_lab.analysis_requests (status, updated_at, event_id);
COMMENT ON TABLE kafka_lab.analysis_requests IS
    'aml-ai-agent-system-rebuild:kafka-analysis:v1:synthetic-only';

CREATE TABLE kafka_lab.analysis_deliveries (
    topic text NOT NULL CHECK (topic = 'aml.lab.analysis-requested.v1'),
    partition integer NOT NULL CHECK (partition >= 0),
    offset_value bigint NOT NULL CHECK (offset_value >= 0),
    event_id uuid NOT NULL REFERENCES kafka_lab.analysis_requests(event_id),
    canonical_sha256 text NOT NULL CHECK (canonical_sha256 ~ '^[0-9a-f]{64}$'),
    duplicate boolean NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (topic, partition, offset_value)
);
CREATE INDEX ON kafka_lab.analysis_deliveries (event_id);
COMMENT ON TABLE kafka_lab.analysis_deliveries IS
    'aml-ai-agent-system-rebuild:kafka-analysis:v1:synthetic-only';
