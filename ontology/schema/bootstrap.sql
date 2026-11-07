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
