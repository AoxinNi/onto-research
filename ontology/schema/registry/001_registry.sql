CREATE SCHEMA IF NOT EXISTS onto_research;

CREATE TABLE IF NOT EXISTS onto_research.projects (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    description text,
    schema_name text NOT NULL UNIQUE,
    status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE onto_research.projects ENABLE ROW LEVEL SECURITY;

ALTER TABLE onto_research.projects ADD COLUMN IF NOT EXISTS owner_user_id text;
CREATE INDEX IF NOT EXISTS projects_owner_idx ON onto_research.projects (owner_user_id);

CREATE TABLE IF NOT EXISTS onto_research.web_sessions (
    token_hash text PRIMARY KEY,
    user_id text NOT NULL,
    expires_at timestamptz NOT NULL
);
ALTER TABLE onto_research.web_sessions ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS onto_research.web_registered_users (
    user_id text PRIMARY KEY,
    provider text NOT NULL CHECK (provider IN ('sms', 'wechat')),
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE onto_research.web_registered_users ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS onto_research.web_sms_requests (
    recipient_hash text NOT NULL,
    sent_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE onto_research.web_sms_requests ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS onto_research.web_auth_challenges (
    token_hash text PRIMARY KEY,
    provider text NOT NULL CHECK (provider IN ('sms', 'wechat')),
    payload jsonb NOT NULL,
    attempts int NOT NULL DEFAULT 0,
    expires_at timestamptz NOT NULL
);
ALTER TABLE onto_research.web_auth_challenges ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS onto_research.web_model_settings (
    user_id text PRIMARY KEY,
    base_url text NOT NULL,
    model text NOT NULL,
    auth_kind text NOT NULL CHECK (auth_kind IN ('api_key', 'bearer')),
    encrypted_key bytea NOT NULL
);
ALTER TABLE onto_research.web_model_settings ENABLE ROW LEVEL SECURITY;
