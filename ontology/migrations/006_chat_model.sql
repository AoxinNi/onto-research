-- Which model a workbench conversation uses (NULL: the server default, WORKBENCH_MODEL).
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS model text;
