-- Data layer: series (what can be collected), observations (the data points) and bindings (which node a
-- series describes). Bindings are part of the reviewed ontology; observations are written by collectors.

CREATE TABLE IF NOT EXISTS series (
    id            text PRIMARY KEY,
    key           text NOT NULL UNIQUE,          -- canonical source + params, e.g. binance_klines:interval=1h,symbol=BTCUSDT
    source        text NOT NULL,
    params        jsonb NOT NULL DEFAULT '{}',
    label         text NOT NULL,
    unit          text,
    frequency     text NOT NULL,                 -- nominal spacing of points: 1h, 8h, 1d, 1mo …
    status        text NOT NULL DEFAULT 'idle' CHECK (status IN ('idle', 'ok', 'error')),
    last_ts       timestamptz,                   -- newest observation
    last_fetch_at timestamptz,
    last_error    text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS observations (
    series_id text NOT NULL REFERENCES series (id) ON DELETE CASCADE,
    ts        timestamptz NOT NULL,
    value     double precision NOT NULL,
    payload   jsonb,
    PRIMARY KEY (series_id, ts)
);

CREATE TABLE IF NOT EXISTS bindings (
    id           text PRIMARY KEY,
    node_id      text NOT NULL REFERENCES objects (id),
    series_id    text NOT NULL REFERENCES series (id),
    role         text NOT NULL,
    provenance   jsonb NOT NULL,
    rationale    text NOT NULL,
    status       text NOT NULL CHECK (status IN ('active', 'archived')),
    changeset_id text REFERENCES changesets (id),  -- NULL: applied directly (type template or a human)
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS bindings_unique_active ON bindings (node_id, role) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS bindings_series ON bindings (series_id) WHERE status = 'active';

ALTER TABLE operations DROP CONSTRAINT IF EXISTS operations_target_table_check;
ALTER TABLE operations ADD CONSTRAINT operations_target_table_check
    CHECK (target_table IN ('objects', 'links', 'bindings'));
