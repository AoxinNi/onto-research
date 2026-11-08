"""HTTP routes of a project's library: sources, their files and text, excerpts, notes, and groundings.

Library writes take effect at once (they are raw material). A grounding a person adds here is direct too; one the
agent proposes goes through review like its other writes. Pages are told about library changes with a
{"type": "library"} WebSocket message and about groundings with a fresh graph snapshot.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote, unquote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from ontology import bibio, projects, resolve
from ontology.errors import OntologyError
from ontology.library import Library, file_limit
from ontology.store import STANCES, OntologyStore

router = APIRouter()
Project = Query(projects.DEFAULT, pattern=r"^[a-z][a-z0-9_-]{1,40}$")
_hooks: dict[str, Callable[..., Any]] = {}


def install(app: Any, *, broadcast: Callable[[str, dict], Awaitable[None]], snapshot: Callable[[str], dict],
            record_history: Callable[[str, str], Any]) -> None:
    _hooks.update(broadcast=broadcast, snapshot=snapshot, record_history=record_history)
    app.include_router(router)


@contextmanager
def _lib(project: str):
    try:
        conn = projects.project_connect(project)
    except OntologyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    try:
        yield Library(conn, actor="human"), OntologyStore(conn, actor="human")
    except OntologyError as e:
        msg = str(e)
        raise HTTPException(status_code=404 if msg.startswith("no ") else 409, detail=msg) from None
    finally:
        conn.close()


def _run(project: str, fn: Callable[[Library, OntologyStore], Any]) -> Any:
    with _lib(project) as (lib, store):
        return fn(lib, store)


async def _changed(project: str, **what: Any) -> None:
    await _hooks["broadcast"](project, {"type": "library", **what})


async def _graph_changed(project: str, message: str) -> None:
    _hooks["record_history"](project, message)
    await _hooks["broadcast"](project, _hooks["snapshot"](project))


async def _body(request: Request, limit: int) -> bytes:
    """The raw request body, refused as soon as it passes `limit` bytes."""
    if int(request.headers.get("content-length") or 0) > limit:
        raise HTTPException(status_code=413, detail=f"文件超过 {limit // (1024 * 1024)} MB")
    data = bytearray()
    async for part in request.stream():
        data += part
        if len(data) > limit:
            raise HTTPException(status_code=413, detail=f"文件超过 {limit // (1024 * 1024)} MB")
    if not data:
        raise HTTPException(status_code=400, detail="文件是空的")
    return bytes(data)


def _filename(request: Request) -> str:
    return unquote(request.headers.get("x-filename") or "file")[:300]


# ---------------------------------------------------------------- items


class ItemIn(BaseModel):
    identifier: str | None = None
    csl: dict[str, Any] | None = None
    kind: str | None = None
    tags: list[str] | None = None
    citekey: str | None = None


class ItemPatch(BaseModel):
    csl: dict[str, Any] | None = None
    kind: str | None = None
    tags: list[str] | None = None
    citekey: str | None = None
    reading: str | None = None


@router.get("/api/library")
def list_items(project: str = Project, q: str | None = None, kind: str | None = None, tag: str | None = None,
               reading: str | None = None, archived: bool = False, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    return _run(project, lambda lib, _: {
        **lib.list_items(q, kind, tag, reading, "archived" if archived else "active", limit, offset),
        "tags": lib.tags()})


@router.get("/api/library/search")
def search_library(q: str, project: str = Project, kind: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Metadata and full text, with matching passages."""
    return _run(project, lambda lib, _: lib.search(q, kind, None, limit))


@router.get("/api/scholarly")
async def search_scholarly(q: str, year_from: int | None = None, limit: int = 10) -> list[dict[str, Any]]:
    try:
        return await asyncio.to_thread(resolve.search_scholarly, q, year_from, limit)
    except OntologyError as e:
        raise HTTPException(status_code=502, detail=str(e)) from None


