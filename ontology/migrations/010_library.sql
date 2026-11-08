-- Library: the project's sources (papers, books, web pages…), their files and extracted text, excerpts a reader
-- marked in them, and free-form research notes. This is raw material, like series and news documents: it is
-- written directly (by a person, the agent or an import) and attributed, not reviewed. Linking it into the graph
-- is reviewed; see groundings in 011.

CREATE TABLE IF NOT EXISTS library_items (
    id         text PRIMARY KEY,                -- it_<12hex>
    citekey    text NOT NULL UNIQUE,            -- cited in manuscripts as [@citekey]
    kind       text NOT NULL CHECK (kind IN ('article', 'preprint', 'book', 'chapter', 'thesis', 'conference',
                                             'report', 'webpage', 'dataset', 'software', 'other')),
    csl        jsonb NOT NULL,                  -- CSL-JSON item: the single source for citations and exports
    title      text NOT NULL,                   -- title … abstract are copied out of csl for search and sorting
    authors    text NOT NULL DEFAULT '',        -- "Zhang, San; Li, Wei"
    year       integer,
    doi        text,
    url        text,
    abstract   text,
    tags       text[] NOT NULL DEFAULT '{}',
    reading    text NOT NULL DEFAULT 'unread' CHECK (reading IN ('unread', 'reading', 'read')),
    origin     jsonb NOT NULL,                  -- where the metadata came from: {kind: doi|arxiv|isbn|url|openalex|file|bibtex|ris|csl|manual, ref}
    added_by   text NOT NULL,                   -- human | workbench-agent | import
    status     text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS library_items_doi ON library_items (lower(doi)) WHERE doi IS NOT NULL AND status = 'active';
CREATE INDEX IF NOT EXISTS library_items_tags ON library_items USING gin (tags);
CREATE INDEX IF NOT EXISTS library_items_created ON library_items (created_at DESC);

CREATE TABLE IF NOT EXISTS library_files (
    id             text PRIMARY KEY,            -- f_<12hex>
    item_id        text NOT NULL REFERENCES library_items (id) ON DELETE CASCADE,
    filename       text NOT NULL,
    media_type     text NOT NULL CHECK (media_type IN ('application/pdf', 'text/plain', 'text/markdown', 'text/x-web-extract')),
    bytes          bytea NOT NULL,              -- web captures keep only the extracted text, never the page's HTML
    size           integer NOT NULL,
    sha256         text NOT NULL,
    pages          integer,
    extract_status text NOT NULL DEFAULT 'pending' CHECK (extract_status IN ('pending', 'ok', 'empty', 'failed')),
    extract_error  text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (item_id, sha256)
);

CREATE TABLE IF NOT EXISTS library_chunks (   -- extracted text, for the reader, excerpt checks and agent search
    file_id    text NOT NULL REFERENCES library_files (id) ON DELETE CASCADE,
    seq        integer NOT NULL,
    item_id    text NOT NULL,
    page       integer,                         -- PDF page, from 1; NULL for plain text
    char_start integer NOT NULL,                -- offset in the file's full text
    text       text NOT NULL,
    PRIMARY KEY (file_id, seq)
);
CREATE INDEX IF NOT EXISTS library_chunks_item ON library_chunks (item_id, file_id, seq);

CREATE TABLE IF NOT EXISTS excerpts (          -- a passage of a source and the reader's comment on it
    id         text PRIMARY KEY,                -- ex_<12hex>
    item_id    text NOT NULL REFERENCES library_items (id),
    file_id    text REFERENCES library_files (id) ON DELETE SET NULL,
    quote      text NOT NULL,                   -- verbatim
    page       integer,
    anchor     jsonb,                           -- {file_id, seq, start, end} into library_chunks
    verified   boolean NOT NULL,                -- the quote was found in the extracted text
    comment    text,
    color      text,
    author     text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS excerpts_item ON excerpts (item_id);

CREATE TABLE IF NOT EXISTS notes (             -- Markdown; may cite [@citekey] and point at [[n_…]], [[ex_…]], [[it_…]]
    id         text PRIMARY KEY,                -- nt_<12hex>
    title      text NOT NULL,
    body       text NOT NULL DEFAULT '',
    refs       jsonb NOT NULL DEFAULT '{}',     -- {items, excerpts, nodes} parsed from the body on save, for back-links
    author     text NOT NULL,
    pinned     boolean NOT NULL DEFAULT false,
    status     text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS notes_refs ON notes USING gin (refs);
