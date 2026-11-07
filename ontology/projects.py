"""Ontology projects: several independent ontologies in one database, one Postgres schema each.

A project's schema holds the full set of tables (types, objects, links, changesets, data, news, evidence), so
the store and every tool work unchanged once the connection points at it. The empty registry lives in
`onto_research.projects` until a user creates their first project.

    python -m ontology.cli projects                      # list
    python -m ontology.cli --project <id> changesets     # any command, against one project
"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from .db import connect, migrate
from .errors import OntologyError
from .store import OntologyStore

DEFAULT = "research"
PROJECT_SCHEMA_PREFIX = "onto_research_p_"
FORMAT = "ontology-researchreport/1"
OLD_FORMATS = {"btc-ontology/1"}  # files exported before the rename still import
ID = re.compile(r"^[a-z][a-z0-9_-]{1,40}$")
SAMPLE_NODES = 60  # nodes per project sent to the home page for its mini constellation

REGISTRY = Path(__file__).parent / "schema" / "bootstrap.sql"

# None is reserved for trusted CLI and background work. The local web app is
# scoped to legacy/development projects with no account owner.
workspace_owner: ContextVar[str | None] = ContextVar("workspace_owner", default=None)
OWNER_FILTER = "(%s::text IS NULL OR owner_user_id IS NOT DISTINCT FROM NULLIF(%s::text, ''))"


def _owner_params() -> tuple[str | None, str | None]:
    owner = workspace_owner.get()
    return owner, owner


def ensure_registry(conn: psycopg.Connection) -> None:
    if conn.execute("SELECT to_regclass('onto_research.web_model_settings') AS registry").fetchone()["registry"]:
        return
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('onto_research.bootstrap'))")
        if not conn.execute("SELECT to_regclass('onto_research.web_model_settings') AS registry").fetchone()["registry"]:
            conn.execute(REGISTRY.read_text(encoding="utf-8"))


def active_ids(conn: psycopg.Connection) -> list[str]:
    ensure_registry(conn)
    return [r["id"] for r in conn.execute(
        f"SELECT id FROM onto_research.projects WHERE status = 'active' AND {OWNER_FILTER} ORDER BY updated_at DESC, id",
        _owner_params(),
    )]


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def get(conn: psycopg.Connection, project_id: str) -> dict[str, Any]:
    ensure_registry(conn)
    row = conn.execute(f"SELECT * FROM onto_research.projects WHERE id = %s AND {OWNER_FILTER}",
                       (project_id, *_owner_params())).fetchone()
    if not row:
        raise OntologyError(f"no project '{project_id}'")
    return row


def schema_of(conn: psycopg.Connection, project_id: str) -> str:
    return get(conn, project_id)["schema_name"]


def project_connect(project_id: str = DEFAULT, url: str | None = None) -> psycopg.Connection:
    """Open a session pinned to an existing project's schema."""
    conn = connect(url)
    try:
        schema = schema_of(conn, project_id)
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
    except BaseException:
        conn.close()
        raise
    return conn


def _slug(name: str) -> str:
    ascii_part = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:20]
    return f"{ascii_part or 'p'}-{uuid.uuid4().hex[:6]}" if ascii_part else f"p-{uuid.uuid4().hex[:8]}"


def create(conn: psycopg.Connection, name: str, description: str | None = None, project_id: str | None = None) -> dict[str, Any]:
    """A new, empty project with the seed types."""
    ensure_registry(conn)
    if not name.strip():
        raise OntologyError("project name is empty")
    pid = project_id or _slug(name)
    if not ID.match(pid):
        raise OntologyError("project id must be lowercase letters, digits, - or _, starting with a letter")
    if conn.execute("SELECT 1 FROM onto_research.projects WHERE id = %s", (pid,)).fetchone():
        raise OntologyError(f"project '{pid}' already exists")
    schema = PROJECT_SCHEMA_PREFIX + pid
    with conn.transaction():
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(
            "INSERT INTO onto_research.projects (id, name, description, schema_name, owner_user_id) VALUES (%s, %s, %s, %s, %s)",
            (pid, name.strip(), (description or "").strip() or None, schema, workspace_owner.get() or None),
        )
        conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
        migrate(conn)
        OntologyStore(conn, actor="human").sync_seed_types()
    return summary(conn, get(conn, pid))


def _url(conn: psycopg.Connection) -> str:
    return make_conninfo(conn.info.dsn, password=conn.info.password) if conn.info.password else conn.info.dsn


def update(conn: psycopg.Connection, project_id: str, name: str | None = None, description: str | None = None,
           status: str | None = None) -> dict[str, Any]:
    p = get(conn, project_id)
    if status and status not in ("active", "archived"):
        raise OntologyError("status must be active or archived")
    if name is not None and not name.strip():
        raise OntologyError("project name is empty")
    conn.execute(
        f"""UPDATE onto_research.projects SET name = %s, description = %s, status = %s, updated_at = now()
            WHERE id = %s AND {OWNER_FILTER}""",
        (name.strip() if name else p["name"], description.strip() or None if description is not None else p["description"],
         status or p["status"], project_id, *_owner_params()),
    )
    return summary(conn, get(conn, project_id))


