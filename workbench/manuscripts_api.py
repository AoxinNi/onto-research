"""HTTP routes of a project's manuscripts: sections, suggestions, the citation check, preview references, export.

A person edits sections directly; every save sends the revision it started from and gets 409 with the current
section when someone else saved first. The agent's suggestions are accepted or rejected here. Pages are told about
changes with {"type": "manuscript", "id": …} on the project's WebSocket.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from ontology import cite, export, projects, reports
from ontology.errors import OntologyError
from ontology.manuscripts import KINDS, Manuscripts, SectionConflict

from .library_api import _attachment

router = APIRouter()
Project = Query(projects.DEFAULT, pattern=r"^[a-z][a-z0-9_-]{1,40}$")
_hooks: dict[str, Callable[..., Any]] = {}


def install(app: Any, *, broadcast: Callable[[str, dict], Awaitable[None]]) -> None:
    _hooks["broadcast"] = broadcast
    app.add_exception_handler(_Conflict, conflict_handler)
    app.include_router(router)


@contextmanager
def _ms(project: str):
    try:
        conn = projects.project_connect(project)
    except OntologyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    try:
        yield Manuscripts(conn, actor="human")
    except export.PandocMissing as e:
        raise HTTPException(status_code=501, detail=str(e)) from None
    except SectionConflict:
        raise
    except OntologyError as e:
        msg = str(e)
        raise HTTPException(status_code=404 if msg.startswith("no ") else 409, detail=msg) from None
    finally:
        conn.close()


def _run(project: str, fn: Callable[[Manuscripts], Any]) -> Any:
    with _ms(project) as m:
        return fn(m)


async def _go(project: str, fn: Callable[[Manuscripts], Any]) -> Any:
    try:
        return await asyncio.to_thread(_run, project, fn)
    except SectionConflict as e:
        raise _Conflict(e.current) from None


class _Conflict(Exception):
    def __init__(self, current: dict[str, Any]):
        self.current = current


async def _changed(project: str, manuscript_id: str, **what: Any) -> None:
    await _hooks["broadcast"](project, {"type": "manuscript", "id": manuscript_id, **what})


def conflict_handler(_request: Any, exc: _Conflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": "另一处已修改这一节", "conflict": True,
                                                  "current": exc.current})


# ---------------------------------------------------------------- manuscripts


class ManuscriptIn(BaseModel):
    title: str
    kind: str = "article"
    csl_style: str = "gb7714-numeric"
    lang: str = "zh-CN"
    meta: dict[str, Any] | None = None
    outline: bool = True


class ManuscriptPatch(BaseModel):
    title: str | None = None
    kind: str | None = None
    csl_style: str | None = None
    lang: str | None = None
    meta: dict[str, Any] | None = None
    archived: bool | None = None


@router.get("/api/manuscripts")
def list_manuscripts(project: str = Project, archived: bool = False) -> dict[str, Any]:
    return {"manuscripts": _run(project, lambda m: m.list(archived)),
            "styles": [{"id": k, "label": v["label"], "numeric": v["numeric"]} for k, v in cite.STYLES.items()],
            "kinds": list(KINDS),
            "pandoc": export.pandoc_path() is not None}


@router.post("/api/manuscripts")
async def create_manuscript(body: ManuscriptIn, project: str = Project) -> dict[str, Any]:
    out = await _go(project, lambda m: m.create(body.title, body.kind, body.csl_style, body.lang, body.meta,
                                                None if body.outline else []))
    await _changed(project, out["id"])
    return out


@router.get("/api/manuscripts/{manuscript_id}")
def get_manuscript(manuscript_id: str, project: str = Project) -> dict[str, Any]:
    return _run(project, lambda m: m.get(manuscript_id))


@router.patch("/api/manuscripts/{manuscript_id}")
async def edit_manuscript(manuscript_id: str, body: ManuscriptPatch, project: str = Project) -> dict[str, Any]:
    out = await _go(project, lambda m: m.update(manuscript_id, body.title, body.kind, body.csl_style, body.lang,
                                                body.meta, body.archived))
    await _changed(project, manuscript_id)
    return out


# ---------------------------------------------------------------- sections


class SectionIn(BaseModel):
    heading: str
    level: int = 1
    body: str = ""
    after_section_id: str | None = None
    at_start: bool = False


class SectionPatch(BaseModel):
    base_rev: int
    heading: str | None = None
    body: str | None = None
    level: int | None = None


class ReorderIn(BaseModel):
    order: list[dict[str, Any]]


@router.post("/api/manuscripts/{manuscript_id}/sections")
async def add_section(manuscript_id: str, body: SectionIn, project: str = Project) -> dict[str, Any]:
    out = await _go(project, lambda m: m.add_section(manuscript_id, body.heading, body.level, body.after_section_id,
                                                     body.body, at_start=body.at_start))
    await _changed(project, manuscript_id, section=out["id"])
    return out


@router.patch("/api/sections/{section_id}")
async def save_section(section_id: str, body: SectionPatch, project: str = Project) -> dict[str, Any]:
    out = await _go(project, lambda m: {**m.save_section(section_id, body.base_rev, body.heading, body.body,
                                                         body.level), "manuscript_id": m._section(section_id)["manuscript_id"]})
    await _changed(project, out["manuscript_id"], section=section_id, rev=out["rev"])
    return out


@router.get("/api/sections/{section_id}")
def read_section(section_id: str, project: str = Project) -> dict[str, Any]:
    return _run(project, lambda m: m.read_section(section_id))


@router.delete("/api/sections/{section_id}")
async def delete_section(section_id: str, project: str = Project) -> dict[str, Any]:
    out = await _go(project, lambda m: m.delete_section(section_id))
    await _changed(project, out["manuscript_id"], deleted=section_id)
    return out


@router.get("/api/sections/{section_id}/revisions")
def section_revisions(section_id: str, project: str = Project) -> list[dict[str, Any]]:
    return _run(project, lambda m: m.revisions(section_id))


@router.post("/api/manuscripts/{manuscript_id}/reorder")
async def reorder(manuscript_id: str, body: ReorderIn, project: str = Project) -> dict[str, Any]:
    out = await _go(project, lambda m: m.reorder(manuscript_id, body.order))
    await _changed(project, manuscript_id)
    return out


# ---------------------------------------------------------------- suggestions


class AcceptIn(BaseModel):
    force: bool = False


class FromReportIn(BaseModel):
    sections: list[int]
    report_id: str | None = None
    after_section_id: str | None = None


@router.get("/api/manuscripts/{manuscript_id}/suggestions")
def list_suggestions(manuscript_id: str, project: str = Project, all: bool = False) -> list[dict[str, Any]]:
    return _run(project, lambda m: m.suggestions(manuscript_id, all))


@router.post("/api/suggestions/{suggestion_id}/accept")
async def accept_suggestion(suggestion_id: str, body: AcceptIn | None = None, project: str = Project) -> dict[str, Any]:
    out = await _go(project, lambda m: m.accept(suggestion_id, bool(body and body.force)))
    await _changed(project, out["manuscript_id"], suggestion=suggestion_id)
    return out


@router.post("/api/suggestions/{suggestion_id}/reject")
async def reject_suggestion(suggestion_id: str, project: str = Project) -> dict[str, Any]:
    out = await _go(project, lambda m: m.reject(suggestion_id))
    await _changed(project, out["manuscript_id"], suggestion=suggestion_id)
    return out


@router.post("/api/manuscripts/{manuscript_id}/from-report")
async def from_report(manuscript_id: str, body: FromReportIn, project: str = Project) -> list[dict[str, Any]]:
    """Sections of a research report become insert suggestions (claims anchored, groundings cited)."""
    def go(m: Manuscripts) -> list[dict[str, Any]]:
        r = reports.get(m.conn, body.report_id) if body.report_id else reports.latest(m.conn)
        if not r:
            raise OntologyError("no report yet")
        return m.suggest_from_report(manuscript_id, r, body.sections, body.after_section_id)

    out = await _go(project, go)
    await _changed(project, manuscript_id)
    return out


# ---------------------------------------------------------------- citations and export


@router.get("/api/manuscripts/{manuscript_id}/check")
def check(manuscript_id: str, project: str = Project) -> dict[str, Any]:
    return _run(project, lambda m: m.check(manuscript_id))


@router.get("/api/manuscripts/{manuscript_id}/references")
async def references(manuscript_id: str, project: str = Project, style: str | None = None) -> dict[str, Any]:
    """Labels and formatted entries of the cited sources, for the preview. Missing keys are listed apart."""
    def go(m: Manuscripts) -> dict[str, Any]:
        ms = m._row(manuscript_id)
        found, missing = m.cited_items(manuscript_id)
        st = style or ms["csl_style"]
        if st not in cite.STYLES:
            raise OntologyError(f"unknown style '{st}'")
        ids = {r["citekey"]: r["id"] for r in m.conn.execute(
            "SELECT id, citekey FROM library_items WHERE citekey = ANY(%s)", ([k for k, _ in found],))}
        out = export.references(found, st, ms["lang"])
        return {**out, "style": st, "missing": missing, "items": ids}

    return await _go(project, go)


@router.get("/api/manuscripts/{manuscript_id}/export")
async def export_manuscript(manuscript_id: str, project: str = Project, format: str = "docx",
                            formatted: bool = False) -> Response:
    def go(m: Manuscripts) -> tuple[bytes, str, str]:
        ms = m.get(manuscript_id)
        found, _missing = m.cited_items(manuscript_id)
        return export.export(ms, found, format, formatted)

    data, name, media = await _go(project, go)
    return _attachment(data, name, media)