@router.post("/api/library")
async def add_item(body: ItemIn, project: str = Project) -> dict[str, Any]:
    """From an identifier (DOI, arXiv, ISBN, OpenAlex id, web address), or metadata a person typed in."""
    if body.identifier:
        try:
            resolved = await asyncio.to_thread(resolve.resolve, body.identifier)
        except OntologyError as e:
            raise HTTPException(status_code=422, detail=str(e)) from None
        out = await asyncio.to_thread(_run, project, lambda lib, _: lib.add_resolved(resolved, body.tags))
    elif body.csl:
        out = await asyncio.to_thread(_run, project, lambda lib, _: lib.add_item(
            body.csl, {"kind": "manual"}, body.kind, body.tags, body.citekey))
    else:
        raise HTTPException(status_code=400, detail="give an identifier or the metadata")
    await _changed(project, item=out["item"]["id"])
    return out


@router.post("/api/library/import")
async def import_items(request: Request, project: str = Project, format: str | None = None) -> dict[str, Any]:
    """A BibTeX, RIS or CSL-JSON file as the raw body; X-Filename helps tell the format."""
    data = await _body(request, bibio.MAX_IMPORT)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="导入文件需要是 UTF-8 文本") from None
    name = _filename(request)

    def go(lib: Library, _: OntologyStore) -> dict[str, Any]:
        parsed = bibio.parse(text, name, format)
        out = lib.import_entries(parsed["entries"], {"kind": "import", "format": parsed["format"], "file": name})
        return {**out, "format": parsed["format"], "skipped": parsed["skipped"] + out["skipped"]}

    out = await asyncio.to_thread(_run, project, go)
    await _changed(project)
    return out


@router.get("/api/library/export.bib")
def export_bibtex(project: str = Project) -> Response:
    def go(lib: Library, _: OntologyStore) -> str:
        rows = lib.conn.execute(
            "SELECT citekey, csl FROM library_items WHERE status = 'active' ORDER BY citekey").fetchall()
        return bibio.to_bibtex(rows)

    return _attachment(_run(project, go).encode("utf-8"), "library.bib", "application/x-bibtex; charset=utf-8")


@router.post("/api/library/upload")
async def upload_new(request: Request, project: str = Project) -> dict[str, Any]:
    """A new source from a file (raw body, X-Filename). A DOI printed in a PDF fills in its metadata."""
    data = await _body(request, file_limit())
    name = _filename(request)

    def lookup(doi: str) -> dict[str, Any]:
        return resolve.doi(doi)

    out = await asyncio.to_thread(_run, project, lambda lib, _: lib.add_upload(
        name, data, request.headers.get("content-type"), lookup))
    await _changed(project, item=out["item"]["id"])
    return out


@router.get("/api/library/{item_id}")
def get_item(item_id: str, project: str = Project) -> dict[str, Any]:
    return _run(project, lambda lib, _: lib.get_item(item_id))


@router.patch("/api/library/{item_id}")
async def edit_item(item_id: str, body: ItemPatch, project: str = Project) -> dict[str, Any]:
    out = await asyncio.to_thread(_run, project, lambda lib, _: lib.update_item(
        item_id, body.csl, body.kind, body.tags, body.reading, body.citekey))
    await _changed(project, item=item_id)
    return out


@router.delete("/api/library/{item_id}")
async def archive_item(item_id: str, project: str = Project, restore: bool = False) -> dict[str, Any]:
    out = await asyncio.to_thread(_run, project, lambda lib, _: lib.archive_item(item_id, restore))
    await _changed(project, item=item_id)
    return out


@router.post("/api/library/{item_id}/files")
async def add_file(item_id: str, request: Request, project: str = Project) -> dict[str, Any]:
    data = await _body(request, file_limit())
    name = _filename(request)
    out = await asyncio.to_thread(_run, project, lambda lib, _: lib.add_file(
        item_id, name, data, request.headers.get("content-type")))
    await _changed(project, item=item_id)
    return out


def _attachment(data: bytes, filename: str, media_type: str, inline: bool = False) -> Response:
    ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\;' else "_" for c in filename) or "file"
    disposition = "inline" if inline else "attachment"
    return Response(data, media_type=media_type, headers={
        "Content-Disposition": f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'",
        "Cache-Control": "private, no-cache"})


