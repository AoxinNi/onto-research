-- Ontology core: type registry, objects, links, changesets and the operation log used for undo.

CREATE TABLE IF NOT EXISTS types (
    kind          text NOT NULL CHECK (kind IN ('object', 'link')),
    name          text NOT NULL,
    spec          jsonb NOT NULL,
    status        text NOT NULL CHECK (status IN ('accepted', 'proposed', 'rejected')),
    source        text NOT NULL CHECK (source IN ('seed', 'proposal')),
    justification text,
    proposed_by   text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    decided_at    timestamptz,
    PRIMARY KEY (kind, name)
);

CREATE TABLE IF NOT EXISTS changesets (
    id           text PRIMARY KEY,
    actor        text NOT NULL,
    summary      text,
    status       text NOT NULL CHECK (status IN ('open', 'pending', 'accepted', 'rejected', 'reverted')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    submitted_at timestamptz,
    decided_at   timestamptz
);
CREATE INDEX IF NOT EXISTS changesets_actor_status ON changesets (actor, status);

CREATE TABLE IF NOT EXISTS objects (
    id           text PRIMARY KEY,
    type         text NOT NULL,
    key          text NOT NULL,
    name         text NOT NULL,
    aliases      text[] NOT NULL DEFAULT '{}',
    terms        text[] NOT NULL DEFAULT '{}',  -- normalized key, name and aliases, for matching
    description  text,
    props        jsonb NOT NULL DEFAULT '{}',
    provenance   jsonb NOT NULL,
    status       text NOT NULL CHECK (status IN ('active', 'archived')),
    merged_into  text REFERENCES objects (id),
    changeset_id text REFERENCES changesets (id),
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (type, key)
);
CREATE INDEX IF NOT EXISTS objects_terms ON objects USING gin (terms);

CREATE TABLE IF NOT EXISTS links (
    id           text PRIMARY KEY,
    type         text NOT NULL,
    from_id      text NOT NULL REFERENCES objects (id),
    to_id        text NOT NULL REFERENCES objects (id),
    props        jsonb NOT NULL DEFAULT '{}',
    provenance   jsonb NOT NULL,
    confidence   double precision CHECK (confidence BETWEEN 0 AND 1),
    valid_from   timestamptz,
    valid_to     timestamptz,
    rationale    text NOT NULL,
    status       text NOT NULL CHECK (status IN ('active', 'archived')),
    changeset_id text REFERENCES changesets (id),
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CHECK (from_id <> to_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS links_unique_active ON links (type, from_id, to_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS links_from ON links (from_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS links_to ON links (to_id) WHERE status = 'active';

-- One row per row-level change. before/after are full row images; before IS NULL means the row was created.
CREATE TABLE IF NOT EXISTS operations (
    id           bigserial PRIMARY KEY,
    changeset_id text NOT NULL REFERENCES changesets (id),
    op           text NOT NULL,
    target_table text NOT NULL CHECK (target_table IN ('objects', 'links')),
    target_id    text NOT NULL,
    before       jsonb,
    after        jsonb,
    rationale    text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS operations_target ON operations (target_id);
