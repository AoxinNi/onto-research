"""Big Bang: merge several projects into a new one. The projects merged stay as they are.

The new project gets the union of their type packs and custom types, and their accepted graph, data bindings,
library (sources, files, extracted text, paper digests, excerpts), groundings, notes and manuscripts. Chat history,
news, reports and pending (unreviewed) changes stay where they are.

Rows keep their ids (random, so they do not collide across projects), except where two projects hold the same
thing: a node with the same type and key, a source with the same DOI (or title and year), a series with the same
key. Those become one row, and every reference to the other — links, groundings, [[n_…]] in notes, claim anchors,
[@citekey] — is pointed at it. A source whose citekey is taken by a different source gets a new citekey, and the
citations of it are rewritten.

Storage: a merge copies rows, not files. Each PDF it copies moves to onto_research.file_blobs (by sha256), and both
the merged project and the project it came from read it there, so a PDF is stored once however many projects hold
it. What is copied is the graph, notes and extracted text, roughly a tenth of a library's size.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from . import cite, projects
from .errors import OntologyError
from .library import Library, parse_refs

ID_REF = re.compile(r"\[\[((?:n|ex|it)_[0-9a-f]{12})\]\]")
READING = {"unread": 0, "reading": 1, "read": 2}
ACCEPTED = "(x.changeset_id IS NULL OR EXISTS (SELECT 1 FROM {} c WHERE c.id = x.changeset_id AND c.status = 'accepted'))"


def merge(conn: psycopg.Connection, project_ids: list[str], name: str | None = None,
          description: str | None = None) -> dict[str, Any]:
    """A new project holding the content of `project_ids` (at least two). Returns its summary plus `merge`: what
    was copied and what was joined with something another project already had."""
    ids = list(dict.fromkeys(project_ids or []))
    if len(ids) < 2:
        raise OntologyError("pick at least two projects to merge")
    sources = [projects.get(conn, pid) for pid in ids]  # each must exist and belong to this workspace
    names = [s["name"] for s in sources]
    packs = list(dict.fromkeys(k for s in sources for k in s["packs"]))
    made = projects.create(conn, (name or "").strip() or "Big Bang · " + " + ".join(names),
                           (description or "").strip() or f"由 {'、'.join(names)} 合并而成", pack=packs[0])
    try:
        for extra in packs[1:]:
            projects.add_pack(conn, made["id"], extra)
        with conn.transaction():  # all or nothing, the moves of file contents included
            conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(made["schema_name"])))
            stats = _Merge(conn, made["schema_name"], names).run([s["schema_name"] for s in sources])
    except BaseException as e:
        projects.drop(conn, made["id"])
        if isinstance(e, psycopg.Error):
            raise OntologyError(f"merge failed: {e}") from None
        raise
    return {**projects.summary(conn, projects.get(conn, made["id"])), "merge": stats}


class _Merge:
    def __init__(self, conn: psycopg.Connection, target: str, names: list[str]):
        self.conn = conn
        self.target = target
        self.ids: dict[str, str] = {}       # id in a source project -> id in the new one, for rows that were joined
        self.copied: set[str] = set()       # source ids now present in the new project (copied or joined)
        self.renames: dict[str, str] = {}   # citekeys of the current source that changed
        self.cols: dict[str, dict[str, str]] = {}
        self.stats = {"projects": len(names), "nodes": 0, "nodes_joined": 0, "links": 0, "bindings": 0,
                      "sources": 0, "sources_joined": 0, "files": 0, "excerpts": 0, "groundings": 0, "notes": 0,
                      "manuscripts": 0, "citekeys_renamed": 0}
        self.cs = f"cs_{uuid.uuid4().hex[:12]}"
        conn.execute("""INSERT INTO changesets (id, actor, summary, status, submitted_at, decided_at)
                        VALUES (%s, 'human', %s, 'accepted', now(), now())""",
                     (self.cs, "Big Bang：合并 " + "、".join(names)))

    # ---------------------------------------------------------------- helpers

    def q(self, query: Any, *args: Any) -> list[dict[str, Any]]:
        cur = self.conn.execute(query, args)
        return cur.fetchall() if cur.description else []

    def one(self, query: Any, *args: Any) -> dict[str, Any] | None:
        rows = self.q(query, *args)
        return rows[0] if rows else None

    def src(self, table: str) -> sql.Identifier:
        return sql.Identifier(self.schema, table)

    def columns(self, table: str) -> dict[str, str]:
        if table not in self.cols:
            self.cols[table] = {r["column_name"]: r["data_type"] for r in self.q(
                """SELECT column_name, data_type FROM information_schema.columns
                   WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position""", self.target, table)}
        return self.cols[table]

    def rows(self, table: str, where: str = "TRUE", *args: Any, accepted: bool = False) -> list[dict[str, Any]]:
        cond = sql.SQL(where)
        if accepted:
            cond = sql.SQL("({}) AND ").format(cond) + sql.SQL(ACCEPTED).format(self.src("changesets"))
        return self.q(sql.SQL("SELECT x.* FROM {} x WHERE {} ORDER BY x.created_at").format(self.src(table), cond), *args)

    def put(self, table: str, row: dict[str, Any], **change: Any) -> None:
        cols = self.columns(table)
        row = {**row, **change}
        names = [c for c in cols if c in row]
        values = [Jsonb(row[c]) if cols[c] == "jsonb" and row[c] is not None else row[c] for c in names]
        self.conn.execute(sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, names)),
            sql.SQL(", ").join(sql.Placeholder() * len(names))), values)

    def copy(self, table: str, where: str, args: tuple[Any, ...], **change: Any) -> None:
        """INSERT … SELECT from the source's table inside the database, with some columns set to new values."""
        cols = list(self.columns(table))
        picks = [sql.Placeholder() if c in change else sql.Identifier(c) for c in cols]
        self.conn.execute(sql.SQL("INSERT INTO {} ({}) SELECT {} FROM {} WHERE {}").format(
            sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, cols)), sql.SQL(", ").join(picks),
            self.src(table), sql.SQL(where)), [*(change[c] for c in cols if c in change), *args])

    def to(self, source_id: str | None) -> str | None:
        """Where a source row ended up in the new project; None when it was not copied."""
        if source_id is None or source_id not in self.copied:
            return None
        return self.ids.get(source_id, source_id)

    def rewrite(self, text: str) -> str:
        """Point [[id]] references, claim anchors and citations at the rows they now are."""
        text = ID_REF.sub(lambda m: f"[[{self.to(m.group(1)) or m.group(1)}]]", text or "")
        text = cite.rewrite_nodes(text, self.ids)
        # Two passes, so a key renamed to one that is itself renamed is not renamed twice.
        tmp = {old: f"bigbang{i}x{uuid.uuid4().hex[:8]}" for i, old in enumerate(self.renames)}
        for old, t in tmp.items():
            text = cite.rename_citekey(text, old, t)
        for old, t in tmp.items():
            text = cite.rename_citekey(text, t, self.renames[old])
        return text

    def join(self, source_id: str, target_id: str) -> None:
        self.copied.add(source_id)
        if source_id != target_id:
            self.ids[source_id] = target_id

    # ---------------------------------------------------------------- the merge

    def run(self, schemas: list[str]) -> dict[str, Any]:
        for schema in schemas:
            self.schema, self.renames = schema, {}
            self.types()
            self.nodes()
            self.links()
            self.bindings()
            self.items()
            self.files()
            self.excerpts()
            self.groundings()
            self.notes()
            self.manuscripts()
        return self.stats

    def types(self) -> None:
        self.copy("types", "status = 'accepted' AND source = 'proposal' AND (kind, name) NOT IN (SELECT kind, name FROM types)", ())

    def nodes(self) -> None:
        for n in self.rows("objects", "x.status = 'active' AND x.merged_into IS NULL", accepted=True):
            same = self.one("SELECT id, name, aliases FROM objects WHERE type = %s AND key = %s", n["type"], n["key"])
            if same:
                extra = [a for a in [n["name"], *n["aliases"]] if a != same["name"] and a not in same["aliases"]]
                self.q("""UPDATE objects SET aliases = aliases || %s::text[],
                                 terms = ARRAY(SELECT DISTINCT unnest(terms || %s::text[])),
                                 description = coalesce(description, %s), updated_at = now() WHERE id = %s""",
                       list(dict.fromkeys(extra)), n["terms"], n["description"], same["id"])
                self.join(n["id"], same["id"])
                self.stats["nodes_joined"] += 1
            else:
                self.put("objects", n, changeset_id=self.cs)
                self.join(n["id"], n["id"])
                self.stats["nodes"] += 1

    def links(self) -> None:
        for l in self.rows("links", "x.status = 'active'", accepted=True):
            a, b = self.to(l["from_id"]), self.to(l["to_id"])
            if not a or not b or a == b:
                continue
            same = self.one("SELECT id FROM links WHERE type = %s AND from_id = %s AND to_id = %s AND status = 'active'",
                            l["type"], a, b)
            if same:
                self.join(l["id"], same["id"])
            else:
                self.put("links", l, from_id=a, to_id=b, changeset_id=self.cs)
                self.join(l["id"], l["id"])
                self.stats["links"] += 1

    def bindings(self) -> None:
        for b in self.rows("bindings", "x.status = 'active'", accepted=True):
            node = self.to(b["node_id"])
            if not node or self.one("SELECT 1 FROM bindings WHERE node_id = %s AND role = %s AND status = 'active'",
                                    node, b["role"]):
                continue
            series = self.to(b["series_id"])
            if not series:
                s = self.one(sql.SQL("SELECT * FROM {} WHERE id = %s").format(self.src("series")), b["series_id"])
                same = self.one("SELECT id FROM series WHERE key = %s", s["key"])
                if same:
                    series = same["id"]
                else:
                    self.put("series", s)
                    self.copy("observations", "series_id = %s", (s["id"],))
                    series = s["id"]
                self.join(s["id"], series)
            self.put("bindings", b, node_id=node, series_id=series, changeset_id=b["changeset_id"] and self.cs)
            self.stats["bindings"] += 1

    def items(self) -> None:
        lib = Library(self.conn)
        for it in self.rows("library_items", "x.status = 'active'"):
            same = None
            if it["doi"]:
                same = self.one("SELECT * FROM library_items WHERE lower(doi) = lower(%s) AND status = 'active'", it["doi"])
            if not same:
                same = self.one("""SELECT * FROM library_items WHERE status = 'active' AND year IS NOT DISTINCT FROM %s
                                     AND lower(regexp_replace(title, '\\s+', ' ', 'g')) = lower(regexp_replace(%s, '\\s+', ' ', 'g'))
                                     AND (doi IS NULL OR %s::text IS NULL)""", it["year"], it["title"], it["doi"])
            if same:
                reading = max(same["reading"], it["reading"], key=lambda r: READING.get(r, 0))
                self.q("""UPDATE library_items SET tags = ARRAY(SELECT DISTINCT unnest(tags || %s::text[])), reading = %s,
                                 updated_at = now() WHERE id = %s""", it["tags"], reading, same["id"])
                key = same["citekey"]
                self.join(it["id"], same["id"])
                self.stats["sources_joined"] += 1
            else:
                key = it["citekey"]
                if self.one("SELECT 1 FROM library_items WHERE citekey = %s", key):
                    key = lib._unique_citekey(key)
                self.put("library_items", it, citekey=key)
                self.join(it["id"], it["id"])
                self.stats["sources"] += 1
            if key != it["citekey"]:
                self.renames[it["citekey"]] = key
                self.stats["citekeys_renamed"] += 1

    def files(self) -> None:
        fields = sql.SQL(", ").join(sql.Identifier(c) for c in self.columns("library_files") if c != "bytes")
        files = self.q(sql.SQL("SELECT {}, bytes IS NULL AS shared FROM {} x WHERE x.item_id = ANY(%s) ORDER BY created_at")
                       .format(fields, self.src("library_files")), [i for i in self.copied if i.startswith("it_")])
        for f in files:
            if not f["shared"]:  # move the contents to the shared store; the source project reads them there too
                self.q(sql.SQL("""INSERT INTO onto_research.file_blobs (sha256, bytes, size)
                                  SELECT sha256, bytes, size FROM {} WHERE id = %s ON CONFLICT (sha256) DO NOTHING""")
                       .format(self.src("library_files")), f["id"])
                self.q(sql.SQL("UPDATE {} SET bytes = NULL WHERE sha256 = %s").format(self.src("library_files")), f["sha256"])
            item = self.to(f["item_id"])
            same = self.one("SELECT id FROM library_files WHERE item_id = %s AND sha256 = %s", item, f["sha256"])
            if same:
                self.join(f["id"], same["id"])
                continue
            self.put("library_files", {k: v for k, v in f.items() if k != "shared"}, item_id=item, bytes=None)
            self.copy("library_chunks", "file_id = %s", (f["id"],), item_id=item)
            self.copy("paper_digests", "file_id = %s", (f["id"],), item_id=item)
            self.join(f["id"], f["id"])
            self.stats["files"] += 1

    def excerpts(self) -> None:
        for e in self.rows("excerpts", "x.item_id = ANY(%s)", [i for i in self.copied if i.startswith("it_")]):
            item = self.to(e["item_id"])
            same = self.one("SELECT id FROM excerpts WHERE item_id = %s AND quote = %s", item, e["quote"])
            if same:
                self.join(e["id"], same["id"])
                continue
            anchor = e["anchor"]
            if anchor and anchor.get("file_id"):
                anchor = {**anchor, "file_id": self.to(anchor["file_id"])} if self.to(anchor["file_id"]) else None
            self.put("excerpts", e, item_id=item, file_id=self.to(e["file_id"]), anchor=anchor)
            self.join(e["id"], e["id"])
            self.stats["excerpts"] += 1

    def groundings(self) -> None:
        for g in self.rows("groundings", "x.status = 'active'", accepted=True):
            node, link, item = self.to(g["node_id"]), self.to(g["link_id"]), self.to(g["item_id"])
            if not (node or link) or not item:
                continue
            if self.one("""SELECT 1 FROM groundings WHERE node_id IS NOT DISTINCT FROM %s AND link_id IS NOT DISTINCT FROM %s
                           AND item_id = %s AND stance = %s AND status = 'active'""", node, link, item, g["stance"]):
                continue
            self.put("groundings", g, node_id=node, link_id=link, item_id=item, excerpt_id=self.to(g["excerpt_id"]),
                     changeset_id=g["changeset_id"] and self.cs)
            self.stats["groundings"] += 1

    def notes(self) -> None:
        for n in self.rows("notes"):  # archived ones too, so they can still be restored
            body = self.rewrite(n["body"])
            self.put("notes", n, body=body, refs=parse_refs(body))
            self.stats["notes"] += 1

    def manuscripts(self) -> None:
        for m in self.rows("manuscripts", "x.status = 'active'"):
            self.put("manuscripts", m)
            for s in self.q(sql.SQL("SELECT * FROM {} WHERE manuscript_id = %s ORDER BY position")
                            .format(self.src("manuscript_sections")), m["id"]):
                self.put("manuscript_sections", s, body=self.rewrite(s["body"]), rev=1)
            self.stats["manuscripts"] += 1
