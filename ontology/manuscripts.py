"""Manuscripts: papers written in the project, section by section.

A person edits sections directly. Each save names the revision it started from (`base_rev`); when another page or a
citekey rename got there first the save is refused with the current text (SectionConflict), so nothing is silently
overwritten. One author's saves within five minutes of each other are kept as a single revision.

The agent never writes text: it suggests (replace a section, insert one, or propose an outline) and a person
accepts or rejects each suggestion. A replace suggestion written against an older revision is marked stale; it can
still be accepted after the person has seen the difference against the current text.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import cite
from .errors import OntologyError

KINDS = ("article", "thesis", "proposal", "review", "report", "other")
LANGS = ("zh-CN", "en-US")
MAX_BODY = 200_000          # characters in one section
MAX_SECTIONS = 200
MERGE_WINDOW = timedelta(minutes=5)
META_KEYS = {"authors", "abstract", "keywords", "subtitle", "date", "template"}
OPEN_STATUSES = ("open", "pending")


class SectionConflict(OntologyError):
    """A save started from an older revision. `current` is the section as it is now."""

    def __init__(self, current: dict[str, Any]):
        super().__init__(f"section {current['id']} was changed elsewhere (now rev {current['rev']}); "
                         "read it again and merge")
        self.current = current


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def _text(value: Any, name: str, limit: int, required: bool = False) -> str:
    text = str(value or "").replace("\r\n", "\n")
    if required and not text.strip():
        raise OntologyError(f"{name} is empty")
    if len(text) > limit:
        raise OntologyError(f"{name} is longer than {limit} characters")
    return text


def _level(level: Any) -> int:
    try:
        n = int(level)
    except (TypeError, ValueError):
        raise OntologyError("level must be 1, 2 or 3") from None
    if n not in (1, 2, 3):
        raise OntologyError("level must be 1, 2 or 3")
    return n


def _meta(meta: Any) -> dict[str, Any]:
    if not isinstance(meta, dict):
        raise OntologyError("meta must be an object")
    unknown = set(meta) - META_KEYS
    if unknown:
        raise OntologyError(f"unknown manuscript fields: {sorted(unknown)}; known: {sorted(META_KEYS)}")
    out: dict[str, Any] = {}
    if "authors" in meta:
        authors = meta["authors"] or []
        if not isinstance(authors, list) or not all(isinstance(a, dict) and str(a.get("name") or "").strip() for a in authors):
            raise OntologyError("authors is a list of {name, affiliation}")
        out["authors"] = [{"name": str(a["name"]).strip()[:120],
                           **({"affiliation": str(a["affiliation"]).strip()[:300]} if a.get("affiliation") else {})}
                          for a in authors[:50]]
    if "keywords" in meta:
        kw = meta["keywords"] or []
        if not isinstance(kw, list):
            raise OntologyError("keywords is a list of words")
        out["keywords"] = [str(k).strip()[:60] for k in kw if str(k).strip()][:20]
    for key in ("abstract", "subtitle", "date"):
        if key in meta:
            out[key] = _text(meta[key], key, 20_000 if key == "abstract" else 300)
    if "template" in meta:
        if meta["template"] not in (None, "article", "report"):
            raise OntologyError("template is article or report")
        out["template"] = meta["template"]
    return out


class Manuscripts:
    def __init__(self, conn: psycopg.Connection, actor: str = "human"):
        self.conn = conn
        self.actor = actor

    # -------------------------------------------------------------- manuscripts

    def _row(self, manuscript_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM manuscripts WHERE id = %s", (manuscript_id,)).fetchone()
        if not row:
            raise OntologyError(f"no manuscript '{manuscript_id}'; use list_manuscripts to find ids")
        return row

    def _out(self, r: dict[str, Any]) -> dict[str, Any]:
        return {k: _iso(v) for k, v in r.items()}

    def list(self, include_archived: bool = False) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT m.*, count(s.id) AS sections, coalesce(sum(length(s.body)), 0) AS chars,
                      (SELECT count(*) FROM section_suggestions g WHERE g.manuscript_id = m.id
                         AND g.status IN ('pending', 'stale')) AS suggestions
               FROM manuscripts m LEFT JOIN manuscript_sections s ON s.manuscript_id = m.id
               WHERE %s OR m.status = 'active' GROUP BY m.id ORDER BY m.updated_at DESC""", (include_archived,)
        ).fetchall()
        return [self._out(r) for r in rows]

    def create(self, title: str, kind: str = "article", csl_style: str = "gb7714-numeric", lang: str = "zh-CN",
               meta: dict[str, Any] | None = None, sections: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        title = _text(title, "title", 300, required=True).strip()
        if kind not in KINDS:
            raise OntologyError(f"kind must be one of {list(KINDS)}")
        if csl_style not in cite.STYLES:
            raise OntologyError(f"csl_style must be one of {list(cite.STYLES)}")
        if lang not in LANGS:
            raise OntologyError(f"lang must be one of {list(LANGS)}")
        mid = _new_id("ms")
        with self.conn.transaction():
            self.conn.execute(
                "INSERT INTO manuscripts (id, title, kind, csl_style, lang, meta) VALUES (%s, %s, %s, %s, %s, %s)",
                (mid, title, kind, csl_style, lang, Jsonb(_meta(meta or {}))))
            for s in sections if sections is not None else _default_outline(kind, lang):
                self.add_section(mid, s.get("heading") or "无标题", s.get("level", 1), body=s.get("body") or "")
        return self.get(mid)

    def update(self, manuscript_id: str, title: str | None = None, kind: str | None = None,
               csl_style: str | None = None, lang: str | None = None, meta: dict[str, Any] | None = None,
               archived: bool | None = None) -> dict[str, Any]:
        cur = self._row(manuscript_id)
        sets: dict[str, Any] = {}
        if title is not None:
            sets["title"] = _text(title, "title", 300, required=True).strip()
        if kind is not None:
            if kind not in KINDS:
                raise OntologyError(f"kind must be one of {list(KINDS)}")
            sets["kind"] = kind
        if csl_style is not None:
            if csl_style not in cite.STYLES:
                raise OntologyError(f"csl_style must be one of {list(cite.STYLES)}")
            sets["csl_style"] = csl_style
        if lang is not None:
            if lang not in LANGS:
                raise OntologyError(f"lang must be one of {list(LANGS)}")
            sets["lang"] = lang
        if meta is not None:
            sets["meta"] = Jsonb({**cur["meta"], **_meta(meta)})
        if archived is not None:
            sets["status"] = "archived" if archived else "active"
        if sets:
            cols = ", ".join(f"{k} = %s" for k in sets)
            self.conn.execute(f"UPDATE manuscripts SET {cols}, updated_at = now() WHERE id = %s",
                              (*sets.values(), manuscript_id))
        return self.get(manuscript_id)

    def _touch(self, manuscript_id: str) -> None:
        self.conn.execute("UPDATE manuscripts SET updated_at = now() WHERE id = %s", (manuscript_id,))

    def _section_out(self, r: dict[str, Any], with_body: bool) -> dict[str, Any]:
        claims = cite.claims(r["body"])
        out = {"id": r["id"], "position": r["position"], "level": r["level"], "heading": r["heading"], "rev": r["rev"],
               "words": cite.word_count(r["body"]), "citekeys": cite.citekeys(r["body"]),
               "claims": list(dict.fromkeys(c.node for c in claims)),
               "updated_by": r["updated_by"], "updated_at": _iso(r["updated_at"])}
        if with_body:
            out["body"] = r["body"]
        return out

    def sections(self, manuscript_id: str) -> list[dict[str, Any]]:
        return self.conn.execute(
            "SELECT * FROM manuscript_sections WHERE manuscript_id = %s ORDER BY position", (manuscript_id,)
        ).fetchall()

    def get(self, manuscript_id: str, with_bodies: bool = True) -> dict[str, Any]:
        """The manuscript, its sections in order (with bodies unless with_bodies is false) and open suggestions."""
        out = self._out(self._row(manuscript_id))
        rows = self.sections(manuscript_id)
        out["sections"] = [self._section_out(r, with_bodies) for r in rows]
        out["words"] = sum(s["words"] for s in out["sections"])
        out["citekeys"] = list(dict.fromkeys(k for s in out["sections"] for k in s["citekeys"]))
        out["suggestions"] = self.suggestions(manuscript_id)
        return out

    # -------------------------------------------------------------- sections

    def _section(self, section_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM manuscript_sections WHERE id = %s", (section_id,)).fetchone()
        if not row:
            raise OntologyError(f"no section '{section_id}'; use get_manuscript to find ids")
        return row

    def read_section(self, section_id: str) -> dict[str, Any]:
        r = self._section(section_id)
        return {**self._section_out(r, True), "manuscript_id": r["manuscript_id"]}

    def add_section(self, manuscript_id: str, heading: str, level: int = 1, after_section_id: str | None = None,
                    body: str = "", author: str | None = None, at_start: bool = False) -> dict[str, Any]:
        """A new section after `after_section_id` (at the end when not given, at the start with at_start)."""
        self._row(manuscript_id)
        heading = _text(heading, "heading", 300, required=True).strip()
        body = _text(body, "body", MAX_BODY)
        level = _level(level)
        author = author or self.actor
        rows = self.sections(manuscript_id)
        if len(rows) >= MAX_SECTIONS:
            raise OntologyError(f"a manuscript has at most {MAX_SECTIONS} sections")
        if after_section_id:
            after = self._section(after_section_id)
            if after["manuscript_id"] != manuscript_id:
                raise OntologyError("that section belongs to another manuscript")
            position = after["position"] + 1
        else:
            position = 0 if at_start else (rows[-1]["position"] + 1 if rows else 0)
        sid = _new_id("sec")
        with self.conn.transaction():
            self.conn.execute("""UPDATE manuscript_sections SET position = position + 1
                                 WHERE manuscript_id = %s AND position >= %s""", (manuscript_id, position))
            self.conn.execute(
                """INSERT INTO manuscript_sections (id, manuscript_id, position, level, heading, body, updated_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""", (sid, manuscript_id, position, level, heading, body, author))
            self.conn.execute("INSERT INTO section_revisions (section_id, rev, heading, body, author) VALUES (%s, 1, %s, %s, %s)",
                              (sid, heading, body, author))
            self._touch(manuscript_id)
        return self.read_section(sid)

    def save_section(self, section_id: str, base_rev: int, heading: str | None = None, body: str | None = None,
                     level: int | None = None, author: str | None = None) -> dict[str, Any]:
        """Write a section that was read at `base_rev`. Raises SectionConflict when it has moved on since."""
        cur = self._section(section_id)
        heading = cur["heading"] if heading is None else _text(heading, "heading", 300, required=True).strip()
        body = cur["body"] if body is None else _text(body, "body", MAX_BODY)
        level = cur["level"] if level is None else _level(level)
        author = author or self.actor
        if heading == cur["heading"] and body == cur["body"] and level == cur["level"]:
            if base_rev != cur["rev"]:
                raise SectionConflict(self.read_section(section_id))
            return self.read_section(section_id)
        with self.conn.transaction():
            row = self.conn.execute(
                """UPDATE manuscript_sections SET heading = %s, body = %s, level = %s, rev = rev + 1, updated_by = %s,
                          updated_at = now() WHERE id = %s AND rev = %s RETURNING rev""",
                (heading, body, level, author, section_id, base_rev)).fetchone()
            if not row:
                raise SectionConflict(self.read_section(section_id))
            self._revision(section_id, row["rev"], heading, body, author)
            self._touch(cur["manuscript_id"])
            self.conn.execute(
                """UPDATE section_suggestions SET status = 'stale' WHERE section_id = %s AND kind = 'replace'
                   AND status = 'pending' AND base_rev < %s""", (section_id, row["rev"]))
        return self.read_section(section_id)

    def _revision(self, section_id: str, rev: int, heading: str, body: str, author: str) -> None:
        last = self.conn.execute(
            """SELECT rev, author, created_at, now() - created_at < %s AS recent FROM section_revisions
               WHERE section_id = %s ORDER BY rev DESC LIMIT 1""", (MERGE_WINDOW, section_id)).fetchone()
        if last and last["author"] == author and last["recent"] and last["rev"] > 1:
            self.conn.execute(
                """UPDATE section_revisions SET rev = %s, heading = %s, body = %s, updated_at = now()
                   WHERE section_id = %s AND rev = %s""", (rev, heading, body, section_id, last["rev"]))
        else:
            self.conn.execute(
                "INSERT INTO section_revisions (section_id, rev, heading, body, author) VALUES (%s, %s, %s, %s, %s)",
                (section_id, rev, heading, body, author))

    def revisions(self, section_id: str) -> list[dict[str, Any]]:
        self._section(section_id)
        return [{k: _iso(v) for k, v in r.items()} for r in self.conn.execute(
            """SELECT rev, heading, body, author, created_at, updated_at FROM section_revisions
               WHERE section_id = %s ORDER BY rev DESC LIMIT 100""", (section_id,))]

    def delete_section(self, section_id: str) -> dict[str, Any]:
        cur = self._section(section_id)
        with self.conn.transaction():
            self.conn.execute("DELETE FROM manuscript_sections WHERE id = %s", (section_id,))
            self.conn.execute("""UPDATE manuscript_sections SET position = position - 1
                                 WHERE manuscript_id = %s AND position > %s""", (cur["manuscript_id"], cur["position"]))
            self._touch(cur["manuscript_id"])
        return {"deleted": section_id, "manuscript_id": cur["manuscript_id"], "heading": cur["heading"],
                "body": cur["body"], "level": cur["level"]}

    def reorder(self, manuscript_id: str, order: list[dict[str, Any]]) -> dict[str, Any]:
        """Set the order (and levels) of every section: [{id, level?}] in the new order."""
        rows = {r["id"]: r for r in self.sections(manuscript_id)}
        ids = [str(o.get("id")) for o in order]
        if sorted(ids) != sorted(rows) or len(set(ids)) != len(ids):
            raise OntologyError("give every section of the manuscript exactly once")
        with self.conn.transaction():
            for i, o in enumerate(order):
                level = _level(o["level"]) if o.get("level") is not None else rows[o["id"]]["level"]
                self.conn.execute("UPDATE manuscript_sections SET position = %s, level = %s WHERE id = %s",
                                  (i, level, o["id"]))
            self._touch(manuscript_id)
        return self.get(manuscript_id, with_bodies=False)

    # -------------------------------------------------------------- suggestions

    def _suggestion_out(self, r: dict[str, Any]) -> dict[str, Any]:
        out = {k: _iso(v) for k, v in r.items()}
        if r["kind"] == "outline":
            out["outline"] = json.loads(r["body"] or "[]")
        return out

    def suggestions(self, manuscript_id: str, include_decided: bool = False) -> list[dict[str, Any]]:
        return [self._suggestion_out(r) for r in self.conn.execute(
            """SELECT * FROM section_suggestions WHERE manuscript_id = %s AND (%s OR status IN ('pending', 'stale'))
               ORDER BY created_at""", (manuscript_id, include_decided))]

    def _suggestion(self, suggestion_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM section_suggestions WHERE id = %s", (suggestion_id,)).fetchone()
        if not row:
            raise OntologyError(f"no suggestion '{suggestion_id}'")
        return row

    def _suggest(self, manuscript_id: str, kind: str, rationale: str, section_id: str | None = None,
                 base_rev: int | None = None, heading: str | None = None, body: str | None = None,
                 level: int | None = None, author: str | None = None) -> dict[str, Any]:
        rationale = _text(rationale, "rationale", 2000, required=True).strip()
        gid = _new_id("sg")
        self.conn.execute(
            """INSERT INTO section_suggestions (id, manuscript_id, section_id, kind, base_rev, heading, level, body,
                                                rationale, author) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (gid, manuscript_id, section_id, kind, base_rev, heading, level, body, rationale, author or self.actor))
        return self._suggestion_out(self._suggestion(gid))

    def suggest_edit(self, section_id: str, base_rev: int, body: str, rationale: str,
                     heading: str | None = None) -> dict[str, Any]:
        cur = self._section(section_id)
        if base_rev != cur["rev"]:
            raise OntologyError(f"section {section_id} is at rev {cur['rev']}, not {base_rev}: call read_section "
                                "and write the suggestion against the current text")
        body = _text(body, "body", MAX_BODY)
        heading = _text(heading, "heading", 300).strip() or None if heading is not None else None
        if body == cur["body"] and (heading is None or heading == cur["heading"]):
            raise OntologyError("the suggestion is the same as the current text")
        return self._suggest(cur["manuscript_id"], "replace", rationale, section_id, base_rev, heading, body)

    def suggest_section(self, manuscript_id: str, heading: str, body: str, rationale: str,
                        after_section_id: str | None = None, level: int = 1, author: str | None = None) -> dict[str, Any]:
        self._row(manuscript_id)
        if after_section_id and self._section(after_section_id)["manuscript_id"] != manuscript_id:
            raise OntologyError("that section belongs to another manuscript")
        return self._suggest(manuscript_id, "insert_after", rationale, after_section_id, None,
                             _text(heading, "heading", 300, required=True).strip(), _text(body, "body", MAX_BODY),
                             _level(level), author)

    def suggest_outline(self, manuscript_id: str, sections: list[dict[str, Any]], rationale: str) -> dict[str, Any]:
        self._row(manuscript_id)
        if not isinstance(sections, list) or not sections or len(sections) > 60:
            raise OntologyError("an outline is a list of 1 to 60 sections")
        outline = [{"heading": _text(s.get("heading"), "heading", 300, required=True).strip(),
                    "level": _level(s.get("level", 1)), "intent": _text(s.get("intent"), "intent", 1000).strip()}
                   for s in sections if isinstance(s, dict)]
        return self._suggest(manuscript_id, "outline", rationale, body=json.dumps(outline, ensure_ascii=False))

    def accept(self, suggestion_id: str, force: bool = False) -> dict[str, Any]:
        """Apply a suggestion. A stale one (the text has changed since) needs force, after the person compared."""
        g = self._suggestion(suggestion_id)
        if g["status"] not in ("pending", "stale"):
            raise OntologyError(f"this suggestion was already {g['status']}")
        author = g["author"]
        with self.conn.transaction():
            if g["kind"] == "replace":
                cur = self._section(g["section_id"])
                if cur["rev"] != g["base_rev"] and not force:
                    raise OntologyError("the section changed after this suggestion was written; compare and accept "
                                        "again to apply it anyway")
                self.save_section(g["section_id"], cur["rev"], g["heading"], g["body"], author=author)
                applied = [g["section_id"]]
            elif g["kind"] == "insert_after":
                made = self.add_section(g["manuscript_id"], g["heading"], g["level"] or 1, g["section_id"], g["body"] or "",
                                        author=author, at_start=g["section_id"] is None)
                applied = [made["id"]]
            else:
                # Headings the paper has stay as they are; a missing one goes in after the outline's previous heading.
                have = {r["heading"].strip().lower(): r["id"] for r in self.sections(g["manuscript_id"])}
                applied, after = [], None
                for s in json.loads(g["body"] or "[]"):
                    key = s["heading"].strip().lower()
                    if key in have:
                        after = have[key]
                        continue
                    body = f"<!-- {s['intent'].replace('--', '—')} -->\n" if s.get("intent") else ""
                    if after:  # past the subsections of the previous heading, so they keep their parent
                        rows = self.sections(g["manuscript_id"])
                        i = next(i for i, r in enumerate(rows) if r["id"] == after)
                        while i + 1 < len(rows) and rows[i + 1]["level"] > s["level"]:
                            i += 1
                        after = rows[i]["id"]
                    after = self.add_section(g["manuscript_id"], s["heading"], s["level"], after, body=body,
                                             author=author, at_start=after is None)["id"]
                    have[key] = after
                    applied.append(after)
            self.conn.execute("UPDATE section_suggestions SET status = 'accepted', decided_at = now() WHERE id = %s",
                              (suggestion_id,))
        return {"suggestion": self._suggestion_out(self._suggestion(suggestion_id)), "sections": applied,
                "manuscript_id": g["manuscript_id"]}

    def reject(self, suggestion_id: str) -> dict[str, Any]:
        g = self._suggestion(suggestion_id)
        if g["status"] not in ("pending", "stale"):
            raise OntologyError(f"this suggestion was already {g['status']}")
        self.conn.execute("UPDATE section_suggestions SET status = 'rejected', decided_at = now() WHERE id = %s",
                          (suggestion_id,))
        return {"suggestion": self._suggestion_out(self._suggestion(suggestion_id)), "manuscript_id": g["manuscript_id"]}

    # -------------------------------------------------------------- citations

    def cited_items(self, manuscript_id: str) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
        """(citekey, CSL) of the library items a manuscript cites, in order of first citation, and missing keys."""
        keys = self.get(manuscript_id, with_bodies=False)["citekeys"]
        rows = {r["citekey"]: r for r in self.conn.execute(
            "SELECT citekey, csl, status FROM library_items WHERE citekey = ANY(%s)", (keys,))}
        found = [(k, rows[k]["csl"]) for k in keys if k in rows and rows[k]["status"] == "active"]
        return found, [k for k in keys if k not in rows or rows[k]["status"] != "active"]

    def citing(self, citekey: str) -> list[dict[str, Any]]:
        """Sections of active manuscripts that cite a key."""
        out = []
        for r in self.conn.execute(
            """SELECT s.id, s.heading, s.body, m.id AS manuscript_id, m.title FROM manuscript_sections s
               JOIN manuscripts m ON m.id = s.manuscript_id WHERE m.status = 'active' AND s.body LIKE %s
               ORDER BY m.updated_at DESC, s.position""", (f"%@{citekey.replace('%', '')}%",)):
            if citekey in cite.citekeys(r["body"]):
                out.append({"section_id": r["id"], "heading": r["heading"], "manuscript_id": r["manuscript_id"],
                            "title": r["title"]})
        return out

    def rename_citekey(self, old: str, new: str) -> int:
        """Rewrite citations of a renamed source in every section; each changed section gets a new revision."""
        changed = 0
        for r in self.conn.execute("SELECT id, rev, body FROM manuscript_sections WHERE body LIKE %s",
                                   (f"%@{old.replace('%', '')}%",)).fetchall():
            body = cite.rename_citekey(r["body"], old, new)
            if body != r["body"]:
                self.save_section(r["id"], r["rev"], body=body, author="human")
                changed += 1
        return changed

    def check(self, manuscript_id: str) -> dict[str, Any]:
        """The citation check: problems a reader or reviewer would catch, most serious first.

        error: a citekey not in the library (or archived); a claim anchor on a node that does not exist, is archived
        or not yet accepted. warning: an anchored claim with no grounding, or only contradicting ones; a grounding
        on an anchored claim whose excerpt was not found in the source. info: the sentence cites a source that does
        not ground the claim it anchors; a source grounds the claim but the sentence does not cite it."""
        ms = self.get(manuscript_id)
        issues: list[dict[str, Any]] = []
        items = {r["citekey"]: r for r in self.conn.execute(
            "SELECT id, citekey, status, title FROM library_items WHERE citekey = ANY(%s)", (ms["citekeys"],))}
        claims = [(s, c) for s in ms["sections"] for c in cite.claims(s["body"])]
        node_ids = list({c.node for _, c in claims})
        nodes = {r["id"]: r for r in self.conn.execute(
            """SELECT o.id, o.name, o.type, o.status, c.status AS cs_status FROM objects o
               LEFT JOIN changesets c ON c.id = o.changeset_id WHERE o.id = ANY(%s)""", (node_ids,))}
        grounds: dict[str, list[dict[str, Any]]] = {}
        for g in self.conn.execute(
            """SELECT g.node_id, g.stance, g.item_id, i.citekey, i.title, e.verified, g.excerpt_id
               FROM groundings g JOIN library_items i ON i.id = g.item_id LEFT JOIN excerpts e ON e.id = g.excerpt_id
               LEFT JOIN changesets c ON c.id = g.changeset_id
               WHERE g.status = 'active' AND g.node_id = ANY(%s) AND (g.changeset_id IS NULL OR c.status = 'accepted')""",
            (node_ids,)):
            grounds.setdefault(g["node_id"], []).append(g)

        def add(level: str, code: str, section: dict[str, Any], message: str, **extra: Any) -> None:
            issues.append({"level": level, "code": code, "section_id": section["id"], "heading": section["heading"],
                           "message": message, **extra})

        for s in ms["sections"]:
            for key in s["citekeys"]:
                it = items.get(key)
                if not it:
                    add("error", "unknown_citekey", s, f"文献库里没有 @{key}", citekey=key)
                elif it["status"] != "active":
                    add("error", "archived_source", s, f"@{key} 对应的文献已归档", citekey=key, item_id=it["id"])
        seen_unverified: set[tuple[str, str]] = set()
        for s, c in claims:
            n = nodes.get(c.node)
            quote = c.text.strip()[:60]
            if not n:
                add("error", "unknown_node", s, f"论断锚点「{quote}」指向的节点不存在", node_id=c.node)
                continue
            if n["status"] != "active":
                add("error", "archived_node", s, f"「{n['name']}」已归档，正文仍锚定它", node_id=c.node)
                continue
            if n["cs_status"] in OPEN_STATUSES:
                add("error", "pending_node", s, f"「{n['name']}」还在待审核的变更里，接受后才能作为论断锚点", node_id=c.node)
                continue
            gs = grounds.get(c.node, [])
            if not gs:
                add("warning", "ungrounded_claim", s, f"「{n['name']}」没有任何文献依据", node_id=c.node)
            elif all(g["stance"] == "contradicts" for g in gs):
                add("warning", "only_contradicted", s, f"「{n['name']}」只有反驳它的依据", node_id=c.node)
            for g in gs:
                if g["excerpt_id"] and g["verified"] is False and (c.node, g["excerpt_id"]) not in seen_unverified:
                    seen_unverified.add((c.node, g["excerpt_id"]))
                    add("warning", "unverified_excerpt", s,
                        f"「{n['name']}」的依据 @{g['citekey']} 用到的摘录没有在原文中核对到", node_id=c.node,
                        citekey=g["citekey"])
            grounding_keys = {g["citekey"] for g in gs if g["stance"] != "contradicts"}
            for key in c.citekeys:
                if key in items and key not in {g["citekey"] for g in gs}:
                    add("info", "cite_not_grounding", s, f"这句引用了 @{key}，但它不是「{n['name']}」的依据",
                        node_id=c.node, citekey=key)
            missing = sorted(grounding_keys - set(c.citekeys))
            if missing:
                add("info", "grounding_not_cited", s,
                    f"写到「{n['name']}」时没有引用它的依据：{'、'.join('@' + k for k in missing)}",
                    node_id=c.node, citekeys=missing)
        order = {"error": 0, "warning": 1, "info": 2}
        issues.sort(key=lambda i: order[i["level"]])
        counts = {lvl: sum(1 for i in issues if i["level"] == lvl) for lvl in order}
        return {"manuscript_id": manuscript_id, "issues": issues, "counts": counts,
                "claims": [{"section_id": s["id"], "node_id": c.node, "text": c.text, "citekeys": c.citekeys,
                            "groundings": [{"citekey": g["citekey"], "stance": g["stance"], "item_id": g["item_id"]}
                                           for g in grounds.get(c.node, [])]} for s, c in claims]}

    # -------------------------------------------------------------- reports

    def suggest_from_report(self, manuscript_id: str, report: dict[str, Any], section_indexes: list[int],
                            after_section_id: str | None = None) -> list[dict[str, Any]]:
        """Turn sections of a research report into insert suggestions. A sentence that rests on one claim-like node
        becomes an anchor on it, followed by citations of that node's accepted groundings."""
        secs = (report.get("body") or {}).get("sections") or []
        chosen = [secs[i] for i in section_indexes if isinstance(i, int) and 0 <= i < len(secs)]
        if not chosen:
            raise OntologyError("choose at least one section of the report")
        node_ids = list({n for sec in chosen for sn in sec.get("sentences") or [] for n in sn.get("nodes") or []})
        keys: dict[str, list[str]] = {}
        for g in self.conn.execute(
            """SELECT g.node_id, i.citekey FROM groundings g JOIN library_items i ON i.id = g.item_id
               LEFT JOIN changesets c ON c.id = g.changeset_id
               WHERE g.status = 'active' AND i.status = 'active' AND g.stance <> 'contradicts' AND g.node_id = ANY(%s)
                 AND (g.changeset_id IS NULL OR c.status = 'accepted') ORDER BY g.created_at""", (node_ids,)):
            keys.setdefault(g["node_id"], []).append(g["citekey"])
        claimlike = {r["id"] for r in self.conn.execute(
            "SELECT id FROM objects WHERE id = ANY(%s) AND type IN ('Claim', 'Hypothesis', 'Observation')", (node_ids,))}
        out = []
        after = after_section_id
        for sec in chosen:
            parts = []
            for sn in sec.get("sentences") or []:
                text = str(sn.get("text") or "").strip()
                if not text:
                    continue
                anchors = [n for n in sn.get("nodes") or [] if n in claimlike]
                cited = list(dict.fromkeys(k for n in sn.get("nodes") or [] for k in keys.get(n, [])))
                end = text[-1] if text[-1] in "。！？.!?" else ""
                core = text[:-1] if end else text
                core = core.replace("[", "［").replace("]", "］")
                if len(anchors) == 1:
                    core = f"[{core}]{{.claim node={anchors[0]}}}"
                cites = f"[{'; '.join('@' + k for k in cited)}]" if cited else ""
                parts.append(core + cites + (end or "。"))
            g = self.suggest_section(manuscript_id, str(sec.get("heading") or "研报摘录"), "".join(parts),
                                     f"来自研报 v{report.get('version')}《{report.get('title', '')}》", after,
                                     author="report")
            out.append(g)
        return out

    # -------------------------------------------------------------- export / import

    def export(self) -> list[dict[str, Any]]:
        out = []
        for m in self.conn.execute("SELECT * FROM manuscripts WHERE status = 'active' ORDER BY created_at").fetchall():
            out.append({"title": m["title"], "kind": m["kind"], "csl_style": m["csl_style"], "lang": m["lang"],
                        "meta": m["meta"], "sections": [{"level": s["level"], "heading": s["heading"], "body": s["body"]}
                                                        for s in self.sections(m["id"])]})
        return out

    def import_export(self, docs: list[dict[str, Any]], node_refs: dict[str, str]) -> list[str]:
        made = []
        for d in docs:
            if not isinstance(d, dict):
                raise OntologyError("each manuscript is an object")
            sections = [{"heading": s.get("heading") or "无标题", "level": s.get("level", 1),
                         "body": cite.rewrite_nodes(s.get("body") or "", node_refs)} for s in d.get("sections") or []]
            made.append(self.create(d.get("title") or "论文", d.get("kind") or "article",
                                    d.get("csl_style") or "gb7714-numeric", d.get("lang") or "zh-CN",
                                    d.get("meta") or {}, sections)["id"])
        return made


def _default_outline(kind: str, lang: str) -> list[dict[str, Any]]:
    if lang == "en-US":
        heads = ["Introduction", "Related Work", "Methods", "Results", "Discussion", "Conclusion"]
    elif kind == "proposal":
        heads = ["研究背景与意义", "国内外研究现状", "研究内容与目标", "研究方法与技术路线", "预期成果", "进度安排"]
    elif kind == "review":
        heads = ["引言", "研究现状", "主要争议", "研究展望", "结论"]
    else:
        heads = ["引言", "相关研究", "研究方法", "结果", "讨论", "结论"]
    return [{"heading": h, "level": 1} for h in heads]
