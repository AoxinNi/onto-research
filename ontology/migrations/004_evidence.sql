-- Evidence attached to links: computed from data (series co-movement, co-mentions in news) or noted by an agent.
-- Evidence annotates a link; it does not change the graph, so it is not reviewed.

CREATE TABLE IF NOT EXISTS link_evidence (
    id          bigserial PRIMARY KEY,
    link_id     text NOT NULL REFERENCES links (id) ON DELETE CASCADE,
    kind        text NOT NULL CHECK (kind IN ('series', 'news', 'note')),
    ref         text NOT NULL,                 -- series pair, 'co-mentions', or the note's own key
    verdict     text NOT NULL CHECK (verdict IN ('supports', 'reverse', 'contradicts', 'weak', 'insufficient')),
    strength    double precision NOT NULL CHECK (strength BETWEEN 0 AND 1),
    summary     text NOT NULL,
    stat        jsonb NOT NULL DEFAULT '{}',
    author      text NOT NULL,
    computed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (link_id, kind, ref)
);
CREATE INDEX IF NOT EXISTS link_evidence_link ON link_evidence (link_id);
