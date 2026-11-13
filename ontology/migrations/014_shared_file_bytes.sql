-- A file's bytes may live in the shared onto_research.file_blobs (by sha256) instead of in its row: NULL here.
ALTER TABLE library_files ALTER COLUMN bytes DROP NOT NULL;
