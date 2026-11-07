"""Postgres connection and migrations."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

MIGRATIONS = Path(__file__).parent / "migrations"


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL 未配置。请在服务环境中填写 PostgreSQL 连接串。")
    return url


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or database_url(), autocommit=True, row_factory=dict_row)


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply migrations in file-name order; returns the ones applied now."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, applied_at timestamptz DEFAULT now())")
    done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
    applied = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.stem in done:
            continue
        with conn.transaction():
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
        applied.append(path.stem)
    return applied