def claim_legacy(conn: psycopg.Connection, project_id: str, user_id: str) -> None:
    """Bind one reviewed legacy project. Existing ownership cannot be reassigned."""
    if workspace_owner.get() is not None or not user_id.strip():
        raise OntologyError("project claims require a database operator and a user ID")
    ensure_registry(conn)
    row = conn.execute(
        """UPDATE onto_research.projects SET owner_user_id = %s
           WHERE id = %s AND owner_user_id IS NULL RETURNING id""",
        (user_id, project_id),
    ).fetchone()
    if row is None and get(conn, project_id)["owner_user_id"] != user_id:
        raise OntologyError("project already belongs to another user")


def summary(conn: psycopg.Connection, p: dict[str, Any]) -> dict[str, Any]:
    """Registry row plus what the home page shows: counts, pending review, data health and a graph sample."""
    out = {k: _iso(v) for k, v in p.items() if k != "owner_user_id"}
    with project_connect(p["id"], _url(conn)) as pc:
        q = lambda s, *a: pc.execute(s, a).fetchone()  # noqa: E731
        out["stats"] = {
            "nodes": q("SELECT count(*) AS n FROM objects WHERE status = 'active'")["n"],
            "links": q("SELECT count(*) AS n FROM links WHERE status = 'active'")["n"],
            "feeds": q("SELECT count(*) AS n FROM bindings WHERE status = 'active'")["n"],
            "pending": q("""SELECT count(*) AS n FROM changesets c WHERE status IN ('open', 'pending')
                            AND EXISTS (SELECT 1 FROM operations o WHERE o.changeset_id = c.id)""")["n"],
            "feed_errors": q("""SELECT count(DISTINCT s.id) AS n FROM series s JOIN bindings b ON b.series_id = s.id
                                WHERE b.status = 'active' AND s.status = 'error'""")["n"],
            "news_7d": q("""SELECT count(DISTINCT m.document_id) AS n FROM mentions m JOIN documents d ON d.id = m.document_id
                            WHERE d.published_at > now() - interval '7 days'""")["n"],
        }
        last = q("""SELECT greatest((SELECT max(updated_at) FROM objects), (SELECT max(updated_at) FROM links),
                                    (SELECT max(coalesce(decided_at, submitted_at, created_at)) FROM changesets)) AS t""")["t"]
        out["last_activity"] = _iso(last or p["updated_at"])
        nodes = pc.execute(
            """SELECT o.id, o.type, (SELECT count(*) FROM links l WHERE l.status = 'active' AND (l.from_id = o.id OR l.to_id = o.id)) AS deg
               FROM objects o WHERE o.status = 'active' ORDER BY deg DESC, o.created_at LIMIT %s""", (SAMPLE_NODES,),
        ).fetchall()
        ids = [n["id"] for n in nodes]
        links = pc.execute(
            "SELECT from_id, to_id FROM links WHERE status = 'active' AND from_id = ANY(%s) AND to_id = ANY(%s)", (ids, ids),
        ).fetchall()
        index = {nid: i for i, nid in enumerate(ids)}
        out["sample"] = {"nodes": [[n["type"], n["deg"]] for n in nodes],
                         "links": [[index[l["from_id"]], index[l["to_id"]]] for l in links]}
    return out


def list_all(conn: psycopg.Connection, include_archived: bool = True) -> list[dict[str, Any]]:
    ensure_registry(conn)
    rows = conn.execute(
        f"""SELECT * FROM onto_research.projects WHERE (%s OR status = 'active') AND {OWNER_FILTER}
            ORDER BY updated_at DESC, id""",
        (include_archived, *_owner_params()),
    ).fetchall()
    return [summary(conn, r) for r in rows]


# ---------------------------------------------------------------- export / import


