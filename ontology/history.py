"""Commit history of the accepted graph, for the timeline (phase 1 of git-style versioning).

Whenever the accepted graph changes (a changeset accepted or reverted, a type decided, a template binding
applied, a CLI decision), a commit stores a full snapshot of it: nodes, links, data bindings and the evidence
at that moment. Commits form a chain through `parent`; ids are content hashes. Pending work is not part of
history until it is accepted.

Data values are not copied into snapshots: observations are append-only and timestamped, so a past state shows
the values as of its commit time. Reports are matched by time as well.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .errors import OntologyError
from .evidence import summaries
from .store import OntologyStore

ACCEPTED = "({t}.changeset_id IS NULL OR c.status = 'accepted')"


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def snapshot(store: OntologyStore) -> dict[str, Any]:
    """The accepted graph as plain JSON, in the shape the canvas reads."""
    conn = store.conn
    nodes = [store._node_out(r) | {"pending": False} for r in conn.execute(
        f"""SELECT o.* FROM objects o LEFT JOIN changesets c ON c.id = o.changeset_id
            WHERE o.status = 'active' AND {ACCEPTED.format(t='o')} ORDER BY o.created_at""")]
    ids = {n["id"] for n in nodes}
    links = [store._link_out(r) | {"pending": False} for r in conn.execute(
        f"""SELECT l.* FROM links l LEFT JOIN changesets c ON c.id = l.changeset_id
            WHERE l.status = 'active' AND {ACCEPTED.format(t='l')} ORDER BY l.created_at""")
        if r["from_id"] in ids and r["to_id"] in ids]
    bindings = [dict(r) for r in conn.execute(
        f"""SELECT b.node_id, b.role, b.series_id FROM bindings b LEFT JOIN changesets c ON c.id = b.changeset_id
            WHERE b.status = 'active' AND {ACCEPTED.format(t='b')} AND b.node_id = ANY(%s) ORDER BY b.created_at""",
        (list(ids),))]
    link_ids = {l["id"] for l in links}
    evidence = {k: v for k, v in summaries(conn).items() if k in link_ids}
    return {"nodes": nodes, "links": links, "bindings": bindings, "evidence": evidence}


def content_hash(snap: dict[str, Any]) -> str:
    """What counts as a change: nodes, links and bindings. Evidence is recomputed hourly and is not a change."""
    core = {k: snap[k] for k in ("nodes", "links", "bindings")}
    return hashlib.sha256(json.dumps(core, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def diff_stats(before: dict[str, Any] | None, after: dict[str, Any]) -> dict[str, int]:
    def index(snap, key):
        return {x["id"]: x for x in (snap or {}).get(key, [])}

    out = {}
    for key, name in (("nodes", "nodes"), ("links", "links")):
        a, b = index(before, key), index(after, key)
        out[f"{name}_added"] = len(b.keys() - a.keys())
        out[f"{name}_removed"] = len(a.keys() - b.keys())
        out[f"{name}_changed"] = sum(1 for k in a.keys() & b.keys() if a[k] != b[k])
    ab = {(x["node_id"], x["role"], x["series_id"]) for x in (before or {}).get("bindings", [])}
    bb = {(x["node_id"], x["role"], x["series_id"]) for x in after["bindings"]}
    out["bindings_added"], out["bindings_removed"] = len(bb - ab), len(ab - bb)
    out["nodes"], out["links"] = len(after["nodes"]), len(after["links"])
    return out


def describe(stats: dict[str, int]) -> str:
    parts = []
    for key, label in (("nodes", "节点"), ("links", "关系")):
        for kind, sign in (("added", "+"), ("removed", "−"), ("changed", "~")):
            if stats.get(f"{key}_{kind}"):
                parts.append(f"{sign}{stats[f'{key}_{kind}']} {label}")
    if stats.get("bindings_added") or stats.get("bindings_removed"):
        parts.append(f"数据绑定 +{stats.get('bindings_added', 0)}/−{stats.get('bindings_removed', 0)}")
    return "，".join(parts) or "无结构变化"


def last(conn: psycopg.Connection) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM commits ORDER BY created_at DESC, id LIMIT 1").fetchone()


def commit_if_changed(store: OntologyStore, author: str, message: str) -> dict[str, Any] | None:
    """Record a commit if the accepted graph differs from the last one. Returns the new commit, or None."""
    snap = snapshot(store)
    content = content_hash(snap)
    prev = last(store.conn)
    if prev and prev["content"] == content:
        return None
    stats = diff_stats(prev["snapshot"] if prev else None, snap)
    now = datetime.now(UTC)
    full = message if not prev else f"{message}（{describe(stats)}）"
    cid = hashlib.sha256(f"{prev['id'] if prev else ''}|{content}|{now.isoformat()}|{author}|{full}".encode()).hexdigest()[:16]
    store.conn.execute(
        """INSERT INTO commits (id, parent, content, author, message, stats, snapshot, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (cid, prev["id"] if prev else None, content, author, full, Jsonb(stats), Jsonb(snap), now),
    )
    return {"id": cid, "parent": prev["id"] if prev else None, "author": author, "message": full, "stats": stats,
            "created_at": now.isoformat()}


def log(conn: psycopg.Connection, limit: int = 1000) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, parent, author, message, stats, created_at FROM commits ORDER BY created_at DESC LIMIT %s", (limit,),
    ).fetchall()
    return [{**r, "created_at": _iso(r["created_at"])} for r in reversed(rows)]


def graph_at(store: OntologyStore, commit_id: str) -> dict[str, Any]:
    """A past state in the canvas's shape, with data values as of the commit time."""
    row = store.conn.execute("SELECT * FROM commits WHERE id = %s", (commit_id,)).fetchone()
    if not row:
        raise OntologyError(f"no commit '{commit_id}'")
    snap = row["snapshot"]
    at = row["created_at"]
    data: dict[str, list[dict[str, Any]]] = {}
    for b in snap.get("bindings", []):
        try:
            s = store.series.get_series(b["series_id"])
        except OntologyError:
            continue
        data.setdefault(b["node_id"], []).append({"role": b["role"], "pending": False, **store.series.summary(s, at)})
    return {
        "commit": {"id": row["id"], "parent": row["parent"], "author": row["author"], "message": row["message"],
                   "stats": row["stats"], "created_at": _iso(at)},
        "graph": {"nodes": snap["nodes"], "links": snap["links"], "data": data, "evidence": snap.get("evidence", {})},
    }
