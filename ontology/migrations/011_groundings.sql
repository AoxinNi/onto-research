-- Groundings: a library source backs (or argues against) a node or a link. Like bindings, they join raw
-- material to the reviewed graph, so an agent's grounding goes into its changeset; one a person adds directly
-- has no changeset and is in effect at once.

CREATE TABLE IF NOT EXISTS groundings (
    id           text PRIMARY KEY,              -- g_<12hex>
    node_id      text REFERENCES objects (id),
    link_id      text REFERENCES links (id),
    item_id      text NOT NULL REFERENCES library_items (id),
    excerpt_id   text REFERENCES excerpts (id), -- the passage that shows it, when there is one
    stance       text NOT NULL CHECK (stance IN ('states', 'supports', 'contradicts', 'qualifies')),
    rationale    text NOT NULL,
    provenance   jsonb NOT NULL,
    status       text NOT NULL CHECK (status IN ('active', 'archived')),
    changeset_id text REFERENCES changesets (id),
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CHECK ((node_id IS NULL) <> (link_id IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS groundings_unique_active
    ON groundings (coalesce(node_id, link_id), item_id, coalesce(excerpt_id, ''), stance) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS groundings_node ON groundings (node_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS groundings_link ON groundings (link_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS groundings_item ON groundings (item_id) WHERE status = 'active';

ALTER TABLE operations DROP CONSTRAINT IF EXISTS operations_target_table_check;
ALTER TABLE operations ADD CONSTRAINT operations_target_table_check
    CHECK (target_table IN ('objects', 'links', 'bindings', 'groundings'));
