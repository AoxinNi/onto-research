"""The project library: sources (papers, books, web pages…), their files and extracted text, excerpts and notes.

Library content is raw material. People, the agent and imports write it directly; every row says who added it.
What is reviewed is the step from a source into the graph: a grounding (see OntologyStore.ground).

Two rules keep citations honest:
- an item's metadata comes from a resolver (DOI, arXiv, ISBN…), an import file, or a person; the agent cannot type
  metadata in (see resolve.py and the MCP tools);
- an excerpt the agent makes must be found, verbatim up to spacing and punctuation width, in the extracted text.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import time
import unicodedata
import uuid
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .errors import OntologyError

KINDS = ("article", "preprint", "book", "chapter", "thesis", "conference", "report", "webpage", "dataset",
         "software", "other")
# CSL item type -> library kind, and the type written back when a person picks a kind.
CSL_KIND = {
    "article-journal": "article", "article-magazine": "article", "article-newspaper": "article", "review": "article",
    "article": "preprint", "book": "book", "chapter": "chapter", "entry-encyclopedia": "chapter", "thesis": "thesis",
    "paper-conference": "conference", "report": "report", "webpage": "webpage", "post-weblog": "webpage",
    "post": "webpage", "dataset": "dataset", "software": "software",
}
KIND_CSL = {"article": "article-journal", "preprint": "article", "book": "book", "chapter": "chapter",
            "thesis": "thesis", "conference": "paper-conference", "report": "report", "webpage": "webpage",
            "dataset": "dataset", "software": "software", "other": "document"}
READING = ("unread", "reading", "read")
MEDIA = {"application/pdf", "text/plain", "text/markdown", "text/x-web-extract"}
CHUNK = 1500                 # characters per extracted-text chunk
MAX_PAGES = 500              # PDF pages extracted; the rest of a longer file is left out
EXTRACT_SECONDS = 60
MAX_READ = 12_000            # characters one read_item call returns
MIN_QUOTE = 8                # normalised characters an excerpt must have
MAX_CSL = 64 * 1024
MAX_NOTE = 200_000
DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>{}]+)", re.I)
REF = re.compile(r"\[\[((?:n|ex|it)_[0-9a-f]{12})\]\]|(?<![\w.@])@([\w一-鿿][\w一-鿿:.#$%&+?<>~/-]*)")
STOP = {"the", "a", "an", "on", "of", "in", "and", "for", "to", "with", "from", "by", "at", "is", "are"}


def file_limit() -> int:
    return int(os.environ.get("WORKBENCH_LIBRARY_FILE_MB", "50")) * 1024 * 1024


def project_limit() -> int:
    return int(os.environ.get("WORKBENCH_LIBRARY_PROJECT_MB", "1024")) * 1024 * 1024


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# ------------------------------------------------------------------ CSL helpers


def clean_doi(value: Any) -> str | None:
    if not value:
        return None
    m = DOI.search(str(value))
    return m.group(1).rstrip(".,;)]}").lower() if m else None


def _year(csl: dict[str, Any]) -> int | None:
    for key in ("issued", "published-print", "published-online", "created"):
        parts = (csl.get(key) or {}).get("date-parts") if isinstance(csl.get(key), dict) else None
        try:
            if parts and parts[0] and parts[0][0]:
                return int(parts[0][0])
        except (TypeError, ValueError):
            continue
        raw = (csl.get(key) or {}).get("raw") if isinstance(csl.get(key), dict) else None
        if raw and (m := re.search(r"\b(1[5-9]\d\d|20\d\d)\b", str(raw))):
            return int(m.group(1))
    return None


def _person(p: Any) -> str:
    if not isinstance(p, dict):
        return str(p)
    if p.get("literal"):
        return str(p["literal"])
    family, given = str(p.get("family") or "").strip(), str(p.get("given") or "").strip()
    return f"{family}, {given}" if family and given else family or given


def authors_text(csl: dict[str, Any]) -> str:
    people = csl.get("author") or csl.get("editor") or []
    return "; ".join(x for x in (_person(p) for p in people if p) if x)


def normalize_csl(csl: Any, kind: str | None = None) -> tuple[dict[str, Any], str]:
    """A CSL-JSON item fit to store, and the library kind it implies. Raises on what cannot be cited."""
    if not isinstance(csl, dict):
        raise OntologyError("metadata must be a CSL-JSON object")
    import json

    if len(json.dumps(csl, ensure_ascii=False, default=str)) > MAX_CSL:
        raise OntologyError("metadata is too large")
    out = {k: v for k, v in csl.items() if k not in ("id", "citation-key") and v not in (None, "", [], {})}
    title = out.get("title")
    if isinstance(title, list):
        title = title[0] if title else ""
    title = " ".join(str(title or "").split())
    if not title:
        raise OntologyError("a source needs a title")
    out["title"] = title[:1000]
    for key in ("author", "editor"):
        if key in out and not isinstance(out[key], list):
            raise OntologyError(f"{key} must be a list of names")
    if doi := clean_doi(out.get("DOI")):
        out["DOI"] = doi
    else:
        out.pop("DOI", None)
    if kind is not None and kind not in KINDS:
        raise OntologyError(f"kind must be one of {list(KINDS)}")
    kind = kind or CSL_KIND.get(str(out.get("type") or ""), "other")
    out["type"] = out.get("type") if CSL_KIND.get(str(out.get("type") or "")) == kind else KIND_CSL[kind]
    return out, kind


def _ascii_word(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower())


def _cjk(text: str, n: int) -> str:
    return "".join(c for c in text if "一" <= c <= "鿿")[:n]


def citekey_base(csl: dict[str, Any]) -> str:
    """family name + year + first word of the title: zhang2023sleep, 张三2023睡眠."""
    people = csl.get("author") or csl.get("editor") or []
    first = people[0] if people else {}
    family = str(first.get("family") or first.get("literal") or "") if isinstance(first, dict) else str(first)
    name = _ascii_word(family.split()[-1] if family.split() and _ascii_word(family) else family) or _cjk(family, 4)
    year = _year(csl)
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]+", str(csl.get("title") or "")) if w.lower() not in STOP]
    word = _ascii_word(words[0]) if words else _cjk(str(csl.get("title") or ""), 2)
    return f"{name or 'anon'}{year or 'nd'}{word}"[:60]


# ------------------------------------------------------------------ text


def extract_pdf(data: bytes) -> tuple[list[tuple[int, str]], int, dict[str, Any]]:
    """[(page, text)], page count and the document's own metadata. Stops at MAX_PAGES or EXTRACT_SECONDS."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            raise OntologyError("the PDF is encrypted") from None
    started = time.monotonic()
    pages = []
    for i, page in enumerate(reader.pages[:MAX_PAGES]):
        if time.monotonic() - started > EXTRACT_SECONDS:
            break
        try:
            pages.append((i + 1, page.extract_text() or ""))
        except Exception:  # one unreadable page must not lose the rest
            pages.append((i + 1, ""))
    meta = {}
    try:
        info = reader.metadata or {}
        meta = {"title": str(info.get("/Title") or "").strip(), "author": str(info.get("/Author") or "").strip()}
    except Exception:
        pass
    return pages, len(reader.pages), meta


