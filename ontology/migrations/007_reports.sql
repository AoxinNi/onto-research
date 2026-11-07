-- Research reports written from the graph. Each version keeps the fingerprint of the graph it was written
-- from, so the timeline can show the report that matched a past state.

CREATE TABLE IF NOT EXISTS reports (
    id          text PRIMARY KEY,
    version     integer NOT NULL,
    status      text NOT NULL CHECK (status IN ('ready', 'failed')),
    title       text,
    summary     text,
    body        jsonb NOT NULL DEFAULT '{}',     -- {sections: [{heading, sentences: [{id, text, nodes, links, from}]}]}
    graph_hash  text NOT NULL,                   -- fingerprint of the material graph state it describes
    model       text,
    trigger     text NOT NULL,                   -- auto | manual
    error       text,
    seconds     real,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS reports_created ON reports (created_at DESC);
