-- Paper digests: a reading of one PDF in the library, made by the model from its MinerU Markdown. The Markdown and
-- figures themselves stay in the local cache (keyed by the PDF's sha256); this row keeps the part that cost a model
-- call: a small graph of the paper's logic and the overview sections the reader shows.

CREATE TABLE IF NOT EXISTS paper_digests (
    file_id    text PRIMARY KEY REFERENCES library_files (id) ON DELETE CASCADE,
    item_id    text NOT NULL REFERENCES library_items (id) ON DELETE CASCADE,
    sha256     text NOT NULL,
    status     text NOT NULL DEFAULT 'converting' CHECK (status IN ('converting', 'summarizing', 'ok', 'failed')),
    stage      text,                 -- finer progress while converting: uploading, parsing, downloading
    error      text,
    model      text,
    digest     jsonb,                -- {graph: {nodes, edges}, logic, framework, conclusions, experiments, figures, methods}
    figures    jsonb,                -- the figures and tables MinerU found: [{id, name, kind, page, caption}]
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS paper_digests_item ON paper_digests (item_id);
