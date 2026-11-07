-- Workbench conversations, kept per project so a reload (or another tab) picks up where the chat left off.

CREATE TABLE IF NOT EXISTS chat_sessions (
    id         text PRIMARY KEY,
    title      text,                            -- from the first user message
    messages   jsonb NOT NULL DEFAULT '[]',     -- the agent's full message history, to restore its context
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chat_events (    -- what the page shows: user and assistant text, tool calls, errors
    id         bigserial PRIMARY KEY,
    session_id text NOT NULL REFERENCES chat_sessions (id) ON DELETE CASCADE,
    event      jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_events_session ON chat_events (session_id, id);
