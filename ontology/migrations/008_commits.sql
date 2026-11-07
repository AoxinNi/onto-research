-- Commit history of the accepted graph: one row per change, with a full snapshot, so any past state can be
-- shown again. Content-addressed: id = hash(parent, content hash, time, author, message).

CREATE TABLE IF NOT EXISTS commits (
    id          text PRIMARY KEY,
    parent      text REFERENCES commits (id),
    content     text NOT NULL,           -- hash of the graph content alone (nodes, links, bindings)
    author      text NOT NULL,
    message     text NOT NULL,
    stats       jsonb NOT NULL DEFAULT '{}',
    snapshot    jsonb NOT NULL,          -- {nodes, links, bindings, evidence}
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS commits_created ON commits (created_at);
