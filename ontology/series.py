"""The data plane: series, observations, and the per-node summaries the canvas and agents read.

Collectors write observations here without review. What a node *means* in data terms (its bindings) is
written through OntologyStore, because that is part of the reviewed ontology.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import yaml
from psycopg.types.json import Jsonb

from .errors import OntologyError
from .sources import FREQ_SECONDS, Obs, series_key, source

TEMPLATES = Path(__file__).parent / "schema" / "bindings.yaml"
SPARK_POINTS = 60


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def freq_seconds(freq: str) -> int:
    return FREQ_SECONDS.get(freq, 86400)


def load_templates() -> list[dict[str, Any]]:
    return yaml.safe_load(TEMPLATES.read_text(encoding="utf-8")).get("templates", [])


class SeriesStore:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    # ----------------------------------------------------------------- series

    def ensure_series(self, source_name: str, params: dict[str, Any], unit: str | None = None,
                      label: str | None = None) -> dict[str, Any]:
        """Get or register the series for a source + params (idempotent on the canonical key)."""
        src = source(source_name)
        p = src.clean(params)
        key = series_key(src.name, p)
        row = self.conn.execute("SELECT * FROM series WHERE key = %s", (key,)).fetchone()
        if row:
            return row
        return self.conn.execute(
            """INSERT INTO series (id, key, source, params, label, unit, frequency)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (key) DO UPDATE SET key = EXCLUDED.key RETURNING *""",
            (f"s_{uuid.uuid4().hex[:12]}", key, src.name, Jsonb(p), label or src.label(p), unit or src.unit,
             src.frequency(p)),
        ).fetchone()

    def get_series(self, series_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM series WHERE id = %s OR key = %s", (series_id, series_id)).fetchone()
        if not row:
            raise OntologyError(f"no series '{series_id}'")
        return row

    def collectable(self) -> list[dict[str, Any]]:
        """Series with at least one binding that is in effect (applied directly or in an accepted changeset)."""
        rows = self.conn.execute(
            """SELECT DISTINCT s.* FROM series s JOIN bindings b ON b.series_id = s.id
               LEFT JOIN changesets c ON c.id = b.changeset_id
               WHERE b.status = 'active' AND (b.changeset_id IS NULL OR c.status = 'accepted')
               ORDER BY s.created_at"""
        ).fetchall()
        return [r for r in rows if source(r["source"]).polled]

    def write(self, series_id: str, points: list[Obs]) -> int:
        """Upsert points; returns how many were new or changed."""
        if not points:
            return 0
        with self.conn.transaction():
            n = 0
            with self.conn.cursor() as cur:
                for o in points:
                    cur.execute(
                        """INSERT INTO observations (series_id, ts, value, payload) VALUES (%s, %s, %s, %s)
                           ON CONFLICT (series_id, ts) DO UPDATE SET value = EXCLUDED.value, payload = EXCLUDED.payload
                           WHERE observations.value IS DISTINCT FROM EXCLUDED.value
                              OR observations.payload IS DISTINCT FROM EXCLUDED.payload""",
                        (series_id, o.ts, o.value, Jsonb(o.payload) if o.payload else None),
                    )
                    n += cur.rowcount
            self.conn.execute(
                """UPDATE series SET last_ts = (SELECT max(ts) FROM observations WHERE series_id = %s) WHERE id = %s""",
                (series_id, series_id),
            )
        return n

    def mark(self, series_id: str, error: str | None) -> None:
        self.conn.execute(
            "UPDATE series SET last_fetch_at = now(), status = %s, last_error = %s WHERE id = %s",
            ("error" if error else "ok", error, series_id),
        )

    # ------------------------------------------------------------------ reads

    def points(self, series_id: str, start: datetime | None = None, end: datetime | None = None,
               limit: int = 500) -> list[dict[str, Any]]:
        """The newest `limit` points in [start, end], oldest first."""
        rows = self.conn.execute(
            """SELECT ts, value FROM observations WHERE series_id = %s
                 AND (%s::timestamptz IS NULL OR ts >= %s) AND (%s::timestamptz IS NULL OR ts <= %s)
               ORDER BY ts DESC LIMIT %s""",
            (series_id, start, start, end, end, min(limit, 5000)),
        ).fetchall()
        return [{"ts": _iso(r["ts"]), "value": r["value"]} for r in reversed(rows)]

    def summary(self, s: dict[str, Any], as_of: datetime | None = None) -> dict[str, Any]:
        """Latest value, change against ~24h earlier (or the previous point for slower series), sparkline, health.
        With `as_of`, all of it as things stood at that moment (the timeline)."""
        step = freq_seconds(s["frequency"])
        out: dict[str, Any] = {
            "series_id": s["id"], "key": s["key"], "source": s["source"], "label": s["label"], "unit": s["unit"],
            "frequency": s["frequency"], "status": s["status"], "last_error": s["last_error"],
            "last_fetch_at": _iso(s["last_fetch_at"]), "latest": None, "change": None, "spark": [],
        }
        latest = self.conn.execute(
            """SELECT ts, value, payload FROM observations WHERE series_id = %s AND (%s::timestamptz IS NULL OR ts <= %s)
               ORDER BY ts DESC LIMIT 1""", (s["id"], as_of, as_of)
        ).fetchone()
        polled = source(s["source"]).polled
        if not latest:
            out["stale"] = polled
            return out
        out["latest"] = {"ts": _iso(latest["ts"]), "value": latest["value"],
                         "live": bool((latest["payload"] or {}).get("open"))}  # a bar that is still forming
        span = timedelta(days=1) if step < 86400 else timedelta(seconds=step)
        prev = self.conn.execute(
            "SELECT ts, value FROM observations WHERE series_id = %s AND ts <= %s ORDER BY ts DESC LIMIT 1",
            (s["id"], latest["ts"] - span + timedelta(seconds=1)),
        ).fetchone()
        if prev:
            delta = latest["value"] - prev["value"]
            out["change"] = {"window": "24h" if step < 86400 else "上期", "abs": delta,
                             "pct": delta / abs(prev["value"]) * 100 if prev["value"] else None}
        window = timedelta(seconds=step * 90) if step >= 86400 else timedelta(days=3)
        rows = self.conn.execute(
            "SELECT value FROM observations WHERE series_id = %s AND ts > %s AND ts <= %s ORDER BY ts",
            (s["id"], latest["ts"] - window, latest["ts"]),
        ).fetchall()
        vals = [r["value"] for r in rows]
        if len(vals) > SPARK_POINTS:
            stride = len(vals) / SPARK_POINTS
            vals = [vals[int(i * stride)] for i in range(SPARK_POINTS - 1)] + [vals[-1]]
        out["spark"] = vals
        # Monthly macro data is published weeks after the period it describes; allow for that.
        grace = (timedelta(seconds=step * 3) + source(s["source"]).lag
                 + (timedelta(days=45) if step >= 86400 * 28 else timedelta(0)))
        out["stale"] = polled and (as_of or datetime.now(UTC)) - latest["ts"] > grace
        return out

    def node_data(self, node_ids: list[str] | None = None, as_of: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
        """Bindings with series summaries, grouped by node."""
        rows = self.conn.execute(
            """SELECT b.id AS binding_id, b.node_id, b.role, b.provenance, b.rationale, b.changeset_id,
                      c.status AS cs_status, s.*
               FROM bindings b JOIN series s ON s.id = b.series_id LEFT JOIN changesets c ON c.id = b.changeset_id
               WHERE b.status = 'active' AND (%s::text[] IS NULL OR b.node_id = ANY(%s))
               ORDER BY b.created_at""",
            (node_ids, node_ids),
        ).fetchall()
        out: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(r["node_id"], []).append({
                "binding_id": r["binding_id"], "role": r["role"], "provenance": r["provenance"],
                "rationale": r["rationale"], "pending": r["cs_status"] in ("open", "pending"),
                **self.summary(r, as_of),
            })
        return out
