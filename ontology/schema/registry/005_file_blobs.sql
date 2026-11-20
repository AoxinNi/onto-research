-- Library files shared between projects, stored once by content. A project's library_files row with bytes NULL
-- reads its bytes here by sha256. Big Bang merges move the files they copy here, so a merged project and the
-- projects it came from hold one copy of each PDF between them.
CREATE TABLE IF NOT EXISTS onto_research.file_blobs (
    sha256     text PRIMARY KEY,
    bytes      bytea NOT NULL,
    size       integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE onto_research.file_blobs ENABLE ROW LEVEL SECURITY;
