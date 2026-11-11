-- Data sources the user defines without code (ontology/custom_sources.py): RSS feeds for the news desk, and HTTP
-- JSON or CSV endpoints that become series nodes can be bound to. Shared by every project on this workbench.
CREATE TABLE IF NOT EXISTS onto_research.custom_sources (
    name        text PRIMARY KEY,                    -- the id bindings and feeds refer to; fixed once created
    kind        text NOT NULL CHECK (kind IN ('rss', 'json', 'csv')),
    title       text NOT NULL,
    url         text NOT NULL,                       -- json/csv: may hold {param} placeholders
    config      jsonb NOT NULL DEFAULT '{}'::jsonb,  -- how to read the response; see custom_sources.clean
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE onto_research.custom_sources ENABLE ROW LEVEL SECURITY;
