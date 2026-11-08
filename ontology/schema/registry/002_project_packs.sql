-- Type packs a project is seeded with (ontology/schema/packs). Projects created before packs existed used the
-- market types, so existing rows get {market}; new projects default to {research}.
ALTER TABLE onto_research.projects ADD COLUMN IF NOT EXISTS packs text[] NOT NULL DEFAULT '{market}';
ALTER TABLE onto_research.projects ALTER COLUMN packs SET DEFAULT '{research}';
