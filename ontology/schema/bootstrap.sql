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