@router.get("/api/library/files/{file_id}")
def get_file(file_id: str, project: str = Project, inline: bool = False) -> Response:
    f = _run(project, lambda lib, _: lib.file_bytes(file_id))
    media = f["media_type"]
    served = "application/pdf" if media == "application/pdf" else "text/plain; charset=utf-8"
    return _attachment(f["bytes"], f["filename"], served, inline=inline and media == "application/pdf")


@router.delete("/api/library/files/{file_id}")
async def delete_file(file_id: str, project: str = Project) -> dict[str, Any]:
    def go(lib: Library, _: OntologyStore) -> str:
        item_id = lib._file_out(file_id)["item_id"]
        lib.delete_file(file_id)
        return item_id

    item_id = await asyncio.to_thread(_run, project, go)
    await _changed(project, item=item_id)
    return {"deleted": file_id}


@router.get("/api/library/{item_id}/text")
def item_text(item_id: str, project: str = Project, page_from: int | None = None, page_to: int | None = None,
              q: str | None = None, file_id: str | None = None, max_chars: int = 60000) -> dict[str, Any]:
    return _run(project, lambda lib, _: lib.text(item_id, page_from, page_to, q, min(max_chars, 200_000), file_id))


# ---------------------------------------------------------------- excerpts


class ExcerptIn(BaseModel):
    quote: str
    page: int | None = None
    comment: str | None = None
    color: str | None = None


class ExcerptPatch(BaseModel):
    comment: str | None = None
    color: str | None = None


@router.post("/api/library/{item_id}/excerpts")
async def add_excerpt(item_id: str, body: ExcerptIn, project: str = Project) -> dict[str, Any]:
    out = await asyncio.to_thread(_run, project, lambda lib, _: lib.add_excerpt(
        item_id, body.quote, body.page, body.comment, body.color))
    await _changed(project, item=item_id)
    return out


@router.get("/api/excerpts/{excerpt_id}")
def get_excerpt(excerpt_id: str, project: str = Project) -> dict[str, Any]:
    return _run(project, lambda lib, _: lib.get_excerpt(excerpt_id))


@router.patch("/api/excerpts/{excerpt_id}")
async def edit_excerpt(excerpt_id: str, body: ExcerptPatch, project: str = Project) -> dict[str, Any]:
    out = await asyncio.to_thread(_run, project, lambda lib, _: lib.update_excerpt(excerpt_id, body.comment, body.color))
    await _changed(project, item=out["item_id"])
    return out


@router.delete("/api/excerpts/{excerpt_id}")
async def delete_excerpt(excerpt_id: str, project: str = Project) -> dict[str, Any]:
    def go(lib: Library, _: OntologyStore) -> str:
        item_id = lib.get_excerpt(excerpt_id)["item_id"]
        lib.delete_excerpt(excerpt_id)
        return item_id

    item_id = await asyncio.to_thread(_run, project, go)
    await _changed(project, item=item_id)
    return {"deleted": excerpt_id}


# ---------------------------------------------------------------- notes


class NoteIn(BaseModel):
    title: str
    body: str = ""
    pinned: bool = False


class NotePatch(BaseModel):
    title: str | None = None
    body: str | None = None
    pinned: bool | None = None
    archived: bool | None = None


