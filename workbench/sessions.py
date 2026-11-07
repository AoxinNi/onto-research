"""Workbench conversations: persisted per project, restored on reload.

The page's log (chat_events) and the agent's message history (chat_sessions.messages) are stored separately:
the first is what a person reads, the second is what the model needs to continue the conversation.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

SHOWN = {"user", "assistant", "tool", "error", "note"}  # event types worth replaying


def _out(r: dict[str, Any]) -> dict[str, Any]:
    return {"id": r["id"], "title": r["title"] or "新对话", "model": r.get("model"),
            "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat()}


def create(conn: psycopg.Connection) -> dict[str, Any]:
    return _out(conn.execute("INSERT INTO chat_sessions (id) VALUES (%s) RETURNING *", (f"c_{uuid.uuid4().hex[:12]}",)).fetchone())


def get(conn: psycopg.Connection, session_id: str) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM chat_sessions WHERE id = %s", (session_id,)).fetchone()
    return _out(r) if r else None


def latest_or_create(conn: psycopg.Connection) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM chat_sessions ORDER BY updated_at DESC LIMIT 1").fetchone()
    return _out(r) if r else create(conn)


def recent(conn: psycopg.Connection, limit: int = 30) -> list[dict[str, Any]]:
    """Conversations that have something in them, newest first."""
    rows = conn.execute(
        """SELECT s.* FROM chat_sessions s WHERE EXISTS (SELECT 1 FROM chat_events e WHERE e.session_id = s.id)
           ORDER BY s.updated_at DESC LIMIT %s""", (limit,),
    ).fetchall()
    return [_out(r) for r in rows]


def events(conn: psycopg.Connection, session_id: str) -> list[dict[str, Any]]:
    return [r["event"] for r in conn.execute(
        "SELECT event FROM chat_events WHERE session_id = %s ORDER BY id", (session_id,))]


def append(conn: psycopg.Connection, session_id: str, event: dict[str, Any]) -> None:
    if event.get("type") not in SHOWN or (event["type"] == "note" and not event.get("text")):
        return
    conn.execute("INSERT INTO chat_events (session_id, event) VALUES (%s, %s)", (session_id, Jsonb(event)))
    if event["type"] == "user":
        conn.execute("UPDATE chat_sessions SET title = coalesce(title, %s), updated_at = now() WHERE id = %s",
                     (event["text"][:40], session_id))
    else:
        conn.execute("UPDATE chat_sessions SET updated_at = now() WHERE id = %s", (session_id,))


def load_messages(conn: psycopg.Connection, session_id: str) -> list[dict[str, Any]]:
    r = conn.execute("SELECT messages FROM chat_sessions WHERE id = %s", (session_id,)).fetchone()
    return list(r["messages"]) if r else []


def save_messages(conn: psycopg.Connection, session_id: str, messages: list[dict[str, Any]]) -> None:
    conn.execute("UPDATE chat_sessions SET messages = %s, updated_at = now() WHERE id = %s", (Jsonb(messages), session_id))


def set_model(conn: psycopg.Connection, session_id: str, model: str | None) -> None:
    conn.execute("UPDATE chat_sessions SET model = %s WHERE id = %s", (model, session_id))