def export(conn: psycopg.Connection, project_id: str) -> dict[str, Any]:
    """The project's accepted content as portable JSON: custom types, nodes, links and data bindings."""
    p = get(conn, project_id)
    with project_connect(project_id, _url(conn)) as pc:
        types = pc.execute(
            "SELECT kind, name, spec FROM types WHERE status = 'accepted' AND source = 'proposal' ORDER BY kind, name"
        ).fetchall()
        nodes = pc.execute(
            "SELECT id, type, key, name, aliases, description, props, provenance FROM objects WHERE status = 'active' ORDER BY created_at"
        ).fetchall()
        links = pc.execute(
            """SELECT type, from_id, to_id, rationale, provenance, confidence, props, valid_from, valid_to
               FROM links WHERE status = 'active' ORDER BY created_at"""
        ).fetchall()
        bindings = pc.execute(
            """SELECT b.node_id, b.role, b.rationale, s.source, s.params, s.unit, s.label FROM bindings b
               JOIN series s ON s.id = b.series_id WHERE b.status = 'active' AND s.source NOT IN ('news_heat', 'news_sentiment')
               ORDER BY b.created_at"""
        ).fetchall()
    return {
        "format": FORMAT,
        "exported_at": datetime.now(UTC).isoformat(),
        "project": {"name": p["name"], "description": p["description"]},
        "types": types,
        "nodes": [{"ref": n["id"], **{k: n[k] for k in ("type", "key", "name", "aliases", "description", "props", "provenance")}}
                  for n in nodes],
        "links": [{"type": l["type"], "from": l["from_id"], "to": l["to_id"], "rationale": l["rationale"],
                   "provenance": l["provenance"], "confidence": l["confidence"], "props": l["props"],
                   "valid_from": _iso(l["valid_from"]), "valid_to": _iso(l["valid_to"])} for l in links],
        "bindings": [{"node": b["node_id"], "role": b["role"], "source": b["source"], "params": b["params"],
                      "unit": b["unit"], "label": b["label"], "rationale": b["rationale"]} for b in bindings],
    }


def template() -> dict[str, Any]:
    """An empty, valid research project import file."""
    return {
        "format": FORMAT,
        "project": {"name": "示例研究项目", "description": "填写研究主题、节点和关系"},
        "types": [],
        "nodes": [],
        "links": [],
        "bindings": [],
    }


def import_doc(conn: psycopg.Connection, doc: dict[str, Any], name: str | None = None) -> dict[str, Any]:
    """Create a new project from an export (or the template). Its content is accepted as it arrives."""
    if not isinstance(doc, dict) or doc.get("format") not in {FORMAT, *OLD_FORMATS}:
        raise OntologyError(f"not a {FORMAT} file (download the import template to see the format)")
    meta = doc.get("project") or {}
    p = create(conn, name or meta.get("name") or "导入的本体", meta.get("description"))
    try:
        with project_connect(p["id"], _url(conn)) as pc:
            human = OntologyStore(pc, actor="human")
            importer = OntologyStore(pc, actor="import")
            for t in doc.get("types") or []:
                spec = t.get("spec") or {}
                importer.propose_type(t["kind"], t["name"], spec.get("description", ""), "导入", spec.get("layer"),
                                      spec.get("properties"), spec.get("from"), spec.get("to"))
                human.decide_type(t["kind"], t["name"], accept=True)
            importer.registry = importer.load_registry()
            refs: dict[str, str] = {}
            for n in doc.get("nodes") or []:
                made = importer.upsert_node(n["type"], n["name"], n.get("provenance") or {"kind": "user"}, "导入",
                                            key=n.get("key"), aliases=n.get("aliases"), description=n.get("description"),
                                            props=n.get("props"), force_new=True)["node"]
                refs[str(n.get("ref") or n["name"])] = made["id"]
            for l in doc.get("links") or []:
                if l.get("from") not in refs or l.get("to") not in refs:
                    raise OntologyError(f"link {l.get('type')} refers to a node that is not in the file: {l.get('from')} -> {l.get('to')}")
                importer.create_link(l["type"], refs[l["from"]], refs[l["to"]], l.get("provenance") or {"kind": "user"},
                                     l.get("rationale") or "导入", l.get("confidence"), l.get("props"),
                                     l.get("valid_from"), l.get("valid_to"))
            if doc.get("nodes") or doc.get("links"):
                cs = importer.submit_changeset("导入")
                human.accept_changeset(cs["id"])
            for b in doc.get("bindings") or []:
                if b.get("node") not in refs:
                    raise OntologyError(f"binding refers to a node that is not in the file: {b.get('node')}")
                human.bind_direct(refs[b["node"]], b["source"], b.get("params") or {}, b.get("role") or "value",
                                  b.get("rationale") or "导入", unit=b.get("unit"), label=b.get("label"))
    except (OntologyError, KeyError, TypeError) as e:
        drop(conn, p["id"])  # an import is all or nothing
        raise OntologyError(f"import failed: {e if isinstance(e, OntologyError) else f'missing field {e}'}") from None
    return summary(conn, get(conn, p["id"]))


def drop(conn: psycopg.Connection, project_id: str) -> None:
    """Delete a project and everything in it. Only for failed imports and the CLI; the UI archives instead."""
    p = get(conn, project_id)
    if not p["schema_name"].startswith(PROJECT_SCHEMA_PREFIX):
        raise OntologyError("refusing to drop a schema outside this application")
    conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(p["schema_name"])))
    conn.execute("DELETE FROM onto_research.projects WHERE id = %s", (project_id,))
