-- Text layer: news documents, the nodes they mention, and event candidates waiting to be promoted.

CREATE TABLE IF NOT EXISTS feeds (
    name          text PRIMARY KEY,
    url           text NOT NULL,
    lang          text NOT NULL,
    status        text NOT NULL DEFAULT 'idle' CHECK (status IN ('idle', 'ok', 'error')),
    last_fetch_at timestamptz,
    last_error    text
);

CREATE TABLE IF NOT EXISTS documents (
    id            text PRIMARY KEY,
    feed          text NOT NULL,
    url           text NOT NULL UNIQUE,
    title         text NOT NULL,
    summary       text,
    lang          text,
    published_at  timestamptz NOT NULL,
    fetched_at    timestamptz NOT NULL DEFAULT now(),
    relevant      boolean,         -- NULL: not screened yet; false: nothing BTC-related, never sent to a model
    extracted_at  timestamptz,
    extract_error text
);
CREATE INDEX IF NOT EXISTS documents_published ON documents (published_at DESC);
CREATE INDEX IF NOT EXISTS documents_todo ON documents (published_at) WHERE extracted_at IS NULL AND relevant IS NOT FALSE;

CREATE TABLE IF NOT EXISTS mentions (
    document_id text NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    node_id     text NOT NULL REFERENCES objects (id) ON DELETE CASCADE,  -- goes when an undo removes the node
    sentiment   double precision NOT NULL CHECK (sentiment BETWEEN -1 AND 1),
    relevance   double precision NOT NULL CHECK (relevance BETWEEN 0 AND 1),
    quote       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, node_id)
);
CREATE INDEX IF NOT EXISTS mentions_node ON mentions (node_id);

CREATE TABLE IF NOT EXISTS event_candidates (
    id           bigserial PRIMARY KEY,
    document_id  text NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    key          text NOT NULL,    -- normalized title, groups reports of the same event
    title        text NOT NULL,
    category     text NOT NULL,
    severity     integer NOT NULL CHECK (severity BETWEEN 1 AND 5),
    node_ids     text[] NOT NULL DEFAULT '{}',
    state        text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'promoted')),
    promoted_to  text REFERENCES objects (id) ON DELETE SET NULL,  -- a rejected promotion stays 'promoted', so it is not retried
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS event_candidates_open ON event_candidates (key) WHERE state = 'open';