def chunks_of(pages: list[tuple[int | None, str]]) -> list[dict[str, Any]]:
    """Split page texts into chunks of about CHUNK characters at paragraph or line breaks."""
    out, offset = [], 0
    for page, text in pages:
        text = text.replace("\x00", "").strip()
        while text:
            if len(text) <= CHUNK * 1.3:
                piece, text = text, ""
            else:
                cut = max(text.rfind("\n\n", 0, CHUNK), text.rfind("\n", 0, CHUNK), text.rfind("。", 0, CHUNK) + 1,
                          text.rfind(". ", 0, CHUNK) + 1)
                cut = cut if cut > CHUNK // 3 else CHUNK
                piece, text = text[:cut].rstrip(), text[cut:].lstrip()
            out.append({"seq": len(out), "page": page, "char_start": offset, "text": piece})
            offset += len(piece) + 1
    return out


_FOLD = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                       "­": None, " ": " "})


def _norm_char(c: str) -> str:
    return unicodedata.normalize("NFKC", c).translate(_FOLD).casefold()


def fold(text: str) -> tuple[str, list[int]]:
    """Text without spacing and with widths, quotes, dashes and case folded; and each kept character's index.
    Line-end hyphenation ("infor-\\nmation") is joined."""
    text = re.sub(r"(\w)-\n(\w)", lambda m: m.group(1) + "\x01" * 2 + m.group(2), text)
    out, index = [], []
    for i, c in enumerate(text):
        if c == "\x01" or c.isspace():
            continue
        for d in _norm_char(c):
            if not d.isspace():
                out.append(d)
                index.append(i)
    return "".join(out), index


def parse_refs(body: str) -> dict[str, list[str]]:
    refs: dict[str, list[str]] = {"nodes": [], "excerpts": [], "items": [], "citekeys": []}
    for m in REF.finditer(body or ""):
        if m.group(1):
            key = {"n": "nodes", "ex": "excerpts", "it": "items"}[m.group(1).split("_")[0]]
            refs[key].append(m.group(1))
        elif m.group(2):
            refs["citekeys"].append(m.group(2).rstrip(".,;:"))
    return {k: list(dict.fromkeys(v)) for k, v in refs.items()}


# ------------------------------------------------------------------ the library


