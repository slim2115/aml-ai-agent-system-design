-- Bootstrap substitutes __SCHEMA__ with the fixed name reference or practice.
-- These fixtures are synthetic learning material, not approved AML rules.
CREATE TABLE __SCHEMA__.clients (
    client_id text PRIMARY KEY CHECK (client_id ~ '^C-[0-9]{5}$'),
    risk_category text NOT NULL CHECK (risk_category IN ('low', 'medium', 'high')),
    kyc_status text NOT NULL CHECK (kyc_status IN ('verified', 'pending', 'rejected')),
    client_since date NOT NULL
);
CREATE TABLE __SCHEMA__.cards (
    card_id text PRIMARY KEY CHECK (card_id ~ '^CARD-[0-9]{4}$'),
    client_id text NOT NULL REFERENCES __SCHEMA__.clients(client_id),
    card_status text NOT NULL CHECK (card_status IN ('active', 'blocked', 'closed')),
    UNIQUE (card_id, client_id)
);
CREATE TABLE __SCHEMA__.incidents (
    incident_id text PRIMARY KEY CHECK (incident_id ~ '^INC-[0-9]{6}$'),
    case_id text NOT NULL UNIQUE,
    client_id text NOT NULL REFERENCES __SCHEMA__.clients(client_id),
    alert_type text NOT NULL CHECK (alert_type IN
        ('structuring', 'high_risk_country', 'velocity', 'threshold_breach', 'kyc_mismatch')),
    created_at timestamptz NOT NULL
);
CREATE TABLE __SCHEMA__.transactions (
    tx_id text PRIMARY KEY CHECK (tx_id ~ '^TX-[0-9]{4}$'),
    client_id text NOT NULL REFERENCES __SCHEMA__.clients(client_id),
    card_id text NOT NULL,
    amount numeric(38,2) NOT NULL CHECK (amount >= 0 AND amount <> 'NaN'::numeric),
    currency text NOT NULL CHECK (currency IN ('RUB', 'USD')),
    counterparty text NOT NULL,
    purpose text NOT NULL,
    channel text NOT NULL CHECK (channel IN ('transfer', 'online', 'atm', 'pos')),
    status text NOT NULL CHECK (status IN ('completed', 'pending', 'failed', 'cancelled')),
    timestamp timestamptz NOT NULL,
    FOREIGN KEY (card_id, client_id) REFERENCES __SCHEMA__.cards(card_id, client_id)
);
CREATE INDEX ON __SCHEMA__.transactions (client_id, timestamp, tx_id);
CREATE INDEX ON __SCHEMA__.incidents (client_id, created_at, incident_id);
CREATE TABLE __SCHEMA__.rules (
    rule_id text NOT NULL,
    version text NOT NULL,
    title text NOT NULL,
    regulation_ref text NOT NULL,
    category text NOT NULL,
    text text NOT NULL,
    effective_from date NOT NULL,
    PRIMARY KEY (rule_id, version)
);
CREATE TABLE __SCHEMA__.case_notes (
    id uuid PRIMARY KEY,
    incident_id text NOT NULL REFERENCES __SCHEMA__.incidents(incident_id),
    body text NOT NULL CHECK (char_length(body) BETWEEN 1 AND 4000
                             AND body ~ '[^[:space:]]'),
    revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (updated_at >= created_at)
);
CREATE INDEX ON __SCHEMA__.case_notes (incident_id, created_at, id);
COMMENT ON TABLE __SCHEMA__.rules IS
    'Synthetic demo corpus; not legal advice or approved AML policy.';
COMMENT ON TABLE __SCHEMA__.case_notes IS
    'Practice notes only; not operator approvals, export receipts or case closure.';
