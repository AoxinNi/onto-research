"""Ontology store: typed objects and links in Postgres, every write recorded in a changeset.

Writes apply immediately (so later calls in the same conversation can use what was just created),
but they belong to a changeset that a human accepts, rejects or reverts. Rejecting or reverting
replays the operation log backwards from the stored row images.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections import deque
from datetime import datetime
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError, model_validator

from .errors import OntologyError
from .registry import LinkType, ObjectType, Registry, build_registry, load_seed, parse_type
from .evidence import summaries
from .providers import PROVIDERS
from .series import SeriesStore, load_templates
from .sources import source

OBJ_COLS = [
    "id", "type", "key", "name", "aliases", "terms", "description", "props", "provenance",
    "status", "merged_into", "changeset_id", "created_at", "updated_at",
]
LINK_COLS = [
    "id", "type", "from_id", "to_id", "props", "provenance", "confidence", "valid_from", "valid_to",
    "rationale", "status", "changeset_id", "created_at", "updated_at",
]
BINDING_COLS = [
    "id", "node_id", "series_id", "role", "provenance", "rationale", "status", "changeset_id", "created_at", "updated_at",
]
TABLE_COLS = {"objects": OBJ_COLS, "links": LINK_COLS, "bindings": BINDING_COLS}
OPEN_STATUSES = ("open", "pending")
ROLE = re.compile(r"^[a-z][a-z_]{1,23}$")
MAX_DEPTH = 3
MAX_PATH_DEPTH = 5


class Provenance(BaseModel):
    """Where a node or link comes from. llm_prior = the model's own background knowledge."""

    kind: Literal["user", "llm_prior", "data_source"]
    ref: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _data_source_needs_ref(self) -> Provenance:
        if self.kind == "data_source" and not self.ref:
            raise ValueError("provenance of kind 'data_source' needs 'ref' (URL or dataset id)")
        return self


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _terms(key: str, name: str, aliases: list[str]) -> list[str]:
    return sorted({normalize(t) for t in [key, name, *aliases] if t and t.strip()})


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _provenance(value: dict[str, Any] | Provenance) -> dict[str, Any]:
    try:
        p = value if isinstance(value, Provenance) else Provenance.model_validate(value)
    except ValidationError as e:
        raise OntologyError(f"invalid provenance: {e.errors()[0]['msg']}") from None
    return p.model_dump(exclude_none=True)


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class OntologyStore:
    def __init__(self, conn: psycopg.Connection, actor: str = "workbench-agent"):
        self.conn = conn
        self.actor = actor
        self.registry = self.load_registry()
        self.series = SeriesStore(conn)

    # ------------------------------------------------------------------ types

    def sync_seed_types(self) -> None:
        """Make the seed YAML the accepted definition of every type it names."""
        with self.conn.transaction():
            for t in load_seed():
                self.conn.execute(
                    """INSERT INTO types (kind, name, spec, status, source)
                       VALUES (%s, %s, %s, 'accepted', 'seed')
                       ON CONFLICT (kind, name) DO UPDATE
                       SET spec = EXCLUDED.spec, status = 'accepted', source = 'seed', decided_at = now()""",
                    (t.kind, t.name, Jsonb(_spec(t))),
                )
        self.registry = self.load_registry()

    def load_registry(self) -> Registry:
        rows = self.conn.execute("SELECT kind, name, spec FROM types WHERE status = 'accepted'").fetchall()
        return build_registry([parse_type(r["kind"], r["name"], r["spec"]) for r in rows])

    def get_schema(self) -> dict[str, Any]:
        out = self.registry.describe()
        out["proposed_types"] = self.conn.execute(
            "SELECT kind, name, spec, justification, proposed_by FROM types WHERE status = 'proposed' ORDER BY created_at"
        ).fetchall()
        return out

    def propose_type(
        self,
        kind: str,
        name: str,
        description: str,
        justification: str,
        layer: str | None = None,
        properties: dict[str, dict[str, Any]] | None = None,
        from_types: list[str] | None = None,
        to_types: list[str] | None = None,
    ) -> dict[str, Any]:
        spec: dict[str, Any] = {"description": description, "properties": properties or {}}
        if kind == "object":
            spec["layer"] = layer
        elif kind == "link":
            spec["from"], spec["to"] = from_types or [], to_types or []
        t = parse_type(kind, name, spec)
        if isinstance(t, LinkType):
            for ref in t.from_types + t.to_types:
                if ref != "*" and ref not in self.registry.object_types:
                    raise OntologyError(f"link endpoint '{ref}' is not an accepted object type")
        existing = self.conn.execute("SELECT status FROM types WHERE kind = %s AND name = %s", (kind, name)).fetchone()
        if existing and existing["status"] == "accepted":
            raise OntologyError(f"{kind} type '{name}' already exists")
        with self.conn.transaction():
            self.conn.execute(
                """INSERT INTO types (kind, name, spec, status, source, justification, proposed_by)
                   VALUES (%s, %s, %s, 'proposed', 'proposal', %s, %s)
                   ON CONFLICT (kind, name) DO UPDATE
                   SET spec = EXCLUDED.spec, status = 'proposed', justification = EXCLUDED.justification,
                       proposed_by = EXCLUDED.proposed_by, created_at = now(), decided_at = NULL""",
                (kind, name, Jsonb(_spec(t)), justification, self.actor),
            )
        return {"proposed": {"kind": kind, "name": name, **_spec(t)}, "status": "proposed",
                "note": "A human must accept this type before it can be used."}

    def decide_type(self, kind: str, name: str, accept: bool) -> None:
        row = self.conn.execute("SELECT status FROM types WHERE kind = %s AND name = %s", (kind, name)).fetchone()
        if not row or row["status"] != "proposed":
            raise OntologyError(f"no proposed {kind} type '{name}'")
        with self.conn.transaction():
            self.conn.execute(
                "UPDATE types SET status = %s, decided_at = now() WHERE kind = %s AND name = %s",
                ("accepted" if accept else "rejected", kind, name),
            )
            if accept:
                self.registry = self.load_registry()

    # ------------------------------------------------------------- changesets

    def _changeset(self) -> str:
        """The actor's open changeset, opened on first write."""
        row = self.conn.execute(
            "SELECT id FROM changesets WHERE actor = %s AND status = 'open' ORDER BY created_at DESC LIMIT 1",
            (self.actor,),
        ).fetchone()
        if row:
            return row["id"]
        cs = _new_id("cs")
        self.conn.execute("INSERT INTO changesets (id, actor, status) VALUES (%s, %s, 'open')", (cs, self.actor))
        return cs

    def _image(self, table: str, row_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(f"SELECT to_jsonb(t) AS img FROM {table} t WHERE id = %s", (row_id,)).fetchone()
        return row["img"] if row else None

    def _log(self, cs: str, op: str, table: str, row_id: str, before: dict | None, rationale: str | None) -> None:
        self.conn.execute(
            """INSERT INTO operations (changeset_id, op, target_table, target_id, before, after, rationale)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (cs, op, table, row_id, Jsonb(before) if before else None, Jsonb(self._image(table, row_id)), rationale),
        )

    def submit_changeset(self, summary: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT id FROM changesets WHERE actor = %s AND status = 'open' ORDER BY created_at DESC LIMIT 1",
            (self.actor,),
        ).fetchone()
        if not row:
            raise OntologyError("nothing to submit: no open changeset")
        with self.conn.transaction():
            self.conn.execute(
                "UPDATE changesets SET status = 'pending', summary = %s, submitted_at = now() WHERE id = %s",
                (summary, row["id"]),
            )
        return self.get_changeset(row["id"])

    def accept_changeset(self, cs: str) -> dict[str, Any]:
        with self.conn.transaction():
            self._require_changeset(cs, OPEN_STATUSES)
            self.conn.execute("UPDATE changesets SET status = 'accepted', decided_at = now() WHERE id = %s", (cs,))
        return self.get_changeset(cs)

    def reject_changeset(self, cs: str) -> dict[str, Any]:
        """Undo a changeset that was never accepted."""
        return self._undo(cs, OPEN_STATUSES, "rejected")

    def revert_changeset(self, cs: str | None = None) -> dict[str, Any]:
        """Undo a changeset (default: the most recent one that is still in effect)."""
        if cs is None:
            row = self.conn.execute(
                """SELECT id FROM changesets WHERE status IN ('open', 'pending', 'accepted')
                   AND EXISTS (SELECT 1 FROM operations o WHERE o.changeset_id = changesets.id)
                   ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()
            if not row:
                raise OntologyError("there is no changeset to revert")
            cs = row["id"]
        return self._undo(cs, ("open", "pending", "accepted"), "reverted")

    def _require_changeset(self, cs: str, statuses: tuple[str, ...]) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM changesets WHERE id = %s FOR UPDATE", (cs,)).fetchone()
        if not row:
            raise OntologyError(f"no changeset '{cs}'")
        if row["status"] not in statuses:
            raise OntologyError(f"changeset '{cs}' is {row['status']}; expected one of {list(statuses)}")
        return row

    def _undo(self, cs: str, statuses: tuple[str, ...], final: str) -> dict[str, Any]:
        try:
            with self.conn.transaction():
                self._require_changeset(cs, statuses)
                ops = self.conn.execute(
                    "SELECT * FROM operations WHERE changeset_id = %s ORDER BY id DESC", (cs,)
                ).fetchall()
                if ops:
                    later = self.conn.execute(
                        """SELECT DISTINCT o.changeset_id FROM operations o JOIN changesets c ON c.id = o.changeset_id
                           WHERE o.target_id = ANY(%s) AND o.changeset_id <> %s AND o.id > %s
                             AND c.status NOT IN ('rejected', 'reverted')""",
                        ([op["target_id"] for op in ops], cs, min(op["id"] for op in ops)),
                    ).fetchall()
                    if later:
                        raise OntologyError(
                            f"cannot undo {cs}: later changesets {[r['changeset_id'] for r in later]} "
                            "changed the same nodes or links; undo those first"
                        )
                created_nodes = [op["target_id"] for op in ops if op["target_table"] == "objects" and op["before"] is None]
                if created_nodes:
                    # Template bindings were applied outside any changeset; they go with the node.
                    self.conn.execute(
                        "DELETE FROM bindings WHERE changeset_id IS NULL AND node_id = ANY(%s)", (created_nodes,)
                    )
                for op in ops:
                    table, cols = op["target_table"], TABLE_COLS[op["target_table"]]
                    if op["before"] is None:
                        self.conn.execute(f"DELETE FROM {table} WHERE id = %s", (op["target_id"],))
                    else:
                        col_list = ", ".join(cols)
                        self.conn.execute(
                            f"""UPDATE {table} SET ({col_list}) =
                                (SELECT {col_list} FROM jsonb_populate_record(NULL::{table}, %s)) WHERE id = %s""",
                            (Jsonb(op["before"]), op["target_id"]),
                        )
                self.conn.execute("UPDATE changesets SET status = %s, decided_at = now() WHERE id = %s", (final, cs))
        except psycopg.errors.ForeignKeyViolation:
            raise OntologyError(f"cannot undo {cs}: later changes link to nodes it created; undo those first") from None
        return self.get_changeset(cs)

    def list_changesets(self, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT c.id, c.actor, c.summary, c.status, c.created_at, c.submitted_at, c.decided_at,
                      count(o.id) AS operations
               FROM changesets c LEFT JOIN operations o ON o.changeset_id = c.id
               WHERE (%(status)s::text IS NULL OR c.status = %(status)s)
               GROUP BY c.id ORDER BY c.created_at DESC LIMIT %(limit)s""",
            {"status": status, "limit": limit},
        ).fetchall()
        return [{k: _iso(v) for k, v in r.items()} for r in rows]

    def get_changeset(self, cs: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM changesets WHERE id = %s", (cs,)).fetchone()
        if not row:
            raise OntologyError(f"no changeset '{cs}'")
        ops = self.conn.execute(
            "SELECT op, target_table, target_id, before, after, rationale FROM operations WHERE changeset_id = %s ORDER BY id",
            (cs,),
        ).fetchall()
        return {**{k: _iso(v) for k, v in row.items()}, "operations": [_op_summary(o) for o in ops]}

    # ------------------------------------------------------------------ reads

    def _pending(self, changeset_id: str | None) -> bool:
        if not changeset_id:
            return False
        row = self.conn.execute("SELECT status FROM changesets WHERE id = %s", (changeset_id,)).fetchone()
        return bool(row and row["status"] in OPEN_STATUSES)

    def _node_out(self, r: dict[str, Any]) -> dict[str, Any]:
        ot = self.registry.object_types.get(r["type"])
        out = {
            "id": r["id"], "type": r["type"], "layer": ot.layer if ot else None, "key": r["key"],
            "name": r["name"], "aliases": r["aliases"], "description": r["description"],
            "props": r["props"], "provenance": r["provenance"], "status": r["status"],
            "pending": self._pending(r["changeset_id"]),
        }
        if r["merged_into"]:
            out["merged_into"] = r["merged_into"]
        return out

    def _link_out(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": r["id"], "type": r["type"], "from_id": r["from_id"], "to_id": r["to_id"],
            "props": r["props"], "provenance": r["provenance"], "confidence": r["confidence"],
            "valid_from": _iso(r["valid_from"]), "valid_to": _iso(r["valid_to"]),
            "rationale": r["rationale"], "status": r["status"], "pending": self._pending(r["changeset_id"]),
        }

    def _object_row(self, node_id: str, *, for_update: bool = False) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM objects WHERE id = %s" + (" FOR UPDATE" if for_update else ""), (node_id,)
        ).fetchone()
        if not row:
            raise OntologyError(f"no node '{node_id}'; use search_nodes to find ids")
        return row

    def _active_object(self, node_id: str) -> dict[str, Any]:
        row = self._object_row(node_id, for_update=True)
        if row["status"] != "active":
            hint = f"; it was merged into {row['merged_into']}" if row["merged_into"] else ""
            raise OntologyError(f"node '{node_id}' ({row['name']}) is archived{hint}")
        return row

    def search_nodes(self, query: str, types: list[str] | None = None, limit: int = 20) -> list[dict[str, Any]]:
        q = normalize(query)
        if not q:
            raise OntologyError("query is empty")
        rows = self.conn.execute(
            """SELECT *, CASE WHEN %(q)s = ANY(terms) THEN 0
                              WHEN EXISTS (SELECT 1 FROM unnest(terms) t WHERE t LIKE %(prefix)s) THEN 1
                              ELSE 2 END AS rank
               FROM objects
               WHERE status = 'active'
                 AND (%(types)s::text[] IS NULL OR type = ANY(%(types)s))
                 AND EXISTS (SELECT 1 FROM unnest(terms) t WHERE t LIKE %(pat)s)
               ORDER BY rank, name LIMIT %(limit)s""",
            {"q": q, "pat": _like(q), "prefix": _like(q)[1:], "types": types, "limit": min(limit, 100)},
        ).fetchall()
        return [{**self._node_out(r), "match": ["exact", "prefix", "contains"][r["rank"]]} for r in rows]

    def get_node(self, node_id: str) -> dict[str, Any]:
        node = self._node_out(self._object_row(node_id))
        rows = self.conn.execute(
            """SELECT l.*, o.name AS other_name, o.type AS other_type, (l.from_id = %(id)s) AS outgoing
               FROM links l JOIN objects o ON o.id = CASE WHEN l.from_id = %(id)s THEN l.to_id ELSE l.from_id END
               WHERE l.status = 'active' AND (l.from_id = %(id)s OR l.to_id = %(id)s)
               ORDER BY l.type LIMIT 100""",
            {"id": node_id},
        ).fetchall()
        node["links"] = [
            {**self._link_out(r), "direction": "out" if r["outgoing"] else "in",
             "other": {"id": r["to_id"] if r["outgoing"] else r["from_id"], "name": r["other_name"], "type": r["other_type"]}}
            for r in rows
        ]
        return node

    def get_neighbors(
        self,
        node_id: str,
        depth: int = 1,
        link_types: list[str] | None = None,
        direction: Literal["out", "in", "both"] = "both",
        limit: int = 200,
    ) -> dict[str, Any]:
        """The subgraph within `depth` hops of a node, as {nodes, links}."""
        if not 1 <= depth <= MAX_DEPTH:
            raise OntologyError(f"depth must be between 1 and {MAX_DEPTH}")
        start = self._object_row(node_id)
        seen_nodes = {start["id"]: start}
        seen_links: dict[str, dict[str, Any]] = {}
        frontier = [start["id"]]
        truncated = False
        for _ in range(depth):
            if not frontier:
                break
            rows = self.conn.execute(
                """SELECT * FROM links WHERE status = 'active'
                   AND (%(types)s::text[] IS NULL OR type = ANY(%(types)s))
                   AND ((%(out)s AND from_id = ANY(%(f)s)) OR (%(in)s AND to_id = ANY(%(f)s)))""",
                {"types": link_types, "f": frontier, "out": direction in ("out", "both"), "in": direction in ("in", "both")},
            ).fetchall()
            next_ids = []
            for r in rows:
                seen_links[r["id"]] = r
                for nid in (r["from_id"], r["to_id"]):
                    if nid not in seen_nodes and nid not in next_ids:
                        next_ids.append(nid)
            if len(seen_nodes) + len(next_ids) > limit:
                next_ids = next_ids[: max(0, limit - len(seen_nodes))]
                truncated = True
            for r in self.conn.execute("SELECT * FROM objects WHERE id = ANY(%s)", (next_ids,)).fetchall():
                seen_nodes[r["id"]] = r
            frontier = next_ids
            if truncated:
                break
        links = [l for l in seen_links.values() if l["from_id"] in seen_nodes and l["to_id"] in seen_nodes]
        return {
            "nodes": [self._node_out(n) for n in seen_nodes.values()],
            "links": [self._link_out(l) for l in links],
            "truncated": truncated,
        }

    def find_paths(
        self, from_id: str, to_id: str, max_depth: int = 4, link_types: list[str] | None = None, limit: int = 5
    ) -> list[list[dict[str, Any]]]:
        """Shortest relation chains between two nodes, ignoring link direction. Each step names the link."""
        if not 1 <= max_depth <= MAX_PATH_DEPTH:
            raise OntologyError(f"max_depth must be between 1 and {MAX_PATH_DEPTH}")
        names = {}
        for nid in (from_id, to_id):
            names[nid] = self._object_row(nid)["name"]
        adj: dict[str, list[dict[str, Any]]] = {}
        for r in self.conn.execute(
            """SELECT l.id, l.type, l.from_id, l.to_id FROM links l
               WHERE l.status = 'active' AND (%s::text[] IS NULL OR l.type = ANY(%s))""",
            (link_types, link_types),
        ):
            adj.setdefault(r["from_id"], []).append(r)
            adj.setdefault(r["to_id"], []).append(r)
        paths: list[list[dict[str, Any]]] = []
        queue: deque[tuple[str, list[dict[str, Any]], frozenset[str]]] = deque([(from_id, [], frozenset([from_id]))])
        expansions = 0
        while queue and len(paths) < limit and expansions < 50_000:
            node, path, visited = queue.popleft()
            expansions += 1
            if len(path) >= max_depth:
                continue
            for l in adj.get(node, []):
                other = l["to_id"] if l["from_id"] == node else l["from_id"]
                if other in visited:
                    continue
                step = {"link_id": l["id"], "type": l["type"], "from_id": l["from_id"], "to_id": l["to_id"]}
                if other == to_id:
                    paths.append(path + [step])
                    if len(paths) >= limit:
                        break
                else:
                    queue.append((other, path + [step], visited | {other}))
        ids = {s[k] for p in paths for s in p for k in ("from_id", "to_id")}
        if ids:
            for r in self.conn.execute("SELECT id, name FROM objects WHERE id = ANY(%s)", (list(ids),)):
                names[r["id"]] = r["name"]
        return [[{**s, "from": names[s["from_id"]], "to": names[s["to_id"]]} for s in p] for p in paths]

    def validate_graph(self) -> dict[str, Any]:
        """Problems a human should look at: orphans, likely duplicates, rows that break the current schema."""
        orphans = self.conn.execute(
            """SELECT id, type, name FROM objects o WHERE status = 'active' AND NOT EXISTS
               (SELECT 1 FROM links l WHERE l.status = 'active' AND (l.from_id = o.id OR l.to_id = o.id))
               ORDER BY type, name"""
        ).fetchall()
        duplicates = self.conn.execute(
            """SELECT a.type, a.id AS a_id, a.name AS a_name, b.id AS b_id, b.name AS b_name
               FROM objects a JOIN objects b ON a.type = b.type AND a.id < b.id AND a.terms && b.terms
               WHERE a.status = 'active' AND b.status = 'active'"""
        ).fetchall()
        violations = []
        for r in self.conn.execute("SELECT id, type, name, props FROM objects WHERE status = 'active'"):
            try:
                self.registry.object_type(r["type"]).validate_props(r["props"])
            except OntologyError as e:
                violations.append({"id": r["id"], "name": r["name"], "problem": str(e)})
        for r in self.conn.execute(
            """SELECT l.id, l.type, l.props, f.type AS from_type, t.type AS to_type, f.status AS fs, t.status AS ts
               FROM links l JOIN objects f ON f.id = l.from_id JOIN objects t ON t.id = l.to_id WHERE l.status = 'active'"""
        ):
            try:
                lt = self.registry.link_type(r["type"])
                if not lt.allows(r["from_type"], r["to_type"]):
                    raise OntologyError(f"{r['type']} does not allow {r['from_type']} -> {r['to_type']}")
                if "archived" in (r["fs"], r["ts"]):
                    raise OntologyError("link points at an archived node")
                lt.validate_props(r["props"])
            except OntologyError as e:
                violations.append({"id": r["id"], "problem": str(e)})
        return {"orphans": orphans, "possible_duplicates": duplicates, "violations": violations,
                "ok": not (orphans or duplicates or violations)}

    # ----------------------------------------------------------------- writes

    def upsert_node(
        self,
        type: str,
        name: str,
        provenance: dict[str, Any] | Provenance,
        rationale: str,
        key: str | None = None,
        aliases: list[str] | None = None,
        description: str | None = None,
        props: dict[str, Any] | None = None,
        force_new: bool = False,
    ) -> dict[str, Any]:
        """Create a node, or update the one with the same (type, key). Refuses likely duplicates."""
        ot = self.registry.object_type(type)
        if not name.strip():
            raise OntologyError("name is empty")
        prov = _provenance(provenance)
        norm_key = normalize(key or name)
        aliases = [a for a in (aliases or []) if a.strip()]
        with self.conn.transaction():
            row = self.conn.execute(
                "SELECT * FROM objects WHERE type = %s AND key = %s FOR UPDATE", (type, norm_key)
            ).fetchone()
            hops = 0
            while row and row["merged_into"] and hops < 10:
                row, hops = self._object_row(row["merged_into"], for_update=True), hops + 1
            warnings = []
            if row is None:
                terms = _terms(norm_key, name, aliases)
                clashes = self.conn.execute(
                    "SELECT id, type, key, name FROM objects WHERE status = 'active' AND terms && %s", (terms,)
                ).fetchall()
                same = [c for c in clashes if c["type"] == type]
                if same and not force_new:
                    raise OntologyError(
                        f"possible duplicate of existing {type} {[(c['id'], c['name'], c['key']) for c in same]}. "
                        "To update it, call upsert_node with its key; if it really is a different thing, pass force_new=true."
                    )
                warnings += [f"same name as {c['type']} {c['name']} ({c['id']})" for c in clashes if c["type"] != type]
                cs = self._changeset()
                node_id = _new_id("n")
                self.conn.execute(
                    """INSERT INTO objects (id, type, key, name, aliases, terms, description, props, provenance, status, changeset_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s)""",
                    (node_id, type, norm_key, name.strip(), aliases, terms, description,
                     Jsonb(ot.validate_props(props or {})), Jsonb(prov), cs),
                )
                self._log(cs, "upsert_node", "objects", node_id, None, rationale)
                return {"node": self.get_node(node_id), "created": True, "changed": True, "warnings": warnings}

            new_aliases = list(dict.fromkeys(row["aliases"] + [a for a in [name.strip(), *aliases] if a != row["name"]]))
            merged_props = {**row["props"], **ot.validate_props(props or {}, partial=True)}
            ot.validate_props(merged_props)
            changes = {
                "aliases": new_aliases,
                "props": merged_props,
                "description": description if description is not None else row["description"],
                "provenance": prov,
                "status": "active",
            }
            if row["status"] == "archived":
                warnings.append(f"{row['name']} was archived and has been restored")
            elif all(changes[k] == row[k] for k in changes):
                return {"node": self.get_node(row["id"]), "created": False, "changed": False, "warnings": warnings}
            self._update_object(row, changes, "upsert_node", rationale)
            return {"node": self.get_node(row["id"]), "created": False, "changed": True, "warnings": warnings}

    def _update_object(self, row: dict[str, Any], changes: dict[str, Any], op: str, rationale: str) -> None:
        cs = self._changeset()
        before = self._image("objects", row["id"])
        merged = {**row, **changes}
        changes["terms"] = _terms(merged["key"], merged["name"], merged["aliases"])
        sets = ", ".join(f"{k} = %s" for k in changes)
        values = [Jsonb(v) if k in ("props", "provenance") else v for k, v in changes.items()]
        self.conn.execute(
            f"UPDATE objects SET {sets}, changeset_id = %s, updated_at = now() WHERE id = %s", (*values, cs, row["id"])
        )
        self._log(cs, op, "objects", row["id"], before, rationale)

    def _update_link(self, row: dict[str, Any], changes: dict[str, Any], op: str, rationale: str) -> None:
        cs = self._changeset()
        before = self._image("links", row["id"])
        sets = ", ".join(f"{k} = %s" for k in changes)
        values = [Jsonb(v) if k in ("props", "provenance") else v for k, v in changes.items()]
        self.conn.execute(
            f"UPDATE links SET {sets}, changeset_id = %s, updated_at = now() WHERE id = %s", (*values, cs, row["id"])
        )
        self._log(cs, op, "links", row["id"], before, rationale)

    def _update_binding(self, row: dict[str, Any], changes: dict[str, Any], op: str, rationale: str) -> None:
        if row["changeset_id"] is None and self.actor in ("human", "system"):
            # Direct bindings stay direct when a human or the template engine changes them.
            sets = ", ".join(f"{k} = %s" for k in changes)
            values = [Jsonb(v) if k == "provenance" else v for k, v in changes.items()]
            self.conn.execute(f"UPDATE bindings SET {sets}, updated_at = now() WHERE id = %s", (*values, row["id"]))
            return
        cs = self._changeset()
        before = self._image("bindings", row["id"])
        sets = ", ".join(f"{k} = %s" for k in changes)
        values = [Jsonb(v) if k == "provenance" else v for k, v in changes.items()]
        self.conn.execute(
            f"UPDATE bindings SET {sets}, changeset_id = %s, updated_at = now() WHERE id = %s", (*values, cs, row["id"])
        )
        self._log(cs, op, "bindings", row["id"], before, rationale)

    def update_props(
        self,
        node_id: str,
        rationale: str,
        props: dict[str, Any] | None = None,
        description: str | None = None,
        add_aliases: list[str] | None = None,
    ) -> dict[str, Any]:
        """Patch a node. A prop set to null is removed."""
        with self.conn.transaction():
            row = self._active_object(node_id)
            ot = self.registry.object_type(row["type"])
            patch = props or {}
            merged = {k: v for k, v in {**row["props"], **ot.validate_props(patch, partial=True)}.items()}
            for k, v in patch.items():
                if v is None:
                    merged.pop(k, None)
            ot.validate_props(merged)
            changes: dict[str, Any] = {"props": merged}
            if description is not None:
                changes["description"] = description
            if add_aliases:
                changes["aliases"] = list(dict.fromkeys(row["aliases"] + [a for a in add_aliases if a.strip()]))
            if all(changes[k] == row[k] for k in changes):
                return {"node": self.get_node(node_id), "changed": False}
            self._update_object(row, changes, "update_props", rationale)
        return {"node": self.get_node(node_id), "changed": True}

    def create_link(
        self,
        type: str,
        from_id: str,
        to_id: str,
        provenance: dict[str, Any] | Provenance,
        rationale: str,
        confidence: float | None = None,
        props: dict[str, Any] | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
    ) -> dict[str, Any]:
        """Create a typed link, or update the existing one with the same (type, from, to)."""
        lt = self.registry.link_type(type)
        if not rationale.strip():
            raise OntologyError("rationale is required: say why this relationship holds")
        if confidence is not None and not 0 <= confidence <= 1:
            raise OntologyError("confidence must be between 0 and 1")
        if from_id == to_id:
            raise OntologyError("a node cannot link to itself")
        prov = _provenance(provenance)
        clean = lt.validate_props(props or {})
        vf, vt = (_parse_dt(valid_from, "valid_from"), _parse_dt(valid_to, "valid_to"))
        with self.conn.transaction():
            src, dst = self._active_object(from_id), self._active_object(to_id)
            if not lt.allows(src["type"], dst["type"]):
                raise OntologyError(
                    f"'{type}' links {lt.from_types} -> {lt.to_types}; "
                    f"{src['name']} is {src['type']} and {dst['name']} is {dst['type']}"
                )
            row = self.conn.execute(
                "SELECT * FROM links WHERE type = %s AND from_id = %s AND to_id = %s AND status = 'active' FOR UPDATE",
                (type, from_id, to_id),
            ).fetchone()
            if row:
                changes = {"props": {**row["props"], **clean}, "provenance": prov, "confidence": confidence,
                           "rationale": rationale, "valid_from": vf, "valid_to": vt}
                if all(changes[k] == row[k] for k in changes):
                    return {"link": self._link_out(row), "created": False, "changed": False}
                self._update_link(row, changes, "create_link", rationale)
                return {"link": self._link_out(self._link_row(row["id"])), "created": False, "changed": True}
            cs = self._changeset()
            link_id = _new_id("l")
            self.conn.execute(
                """INSERT INTO links (id, type, from_id, to_id, props, provenance, confidence, valid_from, valid_to,
                                      rationale, status, changeset_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s)""",
                (link_id, type, from_id, to_id, Jsonb(clean), Jsonb(prov), confidence, vf, vt, rationale, cs),
            )
            self._log(cs, "create_link", "links", link_id, None, rationale)
            return {"link": self._link_out(self._link_row(link_id)), "created": True, "changed": True}

    def _link_row(self, link_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM links WHERE id = %s", (link_id,)).fetchone()
        if not row:
            raise OntologyError(f"no link '{link_id}'")
        return row

    def archive(self, target_id: str, rationale: str) -> dict[str, Any]:
        """Soft-delete a node (and its links) or a single link."""
        with self.conn.transaction():
            if target_id.startswith("l_"):
                row = self._link_row(target_id)
                if row["status"] != "active":
                    raise OntologyError(f"link '{target_id}' is already archived")
                self._update_link(row, {"status": "archived"}, "archive", rationale)
                return {"archived": [target_id]}
            if target_id.startswith("b_"):
                row = self._binding_row(target_id)
                if row["status"] != "active":
                    raise OntologyError(f"binding '{target_id}' is already archived")
                self._update_binding(row, {"status": "archived"}, "archive", rationale)
                return {"archived": [target_id]}
            row = self._active_object(target_id)
            archived = []
            for b in self.conn.execute(
                "SELECT * FROM bindings WHERE status = 'active' AND node_id = %s", (target_id,)
            ).fetchall():
                self._update_binding(b, {"status": "archived"}, "archive", rationale)
                archived.append(b["id"])
            for l in self.conn.execute(
                "SELECT * FROM links WHERE status = 'active' AND (from_id = %s OR to_id = %s)", (target_id, target_id)
            ).fetchall():
                self._update_link(l, {"status": "archived"}, "archive", rationale)
                archived.append(l["id"])
            self._update_object(row, {"status": "archived"}, "archive", rationale)
            return {"archived": [target_id, *archived]}

    def merge_nodes(self, keep_id: str, drop_id: str, rationale: str) -> dict[str, Any]:
        """Fold a duplicate into the node that stays: links move over, names become aliases."""
        if keep_id == drop_id:
            raise OntologyError("keep_id and drop_id are the same node")
        with self.conn.transaction():
            keep, drop = self._active_object(keep_id), self._active_object(drop_id)
            if keep["type"] != drop["type"]:
                raise OntologyError(f"cannot merge a {drop['type']} into a {keep['type']}")
            moved, dropped = [], []
            for l in self.conn.execute(
                "SELECT * FROM links WHERE status = 'active' AND (from_id = %s OR to_id = %s)", (drop_id, drop_id)
            ).fetchall():
                new_from = keep_id if l["from_id"] == drop_id else l["from_id"]
                new_to = keep_id if l["to_id"] == drop_id else l["to_id"]
                clash = self.conn.execute(
                    "SELECT 1 FROM links WHERE type = %s AND from_id = %s AND to_id = %s AND status = 'active'",
                    (l["type"], new_from, new_to),
                ).fetchone()
                if new_from == new_to or clash:
                    self._update_link(l, {"status": "archived"}, "merge_nodes", rationale)
                    dropped.append(l["id"])
                else:
                    self._update_link(l, {"from_id": new_from, "to_id": new_to}, "merge_nodes", rationale)
                    moved.append(l["id"])
            keep_roles = {r["role"] for r in self.conn.execute(
                "SELECT role FROM bindings WHERE status = 'active' AND node_id = %s", (keep_id,)
            )}
            for b in self.conn.execute(
                "SELECT * FROM bindings WHERE status = 'active' AND node_id = %s", (drop_id,)
            ).fetchall():
                if b["role"] in keep_roles:
                    self._update_binding(b, {"status": "archived"}, "merge_nodes", rationale)
                else:
                    self._update_binding(b, {"node_id": keep_id}, "merge_nodes", rationale)
            aliases = [a for a in [drop["name"], *drop["aliases"], *keep["aliases"]] if a != keep["name"]]
            self._update_object(
                keep,
                {"aliases": list(dict.fromkeys(keep["aliases"] + aliases)), "props": {**drop["props"], **keep["props"]},
                 "description": keep["description"] or drop["description"]},
                "merge_nodes", rationale,
            )
            self._update_object(drop, {"status": "archived", "merged_into": keep_id}, "merge_nodes", rationale)
        return {"node": self.get_node(keep_id), "moved_links": moved, "dropped_duplicate_links": dropped}

    # --------------------------------------------------------------- bindings

    def _binding_row(self, binding_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM bindings WHERE id = %s FOR UPDATE", (binding_id,)).fetchone()
        if not row:
            raise OntologyError(f"no binding '{binding_id}'")
        return row

    def _bind(self, node_id: str, source: str, params: dict[str, Any], role: str, rationale: str,
              provenance: dict[str, Any] | None, *, direct: bool, unit: str | None = None,
              label: str | None = None) -> dict[str, Any]:
        if not ROLE.match(role):
            raise OntologyError("role must be a short lowercase word such as price, value, flow, supply")
        if not rationale.strip():
            raise OntologyError("rationale is required: say why this series describes the node")
        with self.conn.transaction():
            node = self._active_object(node_id)
            s = self.series.ensure_series(source, params, unit=unit, label=label)
            prov = _provenance(provenance or {"kind": "data_source", "ref": s["key"]})
            row = self.conn.execute(
                "SELECT * FROM bindings WHERE node_id = %s AND role = %s AND status = 'active' FOR UPDATE",
                (node_id, role),
            ).fetchone()
            if row:
                if row["series_id"] == s["id"]:
                    return {"binding": self._binding_out(row["id"]), "created": False, "changed": False}
                self._update_binding(row, {"series_id": s["id"], "provenance": prov, "rationale": rationale},
                                     "propose_binding", rationale)
                return {"binding": self._binding_out(row["id"]), "created": False, "changed": True}
            binding_id = _new_id("b")
            cs = None if direct else self._changeset()
            self.conn.execute(
                """INSERT INTO bindings (id, node_id, series_id, role, provenance, rationale, status, changeset_id)
                   VALUES (%s, %s, %s, %s, %s, %s, 'active', %s)""",
                (binding_id, node["id"], s["id"], role, Jsonb(prov), rationale, cs),
            )
            if cs:
                self._log(cs, "propose_binding", "bindings", binding_id, None, rationale)
            return {"binding": self._binding_out(binding_id), "created": True, "changed": True}

    def propose_binding(self, node_id: str, source: str, params: dict[str, Any], role: str, rationale: str,
                        provenance: dict[str, Any] | None = None) -> dict[str, Any]:
        """Bind a node to a data series. Goes into the actor's changeset; collection starts once it is accepted."""
        out = self._bind(node_id, source, params, role, rationale, provenance, direct=False)
        out["note"] = "Collection starts when the user accepts the changeset."
        return out

    def bind_direct(self, node_id: str, source: str, params: dict[str, Any], role: str, rationale: str,
                    provenance: dict[str, Any] | None = None, unit: str | None = None,
                    label: str | None = None) -> dict[str, Any]:
        """A binding that is in effect at once: made by a human, or by a type template."""
        return self._bind(node_id, source, params, role, rationale, provenance, direct=True, unit=unit, label=label)

    def _binding_out(self, binding_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            """SELECT b.*, s.key, s.label, s.unit, s.frequency, s.source FROM bindings b JOIN series s ON s.id = b.series_id
               WHERE b.id = %s""", (binding_id,),
        ).fetchone()
        return {"id": row["id"], "node_id": row["node_id"], "role": row["role"], "series_id": row["series_id"],
                "series_key": row["key"], "label": row["label"], "unit": row["unit"], "frequency": row["frequency"],
                "provenance": row["provenance"], "rationale": row["rationale"], "status": row["status"],
                "pending": self._pending(row["changeset_id"])}

    def list_bindings(self, node_id: str | None = None) -> list[dict[str, Any]]:
        """Active bindings (of one node, or all) with the latest value of each series."""
        data = self.series.node_data([node_id] if node_id else None)
        return [b | {"node_id": nid} for nid, bs in data.items() for b in bs]

    def node_series(self, node_id: str, role: str | None = None) -> dict[str, Any]:
        rows = self.conn.execute(
            """SELECT b.role, s.* FROM bindings b JOIN series s ON s.id = b.series_id
               WHERE b.node_id = %s AND b.status = 'active' AND (%s::text IS NULL OR b.role = %s)
               ORDER BY b.created_at""",
            (node_id, role, role),
        ).fetchall()
        if not rows:
            name = self._object_row(node_id)["name"]
            raise OntologyError(f"{name} has no data binding{' with role ' + role if role else ''}; "
                                "see list_bindings, or propose_binding to add one")
        return rows[0]

    def apply_templates(self) -> list[dict[str, Any]]:
        """Bind settled nodes that match a type template. Never re-adds a template binding a human removed."""
        made = []
        nodes = self.conn.execute(
            """SELECT o.* FROM objects o LEFT JOIN changesets c ON c.id = o.changeset_id
               WHERE o.status = 'active' AND (o.changeset_id IS NULL OR c.status = 'accepted')"""
        ).fetchall()
        for t in load_templates():
            if not PROVIDERS[source(t["source"]).provider].configured():
                continue  # e.g. no THS_API_KEY yet; the template applies on the first pass after it is set
            want_terms = {normalize(x) for x in t.get("terms", [])}
            for n in nodes:
                if n["type"] != t["type"]:
                    continue
                if want_terms and not want_terms & set(n["terms"]):
                    continue
                if t.get("props") and any(n["props"].get(k) != v for k, v in t["props"].items()):
                    continue
                if t.get("match") and not all(re.search(rx, str(n["props"].get(k) or "")) for k, rx in t["match"].items()):
                    continue
                if not want_terms and not t.get("props") and not t.get("match"):
                    continue
                ref = f"template:{t['id']}"
                taken = self.conn.execute(
                    """SELECT 1 FROM bindings WHERE node_id = %s AND role = %s
                       AND (status = 'active' OR provenance->>'ref' = %s)""",
                    (n["id"], t["role"], ref),
                ).fetchone()
                if taken:
                    continue
                try:
                    params = {k: str(v).format_map(n["props"]) for k, v in (t.get("params") or {}).items()}
                except (KeyError, ValueError):
                    continue
                r = self.bind_direct(n["id"], t["source"], params, t["role"], f"类型模板 {t['id']} 自动绑定",
                                     {"kind": "data_source", "ref": ref}, unit=t.get("unit"), label=t.get("label"))
                made.append(r["binding"])
        return made

    # ------------------------------------------------------------------ graph

    def graph(self, include_archived: bool = False) -> dict[str, Any]:
        """The whole graph, for the canvas, with each node's bound data summarised."""
        status = "('active', 'archived')" if include_archived else "('active')"
        nodes = self.conn.execute(f"SELECT * FROM objects WHERE status IN {status} ORDER BY created_at").fetchall()
        links = self.conn.execute(f"SELECT * FROM links WHERE status IN {status} ORDER BY created_at").fetchall()
        return {"nodes": [self._node_out(n) for n in nodes], "links": [self._link_out(l) for l in links],
                "data": self.series.node_data(), "evidence": summaries(self.conn)}


def _spec(t: ObjectType | LinkType) -> dict[str, Any]:
    return t.model_dump(by_alias=True, exclude={"name", "kind"}, exclude_none=True)


def _parse_dt(value: str | None, field: str) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise OntologyError(f"{field} must be an ISO 8601 datetime") from None


def _op_summary(o: dict[str, Any]) -> dict[str, Any]:
    img = o["after"] or o["before"] or {}
    if o["target_table"] == "bindings":
        label = f"binding {img.get('role')}: {img.get('node_id')} -> {img.get('series_id')}"
    else:
        label = img.get("name") or f"{img.get('type')} {img.get('from_id')} -> {img.get('to_id')}"
    return {"op": o["op"], "table": o["target_table"], "id": o["target_id"], "label": label,
            "created": o["before"] is None, "rationale": o["rationale"]}