class Library:
    def __init__(self, conn: psycopg.Connection, actor: str = "human"):
        self.conn = conn
        self.actor = actor

    # -------------------------------------------------------------- items

    def _item_row(self, item_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM library_items WHERE id = %s", (item_id,)).fetchone()
        if not row:
            raise OntologyError(f"no library item '{item_id}'; use search_library to find ids")
        return row

    def _unique_citekey(self, base: str, exclude: str | None = None) -> str:
        taken = {r["citekey"] for r in self.conn.execute(
            "SELECT citekey FROM library_items WHERE citekey LIKE %s AND id IS DISTINCT FROM %s",
            (base.replace("%", "") + "%", exclude))}
        if base not in taken:
            return base
        for suffix in "abcdefghijklmnopqrstuvwxyz":
            if base + suffix not in taken:
                return base + suffix
        return f"{base}{uuid.uuid4().hex[:4]}"

    def _item_out(self, r: dict[str, Any]) -> dict[str, Any]:
        return {"id": r["id"], "citekey": r["citekey"], "kind": r["kind"], "title": r["title"], "authors": r["authors"],
                "year": r["year"], "doi": r["doi"], "url": r["url"], "abstract": r["abstract"], "tags": r["tags"],
                "reading": r["reading"], "origin": r["origin"], "added_by": r["added_by"], "status": r["status"],
                "csl": r["csl"], "created_at": _iso(r["created_at"]), "updated_at": _iso(r["updated_at"])}

    def find_doi(self, doi: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM library_items WHERE lower(doi) = lower(%s) AND status = 'active'", (doi,)
        ).fetchone()
        return self._item_out(row) if row else None

    def add_item(self, csl: dict[str, Any], origin: dict[str, Any], kind: str | None = None,
                 tags: list[str] | None = None, citekey: str | None = None) -> dict[str, Any]:
        """Add a source. One with the DOI of an existing active item returns that item instead."""
        csl, kind = normalize_csl(csl, kind)
        if csl.get("DOI") and (same := self.find_doi(csl["DOI"])):
            return {"item": same, "created": False}
        key = self._check_citekey(citekey) if citekey else self._unique_citekey(citekey_base(csl))
        iid = _new_id("it")
        self.conn.execute(
            """INSERT INTO library_items (id, citekey, kind, csl, title, authors, year, doi, url, abstract, tags,
                                          origin, added_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (iid, key, kind, Jsonb(csl), csl["title"], authors_text(csl), _year(csl), csl.get("DOI"),
             csl.get("URL"), (str(csl.get("abstract") or "")[:20000] or None), _tags(tags), Jsonb(origin), self.actor),
        )
        return {"item": self._item_out(self._item_row(iid)), "created": True}

    def add_resolved(self, resolved: dict[str, Any], tags: list[str] | None = None) -> dict[str, Any]:
        """Add what resolve.resolve returned: the item, and its file (a PDF or a page's text) when it is new."""
        out = self.add_item(resolved["csl"], resolved["origin"], tags=tags)
        f = resolved.get("file")
        if f and out["created"]:
            try:
                out["file"] = self.add_file(out["item"]["id"], f["filename"], f["bytes"], f.get("media_type"))["file"]
            except OntologyError as e:
                out["file_error"] = str(e)
        return out

    def import_entries(self, entries: list[dict[str, Any]], origin: dict[str, Any],
                       tags: list[str] | None = None) -> dict[str, Any]:
        """Add parsed import entries (bibio.parse). A DOI already here is skipped; a taken citekey gets a suffix."""
        added, existing, skipped = [], [], []
        for e in entries:
            try:
                with self.conn.transaction():
                    key = e.get("citekey")
                    if key and self.conn.execute("SELECT 1 FROM library_items WHERE citekey = %s", (key,)).fetchone():
                        key = self._unique_citekey(key)
                    out = self.add_item(e["csl"], origin, tags=list(tags or []) + list(e.get("tags") or []),
                                        citekey=key)
            except (OntologyError, psycopg.errors.DataError) as err:
                skipped.append(f"{str(e['csl'].get('title') or '?')[:80]}: {err}")
                continue
            (added if out["created"] else existing).append(
                {"id": out["item"]["id"], "citekey": out["item"]["citekey"], "title": out["item"]["title"]})
        return {"added": added, "existing": existing, "skipped": skipped}

    def _check_citekey(self, key: str, exclude: str | None = None) -> str:
        key = key.strip()
        if not re.fullmatch(r"[\w一-鿿][\w一-鿿:.#$%&+?<>~/-]{0,79}", key):
            raise OntologyError("a citekey is letters, digits and _ : . - /, starting with a letter or digit")
        if self.conn.execute("SELECT 1 FROM library_items WHERE citekey = %s AND id IS DISTINCT FROM %s",
                             (key, exclude)).fetchone():
            raise OntologyError(f"citekey '{key}' is already used")
        return key

    def update_item(self, item_id: str, csl: dict[str, Any] | None = None, kind: str | None = None,
                    tags: list[str] | None = None, reading: str | None = None,
                    citekey: str | None = None) -> dict[str, Any]:
        row = self._item_row(item_id)
        sets: dict[str, Any] = {}
        if csl is not None or kind is not None:
            new, new_kind = normalize_csl(csl if csl is not None else row["csl"], kind or (row["kind"] if csl is None else None))
            if new.get("DOI") and (same := self.find_doi(new["DOI"])) and same["id"] != item_id:
                raise OntologyError(f"another item already has DOI {new['DOI']}: {same['id']}")
            sets |= {"csl": Jsonb(new), "kind": new_kind, "title": new["title"], "authors": authors_text(new),
                     "year": _year(new), "doi": new.get("DOI"), "url": new.get("URL"),
                     "abstract": str(new.get("abstract") or "")[:20000] or None}
        if tags is not None:
            sets["tags"] = _tags(tags)
        if reading is not None:
            if reading not in READING:
                raise OntologyError(f"reading must be one of {list(READING)}")
            sets["reading"] = reading
        if citekey is not None and citekey != row["citekey"]:
            sets["citekey"] = self._check_citekey(citekey, exclude=item_id)
        if sets:
            cols = ", ".join(f"{k} = %s" for k in sets)
            with self.conn.transaction():
                self.conn.execute(f"UPDATE library_items SET {cols}, updated_at = now() WHERE id = %s",
                                  (*sets.values(), item_id))
                if "citekey" in sets:
                    self._rename_citations(row["citekey"], sets["citekey"])
        return self.get_item(item_id)

    def _rename_citations(self, old: str, new: str) -> None:
        """A renamed citekey is rewritten wherever it is cited: manuscript sections and notes."""
        from . import cite
        from .manuscripts import Manuscripts

        Manuscripts(self.conn, actor=self.actor).rename_citekey(old, new)
        for n in self.conn.execute("SELECT id, body FROM notes WHERE refs->'citekeys' ? %s", (old,)).fetchall():
            self.update_note(n["id"], body=cite.rename_citekey(n["body"], old, new))

    def archive_item(self, item_id: str, restore: bool = False) -> dict[str, Any]:
        row = self._item_row(item_id)
        if not restore:
            used = self.conn.execute(
                "SELECT count(*) AS n FROM groundings WHERE item_id = %s AND status = 'active'", (item_id,)
            ).fetchone()["n"]
            if used:
                raise OntologyError(f"this source grounds {used} node(s) or link(s); remove those groundings first")
            from .manuscripts import Manuscripts

            if cited := Manuscripts(self.conn).citing(row["citekey"]):
                raise OntologyError(f"《{cited[0]['title']}》的「{cited[0]['heading']}」等 {len(cited)} 节引用了这篇文献，"
                                    "先在论文中删去引用")
        self.conn.execute("UPDATE library_items SET status = %s, updated_at = now() WHERE id = %s",
                          ("active" if restore else "archived", item_id))
        return self.get_item(item_id)

    def get_item(self, item_id: str) -> dict[str, Any]:
        out = self._item_out(self._item_row(item_id))
        out["files"] = [{**{k: _iso(v) for k, v in r.items()}} for r in self.conn.execute(
            """SELECT id, filename, media_type, size, pages, extract_status, extract_error, created_at
               FROM library_files WHERE item_id = %s ORDER BY created_at""", (item_id,))]
        out["excerpts"] = self.excerpts(item_id)
        out["groundings"] = [{**{k: _iso(v) for k, v in r.items()}} for r in self.conn.execute(
            """SELECT g.id, coalesce(g.node_id, g.link_id) AS target_id, g.excerpt_id, g.stance, g.rationale,
                      (c.status IN ('open', 'pending')) AS pending,
                      coalesce(o.name, lf.name || ' → ' || lt.name) AS target_name, o.type AS target_type
               FROM groundings g LEFT JOIN changesets c ON c.id = g.changeset_id
               LEFT JOIN objects o ON o.id = g.node_id LEFT JOIN links l ON l.id = g.link_id
               LEFT JOIN objects lf ON lf.id = l.from_id LEFT JOIN objects lt ON lt.id = l.to_id
               WHERE g.item_id = %s AND g.status = 'active' ORDER BY g.created_at""", (item_id,))]
        for g in out["groundings"]:
            g["pending"] = bool(g["pending"])
        out["notes"] = self.notes_citing(item_id=item_id, citekey=out["citekey"])
        from .manuscripts import Manuscripts

        out["cited_in"] = Manuscripts(self.conn).citing(out["citekey"])
        return out

    def list_items(self, q: str | None = None, kind: str | None = None, tag: str | None = None,
                   reading: str | None = None, status: str = "active", limit: int = 50,
                   offset: int = 0) -> dict[str, Any]:
        params = {"q": _like(q.strip()) if q and q.strip() else None, "kind": kind, "tag": tag, "reading": reading,
                  "status": status, "limit": min(max(limit, 1), 200), "offset": max(offset, 0)}
        where = """status = %(status)s AND (%(kind)s::text IS NULL OR kind = %(kind)s)
                   AND (%(tag)s::text IS NULL OR %(tag)s = ANY(tags))
                   AND (%(reading)s::text IS NULL OR reading = %(reading)s)
                   AND (%(q)s::text IS NULL OR title ILIKE %(q)s OR authors ILIKE %(q)s OR citekey ILIKE %(q)s
                        OR doi ILIKE %(q)s OR abstract ILIKE %(q)s OR array_to_string(tags, ' ') ILIKE %(q)s)"""
        total = self.conn.execute(f"SELECT count(*) AS n FROM library_items WHERE {where}", params).fetchone()["n"]
        rows = self.conn.execute(
            f"""SELECT i.*,
                       (SELECT count(*) FROM library_files f WHERE f.item_id = i.id) AS files,
                       (SELECT count(*) FROM excerpts e WHERE e.item_id = i.id) AS excerpt_count,
                       (SELECT count(*) FROM groundings g WHERE g.item_id = i.id AND g.status = 'active') AS grounding_count
                FROM library_items i WHERE {where} ORDER BY i.created_at DESC LIMIT %(limit)s OFFSET %(offset)s""",
            params,
        ).fetchall()
        items = []
        for r in rows:
            item = self._item_out(r)
            item.pop("csl")
            item |= {"files": r["files"], "excerpts": r["excerpt_count"], "groundings": r["grounding_count"]}
            items.append(item)
        return {"items": items, "total": total}

    def tags(self) -> list[dict[str, Any]]:
        return self.conn.execute(
            """SELECT t AS tag, count(*) AS n FROM library_items, unnest(tags) t WHERE status = 'active'
               GROUP BY t ORDER BY n DESC, t"""
        ).fetchall()

    # -------------------------------------------------------------- files and text

    def add_file(self, item_id: str, filename: str, data: bytes, media_type: str | None = None) -> dict[str, Any]:
        """Store a file of an item and extract its text. Returns the file, plus a DOI found in a PDF's first pages."""
        self._item_row(item_id)
        if not data:
            raise OntologyError("the file is empty")
        if len(data) > file_limit():
            raise OntologyError(f"the file is larger than {file_limit() // (1024 * 1024)} MB")
        used = self.conn.execute("SELECT coalesce(sum(size), 0) AS n FROM library_files").fetchone()["n"]
        if used + len(data) > project_limit():
            raise OntologyError("the project's library is full; remove files or ask the operator to raise the limit")
        media_type = sniff(data, filename, media_type)
        digest = hashlib.sha256(data).hexdigest()
        if same := self.conn.execute("SELECT id FROM library_files WHERE item_id = %s AND sha256 = %s",
                                     (item_id, digest)).fetchone():
            return {"file": self._file_out(same["id"]), "created": False, "detected_doi": None, "pdf_meta": {}}
        fid = _new_id("f")
        name = _safe_name(filename)
        self.conn.execute(
            """INSERT INTO library_files (id, item_id, filename, media_type, bytes, size, sha256)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (fid, item_id, name, media_type, data, len(data), digest),
        )
        detected, meta = self._extract(fid, item_id, media_type, data)
        return {"file": self._file_out(fid), "created": True, "detected_doi": detected, "pdf_meta": meta}

    def _extract(self, file_id: str, item_id: str, media_type: str, data: bytes) -> tuple[str | None, dict[str, Any]]:
        pages: list[tuple[int | None, str]]
        meta: dict[str, Any] = {}
        n_pages = None
        try:
            if media_type == "application/pdf":
                pdf_pages, n_pages, meta = extract_pdf(data)
                pages = list(pdf_pages)
            else:
                pages = [(None, data.decode("utf-8", errors="replace"))]
        except OntologyError as e:
            self.conn.execute("UPDATE library_files SET extract_status = 'failed', extract_error = %s WHERE id = %s",
                              (str(e), file_id))
            return None, {}
        except Exception as e:  # a malformed PDF is stored but has no text
            self.conn.execute("UPDATE library_files SET extract_status = 'failed', extract_error = %s WHERE id = %s",
                              (f"{type(e).__name__}: {e}"[:300], file_id))
            return None, {}
        chunks = chunks_of(pages)
        with self.conn.transaction():
            self.conn.execute("DELETE FROM library_chunks WHERE file_id = %s", (file_id,))
            with self.conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO library_chunks (file_id, seq, item_id, page, char_start, text) VALUES (%s, %s, %s, %s, %s, %s)",
                    [(file_id, c["seq"], item_id, c["page"], c["char_start"], c["text"]) for c in chunks],
                )
            self.conn.execute(
                "UPDATE library_files SET extract_status = %s, extract_error = NULL, pages = %s WHERE id = %s",
                ("ok" if any(c["text"].strip() for c in chunks) else "empty", n_pages, file_id),
            )
        head = " ".join(t for p, t in pages[:2])
        return clean_doi(head), meta

    def _file_out(self, file_id: str) -> dict[str, Any]:
        r = self.conn.execute(
            """SELECT id, item_id, filename, media_type, size, pages, extract_status, extract_error, created_at
               FROM library_files WHERE id = %s""", (file_id,)).fetchone()
        if not r:
            raise OntologyError(f"no file '{file_id}'")
        return {k: _iso(v) for k, v in r.items()}

    def add_upload(self, filename: str, data: bytes, media_type: str | None = None,
                   resolve_doi: Any = None) -> dict[str, Any]:
        """A new item from a file. A DOI found in a PDF fills in the metadata (resolve_doi: doi -> resolved) or,
        when an item with that DOI exists, the file goes to that item instead. Otherwise the PDF's own title, or
        the file name, stands in until a person edits it (needs_metadata)."""
        stem = re.sub(r"\.(pdf|txt|md|markdown)$", "", _safe_name(filename), flags=re.I) or "untitled"
        item = self.add_item({"type": "document", "title": stem}, {"kind": "upload", "filename": _safe_name(filename)})
        iid = item["item"]["id"]
        added = self.add_file(iid, filename, data, media_type)
        out = {"item": None, "file": added["file"], "detected_doi": added["detected_doi"], "needs_metadata": True,
               "existing": False}
        doi = added["detected_doi"]
        if doi and (same := self.find_doi(doi)):
            fid = added["file"]["id"]
            self.conn.execute("UPDATE library_files SET item_id = %s WHERE id = %s", (same["id"], fid))
            self.conn.execute("UPDATE library_chunks SET item_id = %s WHERE file_id = %s", (same["id"], fid))
            self.conn.execute("DELETE FROM library_items WHERE id = %s", (iid,))
            return out | {"item": self.get_item(same["id"]), "file": self._file_out(fid), "existing": True,
                          "needs_metadata": False}
        if doi and resolve_doi:
            try:
                resolved = resolve_doi(doi)
                self.update_item(iid, csl=resolved["csl"])
                self.conn.execute("UPDATE library_items SET origin = %s WHERE id = %s",
                                  (Jsonb({"kind": "upload", "filename": _safe_name(filename), "doi": doi}), iid))
                out["needs_metadata"] = False
            except OntologyError:
                pass
        if out["needs_metadata"] and (title := " ".join(str(added["pdf_meta"].get("title") or "").split())):
            if len(title) > 3 and title.lower() not in ("untitled", "microsoft word", "title"):
                self.update_item(iid, csl={**item["item"]["csl"], "title": title})
        row = self._item_row(iid)  # the citekey was made from the file name; make it from the metadata now
        if (key := self._unique_citekey(citekey_base(row["csl"]), exclude=iid)) != row["citekey"]:
            self.conn.execute("UPDATE library_items SET citekey = %s WHERE id = %s", (key, iid))
        return out | {"item": self.get_item(iid)}

    def file_bytes(self, file_id: str) -> dict[str, Any]:
        r = self.conn.execute("SELECT filename, media_type, bytes FROM library_files WHERE id = %s", (file_id,)).fetchone()
        if not r:
            raise OntologyError(f"no file '{file_id}'")
        return {"filename": r["filename"], "media_type": r["media_type"], "bytes": bytes(r["bytes"])}

    def delete_file(self, file_id: str) -> None:
        f = self._file_out(file_id)
        with self.conn.transaction():
            self.conn.execute("UPDATE excerpts SET anchor = NULL WHERE file_id = %s", (file_id,))
            self.conn.execute("DELETE FROM library_files WHERE id = %s", (f["id"],))

    def text(self, item_id: str, page_from: int | None = None, page_to: int | None = None,
             query: str | None = None, max_chars: int = MAX_READ, file_id: str | None = None) -> dict[str, Any]:
        """Extracted text of an item, in order, by page range or around the passages that match `query`."""
        item = self._item_row(item_id)
        rows = self.conn.execute(
            """SELECT c.file_id, c.seq, c.page, c.text FROM library_chunks c JOIN library_files f ON f.id = c.file_id
               WHERE c.item_id = %s AND (%s::text IS NULL OR c.file_id = %s)
                 AND (%s::int IS NULL OR c.page >= %s) AND (%s::int IS NULL OR c.page <= %s)
               ORDER BY f.created_at, c.seq""",
            (item_id, file_id, file_id, page_from, page_from, page_to, page_to),
        ).fetchall()
        if query and query.strip():
            needle = fold(query)[0]
            rows = [r for r in rows if needle and needle in fold(r["text"])[0]]
        out, used, truncated = [], 0, False
        for r in rows:
            if used + len(r["text"]) > max_chars and out:
                truncated = True
                break
            out.append({"file_id": r["file_id"], "seq": r["seq"], "page": r["page"], "text": r["text"]})
            used += len(r["text"])
        pages = [r["page"] for r in out if r["page"] is not None]
        return {"item_id": item_id, "citekey": item["citekey"], "title": item["title"], "chunks": out,
                "truncated": truncated, "pages": [min(pages), max(pages)] if pages else None,
                "has_text": bool(self.conn.execute("SELECT 1 FROM library_chunks WHERE item_id = %s LIMIT 1",
                                                   (item_id,)).fetchone())}

    def search(self, query: str, kind: str | None = None, tags: list[str] | None = None,
               limit: int = 10) -> list[dict[str, Any]]:
        """Items whose metadata or full text matches, each with up to three matching passages and their pages."""
        q = (query or "").strip()
        if not q:
            raise OntologyError("query is empty")
        pat = _like(q)
        rows = self.conn.execute(
            """SELECT i.*, (i.title ILIKE %(p)s OR i.authors ILIKE %(p)s OR i.citekey ILIKE %(p)s OR i.abstract ILIKE %(p)s
                            OR array_to_string(i.tags, ' ') ILIKE %(p)s) AS meta_hit
               FROM library_items i
               WHERE i.status = 'active' AND (%(kind)s::text IS NULL OR i.kind = %(kind)s)
                 AND (%(tags)s::text[] IS NULL OR i.tags && %(tags)s)
                 AND (i.title ILIKE %(p)s OR i.authors ILIKE %(p)s OR i.citekey ILIKE %(p)s OR i.abstract ILIKE %(p)s
                      OR array_to_string(i.tags, ' ') ILIKE %(p)s
                      OR EXISTS (SELECT 1 FROM library_chunks c WHERE c.item_id = i.id AND c.text ILIKE %(p)s))
               ORDER BY 2 DESC, i.created_at DESC LIMIT %(limit)s""",
            {"p": pat, "kind": kind, "tags": tags or None, "limit": min(limit, 50)},
        ).fetchall()
        out = []
        for r in rows:
            hits = self.conn.execute(
                "SELECT page, text FROM library_chunks WHERE item_id = %s AND text ILIKE %s ORDER BY file_id, seq LIMIT 3",
                (r["id"], pat),
            ).fetchall()
            out.append({"id": r["id"], "citekey": r["citekey"], "title": r["title"], "authors": r["authors"],
                        "year": r["year"], "kind": r["kind"], "doi": r["doi"],
                        "passages": [{"page": h["page"], "text": _around(h["text"], q)} for h in hits]})
        return out

    # -------------------------------------------------------------- excerpts

    def excerpts(self, item_id: str) -> list[dict[str, Any]]:
        return [{k: _iso(v) for k, v in r.items()} for r in self.conn.execute(
            """SELECT id, item_id, file_id, quote, page, anchor, verified, comment, color, author, created_at
               FROM excerpts WHERE item_id = %s ORDER BY page NULLS LAST, (anchor->>'seq')::int NULLS LAST, created_at""",
            (item_id,))]

    def locate(self, item_id: str, quote: str, page: int | None = None) -> dict[str, Any] | None:
        """Where `quote` occurs in the item's text: {file_id, seq, start, end, page}, or None. Spacing, widths,
        quote marks, dashes and case are ignored; a match on the given page wins."""
        needle = fold(quote)[0]
        if len(needle) < MIN_QUOTE:
            raise OntologyError(f"a quote needs at least {MIN_QUOTE} characters of text")
        found = None
        for f in self.conn.execute(
            "SELECT DISTINCT file_id FROM library_chunks WHERE item_id = %s", (item_id,)
        ).fetchall():
            chunks = self.conn.execute(
                "SELECT seq, page, text FROM library_chunks WHERE file_id = %s ORDER BY seq", (f["file_id"],)
            ).fetchall()
            full = "\n".join(c["text"] for c in chunks)
            folded, index = fold(full)
            starts, pos = [], 0
            for c in chunks:
                starts.append(pos)
                pos += len(c["text"]) + 1
            at = folded.find(needle)
            while at >= 0:
                a, b = index[at], index[at + len(needle) - 1] + 1
                k = max(i for i, s in enumerate(starts) if s <= a)
                hit = {"file_id": f["file_id"], "seq": chunks[k]["seq"], "start": a - starts[k],
                       "end": b - starts[k], "page": chunks[k]["page"]}
                if page is None or hit["page"] == page:
                    return hit
                found = found or hit
                at = folded.find(needle, at + 1)
        return found

    def add_excerpt(self, item_id: str, quote: str, page: int | None = None, comment: str | None = None,
                    color: str | None = None, require_found: bool | None = None) -> dict[str, Any]:
        """Mark a passage. A quote that is not in the text is refused when require_found, which is the default for
        every author but a person: only a person may keep a paraphrase or a passage from a file not attached."""
        if require_found is None:
            require_found = self.actor != "human"
        item = self._item_row(item_id)
        if item["status"] != "active":
            raise OntologyError("the source is archived")
        quote = " ".join((quote or "").split())
        if not quote:
            raise OntologyError("the quote is empty")
        if len(quote) > 5000:
            raise OntologyError("an excerpt is at most 5000 characters; mark the key passage")
        hit = self.locate(item_id, quote, page)
        if hit is None and require_found:
            has_text = self.conn.execute("SELECT 1 FROM library_chunks WHERE item_id = %s LIMIT 1", (item_id,)).fetchone()
            raise OntologyError(
                "this quote is not in the source's text; copy it exactly from read_item" if has_text
                else "this source has no extracted text, so a quote cannot be checked; ask the user to attach the file")
        eid = _new_id("ex")
        anchor = {k: hit[k] for k in ("file_id", "seq", "start", "end")} if hit else None
        self.conn.execute(
            """INSERT INTO excerpts (id, item_id, file_id, quote, page, anchor, verified, comment, color, author)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (eid, item_id, hit["file_id"] if hit else None, quote, hit["page"] if hit and hit["page"] else page,
             Jsonb(anchor) if anchor else None, hit is not None, (comment or "").strip() or None,
             _color(color), self.actor),
        )
        return self.get_excerpt(eid)

    def get_excerpt(self, excerpt_id: str) -> dict[str, Any]:
        r = self.conn.execute(
            """SELECT e.*, i.citekey, i.title AS item_title FROM excerpts e JOIN library_items i ON i.id = e.item_id
               WHERE e.id = %s""", (excerpt_id,)).fetchone()
        if not r:
            raise OntologyError(f"no excerpt '{excerpt_id}'")
        return {k: _iso(v) for k, v in r.items()}

    def update_excerpt(self, excerpt_id: str, comment: str | None = None, color: str | None = None) -> dict[str, Any]:
        self.get_excerpt(excerpt_id)
        self.conn.execute(
            """UPDATE excerpts SET comment = coalesce(%s, comment), color = coalesce(%s, color), updated_at = now()
               WHERE id = %s""",
            (None if comment is None else comment.strip() or None, _color(color), excerpt_id),
        )
        return self.get_excerpt(excerpt_id)

    def delete_excerpt(self, excerpt_id: str) -> None:
        self.get_excerpt(excerpt_id)
        if self.conn.execute("SELECT 1 FROM groundings WHERE excerpt_id = %s AND status = 'active'",
                             (excerpt_id,)).fetchone():
            raise OntologyError("this excerpt grounds a node or link; remove that grounding first")
        if self.conn.execute("SELECT 1 FROM groundings WHERE excerpt_id = %s", (excerpt_id,)).fetchone():
            raise OntologyError("this excerpt is part of the graph's history and cannot be deleted")
        self.conn.execute("DELETE FROM excerpts WHERE id = %s", (excerpt_id,))

    # -------------------------------------------------------------- notes

    def _note_out(self, r: dict[str, Any]) -> dict[str, Any]:
        return {k: _iso(v) for k, v in r.items()}

    def list_notes(self, q: str | None = None, limit: int = 100, archived: bool = False) -> list[dict[str, Any]]:
        """Active notes, or with `archived` the archived ones (most recently archived first), so they can be restored."""
        pat = _like(q.strip()) if q and q.strip() else None
        order = "updated_at DESC" if archived else "pinned DESC, updated_at DESC"
        return [self._note_out(r) for r in self.conn.execute(
            f"""SELECT id, title, left(body, 240) AS preview, refs, author, pinned, created_at, updated_at FROM notes
                WHERE status = %s AND (%s::text IS NULL OR title ILIKE %s OR body ILIKE %s)
                ORDER BY {order} LIMIT %s""",
            ("archived" if archived else "active", pat, pat, pat, min(limit, 500)))]

    def count_archived_notes(self) -> int:
        return self.conn.execute("SELECT count(*) AS n FROM notes WHERE status = 'archived'").fetchone()["n"]

    def get_note(self, note_id: str) -> dict[str, Any]:
        r = self.conn.execute("SELECT * FROM notes WHERE id = %s", (note_id,)).fetchone()
        if not r:
            raise OntologyError(f"no note '{note_id}'")
        return self._note_out(r)

    def write_note(self, title: str, body: str = "", pinned: bool = False) -> dict[str, Any]:
        title, body = _note_text(title, body)
        nid = _new_id("nt")
        self.conn.execute(
            "INSERT INTO notes (id, title, body, refs, author, pinned) VALUES (%s, %s, %s, %s, %s, %s)",
            (nid, title, body, Jsonb(parse_refs(body)), self.actor, pinned),
        )
        return self.get_note(nid)

    def update_note(self, note_id: str, title: str | None = None, body: str | None = None,
                    pinned: bool | None = None, archived: bool | None = None) -> dict[str, Any]:
        cur = self.get_note(note_id)
        title, body = _note_text(cur["title"] if title is None else title, cur["body"] if body is None else body)
        self.conn.execute(
            """UPDATE notes SET title = %s, body = %s, refs = %s, pinned = coalesce(%s, pinned),
                      status = coalesce(%s, status), updated_at = now() WHERE id = %s""",
            (title, body, Jsonb(parse_refs(body)), pinned,
             None if archived is None else ("archived" if archived else "active"), note_id),
        )
        return self.get_note(note_id)

    def append_note(self, note_id: str, text: str) -> dict[str, Any]:
        cur = self.get_note(note_id)
        return self.update_note(note_id, body=(cur["body"].rstrip() + "\n\n" + text.strip()).strip())

    def notes_citing(self, item_id: str | None = None, citekey: str | None = None,
                     node_id: str | None = None, excerpt_id: str | None = None) -> list[dict[str, Any]]:
        """Notes that point at an item (by id or [@citekey]), a node or an excerpt: back-links."""
        clauses, params = [], []
        if item_id:
            clauses.append("refs->'items' ? %s")
            params.append(item_id)
        if citekey:
            clauses.append("refs->'citekeys' ? %s")
            params.append(citekey)
        if node_id:
            clauses.append("refs->'nodes' ? %s")
            params.append(node_id)
        if excerpt_id:
            clauses.append("refs->'excerpts' ? %s")
            params.append(excerpt_id)
        if not clauses:
            return []
        return [self._note_out(r) for r in self.conn.execute(
            f"""SELECT id, title, left(body, 240) AS preview, author, updated_at FROM notes
                WHERE status = 'active' AND ({' OR '.join(clauses)}) ORDER BY updated_at DESC LIMIT 50""", params)]

    # -------------------------------------------------------------- export / import

    def export(self) -> dict[str, Any]:
        """Items, excerpts and notes for a project file. Files are left out: they are re-added from the source."""
        items = self.conn.execute(
            """SELECT id AS ref, citekey, kind, csl, tags, reading, origin FROM library_items
               WHERE status = 'active' ORDER BY created_at"""
        ).fetchall()
        excerpts = self.conn.execute(
            """SELECT e.id AS ref, e.item_id AS item, e.quote, e.page, e.comment, e.color FROM excerpts e
               JOIN library_items i ON i.id = e.item_id WHERE i.status = 'active' ORDER BY e.created_at"""
        ).fetchall()
        notes = self.conn.execute(
            "SELECT title, body, pinned FROM notes WHERE status = 'active' ORDER BY created_at"
        ).fetchall()
        return {"items": items, "excerpts": excerpts, "notes": notes}

    def import_export(self, doc: dict[str, Any], node_refs: dict[str, str]) -> dict[str, dict[str, str]]:
        """Load what export() wrote into this (new) project. Returns the item and excerpt id maps; node ids in
        note bodies are rewritten with node_refs."""
        items: dict[str, str] = {}
        excerpts: dict[str, str] = {}
        for it in doc.get("items") or []:
            made = self.add_item(it["csl"], it.get("origin") or {"kind": "import"}, it.get("kind"), it.get("tags"),
                                 it.get("citekey"))["item"]
            if it.get("reading") in READING:
                self.conn.execute("UPDATE library_items SET reading = %s WHERE id = %s", (it["reading"], made["id"]))
            items[str(it.get("ref") or made["id"])] = made["id"]
        for ex in doc.get("excerpts") or []:
            if ex.get("item") not in items:
                raise OntologyError(f"excerpt refers to a source that is not in the file: {ex.get('item')}")
            made = self.add_excerpt(items[ex["item"]], ex["quote"], ex.get("page"), ex.get("comment"), ex.get("color"),
                                    require_found=False)
            excerpts[str(ex.get("ref") or made["id"])] = made["id"]
        ids = {**node_refs, **items, **excerpts}
        for n in doc.get("notes") or []:
            body = re.sub(r"\[\[((?:n|ex|it)_[0-9a-f]{12})\]\]", lambda m: f"[[{ids.get(m.group(1), m.group(1))}]]",
                          n.get("body") or "")
            self.write_note(n.get("title") or "笔记", body, bool(n.get("pinned")))
        return {"items": items, "excerpts": excerpts}


# ------------------------------------------------------------------ small helpers


def sniff(data: bytes, filename: str, declared: str | None) -> str:
    """The stored media type, from the bytes first: a file is never served as anything it is not."""
    if data[:5] == b"%PDF-":
        return "application/pdf"
    head = data[:4096]
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as e:
        if len(data) <= 4096 or e.start < len(head) - 3:  # not just a character cut at the 4096-byte mark
            raise OntologyError("only PDF and UTF-8 text files can be added") from None
    if declared == "text/x-web-extract":
        return declared
    return "text/markdown" if filename.lower().endswith((".md", ".markdown")) else "text/plain"


def _safe_name(name: str) -> str:
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join("_" if c in '/\\:*?"<>|' or ord(c) < 32 else c for c in (name or "")).strip().lstrip(".")
    return (name or "file")[:200]


def _tags(tags: list[str] | None) -> list[str]:
    out = []
    for t in tags or []:
        t = " ".join(str(t).split())[:40]
        if t and t not in out:
            out.append(t)
    return out[:30]


def _color(color: str | None) -> str | None:
    if color is None:
        return None
    if not re.fullmatch(r"[a-z]{3,12}", color):
        raise OntologyError("color is a name such as yellow, green, blue, red")
    return color


def _note_text(title: str, body: str) -> tuple[str, str]:
    title = " ".join((title or "").split())[:200]
    if not title:
        raise OntologyError("a note needs a title")
    if len(body or "") > MAX_NOTE:
        raise OntologyError("the note is too long")
    return title, body or ""


def _around(text: str, q: str, width: int = 160) -> str:
    i = text.casefold().find(q.casefold())
    if i < 0:
        return text[: width * 2]
    a, b = max(0, i - width), min(len(text), i + len(q) + width)
    return ("…" if a else "") + text[a:b] + ("…" if b < len(text) else "")