@router.get("/api/notes")
def list_notes(project: str = Project, q: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    return _run(project, lambda lib, _: lib.list_notes(q, limit))


@router.get("/api/notes/backlinks")
def note_backlinks(project: str = Project, item_id: str | None = None, node_id: str | None = None,
                   excerpt_id: str | None = None) -> list[dict[str, Any]]:
    def go(lib: Library, _: OntologyStore) -> list[dict[str, Any]]:
        citekey = lib.get_item(item_id)["citekey"] if item_id else None
        return lib.notes_citing(item_id=item_id, citekey=citekey, node_id=node_id, excerpt_id=excerpt_id)

    return _run(project, go)


@router.get("/api/notes/{note_id}")
def get_note(note_id: str, project: str = Project) -> dict[str, Any]:
    return _run(project, lambda lib, _: lib.get_note(note_id))


@router.post("/api/notes")
async def write_note(body: NoteIn, project: str = Project) -> dict[str, Any]:
    out = await asyncio.to_thread(_run, project, lambda lib, _: lib.write_note(body.title, body.body, body.pinned))
    await _changed(project, note=out["id"])
    return out


@router.patch("/api/notes/{note_id}")
async def edit_note(note_id: str, body: NotePatch, project: str = Project) -> dict[str, Any]:
    out = await asyncio.to_thread(_run, project, lambda lib, _: lib.update_note(
        note_id, body.title, body.body, body.pinned, body.archived))
    await _changed(project, note=note_id)
    return out


# ---------------------------------------------------------------- groundings


class GroundingIn(BaseModel):
    target_id: str
    item_id: str
    stance: str
    rationale: str
    excerpt_id: str | None = None


@router.get("/api/groundings")
def list_groundings(project: str = Project, target_id: str | None = None,
                    item_id: str | None = None) -> list[dict[str, Any]]:
    return _run(project, lambda _, s: s.groundings(target_id=target_id, item_id=item_id))


@router.post("/api/groundings")
async def add_grounding(body: GroundingIn, project: str = Project) -> dict[str, Any]:
    """A source a person ties to a node or link while reading: in effect at once, and kept in history."""
    if body.stance not in STANCES:
        raise HTTPException(status_code=400, detail=f"stance must be one of {list(STANCES)}")
    out = await asyncio.to_thread(_run, project, lambda _, s: s.ground_direct(
        body.target_id, body.item_id, body.stance, body.rationale, body.excerpt_id, {"kind": "user"}))
    await _graph_changed(project, "添加文献依据")
    await _changed(project, item=body.item_id)
    return out


@router.delete("/api/groundings/{grounding_id}")
async def remove_grounding(grounding_id: str, project: str = Project) -> dict[str, Any]:
    """Remove a grounding. A reviewed one is removed through a changeset the person accepts at once, so the
    removal can be undone like any other."""
    def go(_: Library, s: OntologyStore) -> dict[str, Any]:
        g = s.get_grounding(grounding_id)
        out = s.archive(grounding_id, "移除文献依据")
        if s.conn.execute("SELECT 1 FROM changesets WHERE actor = 'human' AND status = 'open'").fetchone():
            cs = s.submit_changeset("移除文献依据")
            s.accept_changeset(cs["id"])
        return {**out, "item_id": g["item_id"]}

    out = await asyncio.to_thread(_run, project, go)
    await _graph_changed(project, "移除文献依据")
    await _changed(project, item=out["item_id"])
    return out


# ---------------------------------------------------------------- chat context


def chat_context(project: str, context: Any) -> str:
    """One line telling the agent what the person is looking at, from ids the page sends with a message."""
    if not isinstance(context, dict):
        return ""
    parts = []
    with _lib(project) as (lib, store):
        if (iid := context.get("item_id")) and isinstance(iid, str):
            try:
                it = lib.get_item(iid)
                page = context.get("page")
                parts.append(f"文献 {it['id']} [@{it['citekey']}]《{it['title'][:120]}》"
                             + (f" 第 {int(page)} 页" if isinstance(page, int) else ""))
            except OntologyError:
                pass
        if (eid := context.get("excerpt_id")) and isinstance(eid, str):
            try:
                ex = lib.get_excerpt(eid)
                parts.append(f"摘录 {ex['id']}：「{ex['quote'][:300]}」")
            except OntologyError:
                pass
        if (nid := context.get("note_id")) and isinstance(nid, str):
            try:
                parts.append(f"笔记 {nid}《{lib.get_note(nid)['title'][:120]}》")
            except OntologyError:
                pass
        if (node := context.get("node_id")) and isinstance(node, str):
            try:
                parts.append(f"节点 {node}（{store.get_node(node)['name'][:80]}）")
            except OntologyError:
                pass
    return f"（我正在看：{'；'.join(parts)}）\n" if parts else ""
