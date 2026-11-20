-- Manuscripts: papers written inside the project, section by section, in Pandoc Markdown. A person edits the text
-- directly; the agent only suggests edits (section_suggestions), which a person accepts or rejects. Citations are
-- [@citekey] (library_items.citekey) and claim anchors are [text]{.claim node=n_…}.

CREATE TABLE IF NOT EXISTS manuscripts (
    id         text PRIMARY KEY,                -- ms_<12hex>
    title      text NOT NULL,
    kind       text NOT NULL DEFAULT 'article' CHECK (kind IN ('article', 'thesis', 'proposal', 'review', 'report', 'other')),
    csl_style  text NOT NULL DEFAULT 'gb7714-numeric',  -- gb7714-numeric | gb7714-author-date | apa | chicago-author-date | ieee
    lang       text NOT NULL DEFAULT 'zh-CN',
    meta       jsonb NOT NULL DEFAULT '{}',     -- authors [{name, affiliation}], abstract, keywords
    status     text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS manuscript_sections (
    id            text PRIMARY KEY,             -- sec_<12hex>
    manuscript_id text NOT NULL REFERENCES manuscripts (id) ON DELETE CASCADE,
    position      integer NOT NULL,
    level         smallint NOT NULL DEFAULT 1 CHECK (level BETWEEN 1 AND 3),
    heading       text NOT NULL,
    body          text NOT NULL DEFAULT '',     -- Pandoc Markdown
    rev           integer NOT NULL DEFAULT 1,   -- optimistic concurrency: a save names the rev it started from
    updated_by    text NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (manuscript_id, position) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS section_revisions (  -- one author's saves within five minutes of each other are one revision
    section_id text NOT NULL REFERENCES manuscript_sections (id) ON DELETE CASCADE,
    rev        integer NOT NULL,
    heading    text NOT NULL,
    body       text NOT NULL,
    author     text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (section_id, rev)
);

CREATE TABLE IF NOT EXISTS section_suggestions (  -- the agent's proposed edits; a person accepts or rejects each
    id            text PRIMARY KEY,             -- sg_<12hex>
    manuscript_id text NOT NULL REFERENCES manuscripts (id) ON DELETE CASCADE,
    section_id    text REFERENCES manuscript_sections (id) ON DELETE CASCADE,  -- replace: that section; insert_after: the one before (NULL = at the start); outline: NULL
    kind          text NOT NULL CHECK (kind IN ('replace', 'insert_after', 'outline')),
    base_rev      integer,                      -- replace: the rev it was written against; older than the text = stale
    heading       text,
    level         smallint CHECK (level BETWEEN 1 AND 3),
    body          text,                         -- outline: JSON array [{heading, level, intent}]
    rationale     text NOT NULL,
    author        text NOT NULL,
    status        text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'rejected', 'stale')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    decided_at    timestamptz
);
CREATE INDEX IF NOT EXISTS section_suggestions_open ON section_suggestions (manuscript_id) WHERE status IN ('pending', 'stale');
