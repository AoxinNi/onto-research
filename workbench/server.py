"""Local web workbench: the project list, and per project a canvas, a chat box and the review bar.

    python -m workbench.server          # http://127.0.0.1:8765  (projects), /p/<id> (a project's workbench)

The browser talks to the agent over a WebSocket. Every write the agent makes, and every accept / reject /
revert the user clicks, pushes a fresh graph snapshot to all open pages.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import anthropic
import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ontology import collector, custom_sources, evidence, history, mcp_clients, mcp_server, mineru, news, projects, reports, ths
from ontology import providers as providers_mod
from ontology import sources as sources_mod
from ontology.db import connect
from ontology.errors import OntologyError
from ontology.registry import DEFAULT_PACK, PACKS
from ontology.store import OntologyStore

from . import agent as agent_mod
from . import auth, library_api, manuscripts_api, model_settings, sessions
from .agent import WorkbenchAgent
from .auth import WorkspaceScope
from .cloud import _json_body

STATIC = Path(__file__).parent / "static"
log = logging.getLogger("workbench")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    with connect() as conn:
        if os.environ.get("WORKBENCH_AUTO_MIGRATE", "1") == "0":
            projects.ensure_registry(conn)  # an operator runs `python -m ontology.cli migrate` instead
        else:
            projects.upgrade_all(conn)  # raises, so the instance does not start, if a project cannot be upgraded
        custom_sources.sync_quietly(conn)  # sources added on the 数据源 page
    tasks = []
    if os.environ.get("WORKBENCH_COLLECT", "0") == "1":
        tasks.append(asyncio.create_task(collect_forever()))
    if os.environ.get("REPORT_AUTO", "0") == "1":
        tasks.append(asyncio.create_task(report_forever()))
    yield
    for t in tasks:
        t.cancel()


async def collect_forever() -> None:
    """Run the data collector for every active project; its open pages get a fresh snapshot when data changes."""
    conns: dict[str, psycopg.Connection] = {}
    while True:
        try:
            with connect() as reg:
                ids = projects.active_ids(reg)
                custom_sources.sync_quietly(reg)  # pick up sources changed by another process (CLI, MCP)
        except Exception:
            log.exception("could not read the project list")
            ids = []
        for pid in ids:
            try:
                if pid not in conns or conns[pid].closed:
                    conns[pid] = await asyncio.to_thread(projects.project_connect, pid)
                async with collect_lock(pid):
                    changed = await asyncio.to_thread(collector.run_once, conns[pid])
                if changed:
                    await broadcast(pid, snapshot(pid))
            except Exception:  # a bad pass must not stop collection
                log.exception("collector pass failed for %s", pid)
                conns.pop(pid, None)
        await asyncio.sleep(collector.TICK)


REPORT_MIN_INTERVAL = timedelta(minutes=float(os.environ.get("REPORT_MIN_INTERVAL", "10")))
report_client = None          # replaced in tests
_writing: set[str] = set()    # projects whose report is being written right now


def _report_state(project: str) -> dict[str, Any]:
    """Whether the stored report still matches the graph, and when an automatic rewrite may run."""
    with project_conn(project) as c:
        store = OntologyStore(c, actor="system")
        accepted = store.conn.execute(
            """SELECT 1 FROM objects o LEFT JOIN changesets cs ON cs.id = o.changeset_id
               WHERE o.status = 'active' AND (o.changeset_id IS NULL OR cs.status = 'accepted') LIMIT 1""").fetchone()
        current = reports.fingerprint(store) if accepted else None  # pending-only graphs have nothing to report on
        last = reports.latest(c)
        attempt = reports.last_attempt(c)
    since = datetime.now(UTC) - datetime.fromisoformat(attempt["created_at"]) if attempt else None
    return {"current_hash": current, "stale": bool(current) and (not last or last["graph_hash"] != current),
            "writing": project in _writing, "last_failed": attempt and attempt["status"] == "failed" and attempt["error"],
            "next_auto_in": max(0, int((REPORT_MIN_INTERVAL - since).total_seconds())) if since is not None else 0}


async def write_report(project: str, trigger: str) -> dict[str, Any]:
    if project in _writing:
        raise HTTPException(status_code=409, detail="研报正在生成，稍等")
    _writing.add(project)
    await broadcast(project, {"type": "report_status", "writing": True})
    try:
        def run() -> dict[str, Any]:
            with project_conn(project) as c:
                return reports.write(OntologyStore(c, actor="system"), report_client, trigger)

        report = await asyncio.to_thread(run)
        await broadcast(project, {"type": "report", "report": report, "state": await asyncio.to_thread(_report_state, project)})
        return report
    finally:
        _writing.discard(project)
        await broadcast(project, {"type": "report_status", "writing": False})


async def _auto_report(project: str) -> None:
    """Write the report if it is out of date and the debounce allows it."""
    state = await asyncio.to_thread(_report_state, project)
    if state["stale"] and not state["writing"] and state["next_auto_in"] == 0:
        try:
            await write_report(project, "auto")
        except OntologyError as e:
            log.info("automatic report skipped for %s: %s", project, e)
        except Exception:
            log.exception("automatic report failed for %s", project)


def _spawn(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def report_forever() -> None:
    """Keep each project's report in step with its graph, at most once per REPORT_MIN_INTERVAL."""
    await asyncio.sleep(20)  # let the collector settle the first pass
    while True:
        try:
            with connect() as reg:
                ids = projects.active_ids(reg)
            for pid in ids:
                await _auto_report(pid)
        except Exception:
            log.exception("report loop pass failed")
        await asyncio.sleep(60)


app = FastAPI(title="本体工作台", lifespan=lifespan)
app.add_middleware(WorkspaceScope)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def revalidate_assets(request: Request, call_next):
    """Without Cache-Control browsers cache our scripts heuristically and keep running old code after an
    upgrade. no-cache still uses the ETag, so an unchanged file costs a 304, not a download."""
    response = await call_next(request)
    path = request.url.path
    if (path.startswith("/static/") and "/vendor/" not in path) or response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache"
    return response

clients: dict[str, set[WebSocket]] = {}
_collect_locks: dict[str, asyncio.Lock] = {}


def collect_lock(project: str) -> asyncio.Lock:
    """One collection at a time per project: the background loop and the refresh button take turns."""
    return _collect_locks.setdefault(project, asyncio.Lock())
agent_lock = asyncio.Lock()
Project = Query(projects.DEFAULT, pattern=r"^[a-z][a-z0-9_-]{1,40}$")


def make_agent() -> WorkbenchAgent:
    route = model_settings.resolve(projects.workspace_owner.get() or "")
    if not route.key:
        raise model_settings.SettingsError("请先在平台设置中配置 Agent 模型连接。")
    return WorkbenchAgent(route.client(), model=route.model, standard=route.mode != "environment", close_client=True)


@contextmanager
def human_store(project: str = projects.DEFAULT):
    try:
        conn = projects.project_connect(project)
    except OntologyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    try:
        yield OntologyStore(conn, actor="human")
    finally:
        conn.close()


def snapshot(project: str = projects.DEFAULT) -> dict[str, Any]:
    with human_store(project) as s:
        meta = projects.get(s.conn, project)
        return {
            "type": "graph",
            "project": {"id": meta["id"], "name": meta["name"], "description": meta["description"], "packs": meta["packs"]},
            "graph": s.graph(),
            "review": [c for c in s.list_changesets(limit=50) if c["status"] in ("open", "pending") and c["operations"]],
            "proposed_types": s.get_schema()["proposed_types"],
        }


async def broadcast(project: str, event: dict[str, Any]) -> None:
    for ws in list(clients.get(project, ())):
        try:
            await ws.send_json(event)
        except Exception:
            clients[project].discard(ws)


def _human_action(project: str, fn) -> dict[str, Any]:
    with human_store(project) as s:
        try:
            return fn(s)
        except OntologyError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None


def _registry(fn):
    """Run a project-registry call; its errors become 409s the page can show."""
    with connect() as conn:
        try:
            return fn(conn)
        except OntologyError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None


# ---------------------------------------------------------------------- pages


@app.get("/")
def home() -> FileResponse:
    return FileResponse(STATIC / "home.html")


@app.get("/mcp")
def mcp_page() -> FileResponse:
    return FileResponse(STATIC / "home.html")


@app.get("/sources")
def sources_page() -> FileResponse:
    return FileResponse(STATIC / "home.html")


@app.get("/settings")
def settings_page() -> FileResponse:
    return FileResponse(STATIC / "home.html")


@app.get("/p/{project}")
def workbench_page(project: str) -> FileResponse:
    _registry(lambda c: projects.get(c, project))
    return FileResponse(STATIC / "index.html")


# ------------------------------------------------------------------- projects


class ProjectIn(BaseModel):
    name: str | None = None
    description: str | None = None
    status: str | None = None
    pack: str | None = None  # type pack for a new project (registry.PACKS); ignored when editing


@app.get("/auth/options")
def auth_options() -> dict[str, Any]:
    """Local single-user mode: no sign-in. The cloud entry answers this route itself (cloud.py)."""
    return {"mode": "local", "api": 1, "providers": []}


@app.get("/api/projects")
def list_projects() -> list[dict[str, Any]]:
    return _registry(projects.list_all)


@app.post("/api/projects")
def create_project(body: ProjectIn) -> dict[str, Any]:
    return _registry(lambda c: projects.create(c, body.name or "", body.description, pack=body.pack or DEFAULT_PACK))


@app.get("/api/packs")
def list_packs() -> list[dict[str, Any]]:
    return [{"id": k, "label": v, "default": k == DEFAULT_PACK} for k, v in PACKS.items()]


@app.post("/api/projects/{project}/packs/{pack}")
async def add_project_pack(project: str, pack: str) -> dict[str, Any]:
    """Add a type pack's types to an existing project (merging the types both packs name)."""
    result = _registry(lambda c: projects.add_pack(c, project, pack))
    agent_store = mcp_server._store or mcp_server._stores.get(project)
    if agent_store is not None:
        agent_store.registry = agent_store.load_registry()
    record_history(project, f"加入类型包「{PACKS[pack]}」")
    await broadcast(project, snapshot(project))
    return result


@app.patch("/api/projects/{project}")
def edit_project(project: str, body: ProjectIn) -> dict[str, Any]:
    return _registry(lambda c: projects.update(c, project, body.name, body.description, body.status))


@app.get("/api/projects/template")
def import_template() -> JSONResponse:
    return _download(projects.template(), "本体导入模板.json")


@app.post("/api/projects/import")
async def import_project(request: Request, name: str | None = None) -> dict[str, Any]:
    try:
        doc = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="文件不是有效的 JSON") from None
    return _registry(lambda c: projects.import_doc(c, doc, name))


@app.get("/api/projects/{project}/export")
def export_project(project: str) -> JSONResponse:
    doc = _registry(lambda c: projects.export(c, project))
    return _download(doc, f"{doc['project']['name']}.json")


def _download(doc: dict[str, Any], filename: str) -> JSONResponse:
    return JSONResponse(doc, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})


def _list_models() -> dict[str, Any]:
    user_id = projects.workspace_owner.get() or ""
    try:
        route = model_settings.resolve(user_id)
    except model_settings.SettingsError as error:
        return {"default": agent_mod.MODEL, "base_url": None, "via": None, "models": [], "error": str(error)}
    return model_settings.catalog(route, include_url=not user_id or route.mode == "custom")


@app.get("/api/models")
async def list_models() -> dict[str, Any]:
    return await asyncio.to_thread(_list_models)


def _model_settings_user(request: Request) -> str:
    user_id = projects.workspace_owner.get() or ""
    if user_id:
        return user_id
    if (not request.client or request.client.host not in model_settings.LOCAL_HOSTS
            or request.url.hostname not in model_settings.LOCAL_HOSTS):
        raise HTTPException(status_code=403, detail="本地模型设置仅可从本机访问。")
    # The desktop app talks to a local workbench from its own process, without an Origin; browsers cannot send its
    # client header cross-origin (no CORS preflight is ever granted), so it cannot be used to forge this request.
    same_origin = request.headers.get("origin") == f"{request.url.scheme}://{request.url.netloc}"
    if request.method != "GET" and not same_origin and not auth.desktop_client(request.headers):
        raise HTTPException(status_code=403, detail="请求来源无效。")
    return ""


@app.get("/api/settings/model")
def get_model_settings(request: Request) -> dict:
    return model_settings.public_settings(_model_settings_user(request))


@app.get("/api/settings/model/local/{mode}")
def local_model_catalog(mode: str, request: Request) -> dict:
    if _model_settings_user(request):
        raise HTTPException(status_code=403, detail="本机路由仅在本地工作台可用。")
    try:
        default = "claude-sonnet-5" if mode == "ccswitch" else model_settings.environment_route().model
        route = model_settings.local_provider(mode, default)
    except model_settings.SettingsError as error:
        raise HTTPException(status_code=404, detail=str(error)) from None
    return model_settings.catalog(route, include_url=True)


@app.put("/api/settings/model")
async def put_model_settings(request: Request) -> dict:
    user_id = _model_settings_user(request)
    data = await _json_body(request)
    try:
        return await asyncio.to_thread(model_settings.save, user_id, data)
    except model_settings.SettingsError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    except OSError:
        raise HTTPException(status_code=503, detail="无法保存本机模型设置。") from None


def _mineru_settings_user(request: Request) -> None:
    """MinerU settings live on the computer that runs the workbench: only the local workbench can change them."""
    if _model_settings_user(request):
        raise HTTPException(status_code=403, detail="云端工作台的 MinerU 由运营者在部署环境中配置。")


@app.get("/api/settings/mineru")
def get_mineru_settings(request: Request) -> dict:
    if _model_settings_user(request):
        return {**mineru.status(), "editable": False}
    return {**mineru.settings(), "editable": True}


@app.put("/api/settings/mineru")
async def put_mineru_settings(request: Request) -> dict:
    _mineru_settings_user(request)
    data = await _json_body(request)
    try:
        return {**await asyncio.to_thread(mineru.save_settings, data), "editable": True}
    except OntologyError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    except OSError:
        raise HTTPException(status_code=503, detail="无法保存本机 MinerU 设置。") from None


@app.post("/api/settings/mineru/test")
async def test_mineru_settings(request: Request) -> dict:
    _mineru_settings_user(request)
    try:
        return {"ok": True, "message": await asyncio.to_thread(mineru.check)}
    except OntologyError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None


@app.post("/api/settings/model/test")
async def test_model_settings(request: Request) -> dict:
    user_id = _model_settings_user(request)
    try:
        route = await asyncio.to_thread(model_settings.resolve, user_id)
        if not route.key:
            raise model_settings.SettingsError("请先保存模型连接。")
        async with route.client() as client:
            reply = await client.messages.create(model=route.model, messages=[{"role": "user", "content": "Reply OK."}],
                                                 max_tokens=16, timeout=12)
            if not getattr(reply, "content", None):
                raise model_settings.SettingsError("模型没有返回可识别的消息。")
    except model_settings.SettingsError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    except anthropic.AuthenticationError:
        raise HTTPException(status_code=502, detail="模型服务拒绝了凭据，请检查 API Key 和鉴权方式。") from None
    except (anthropic.APIError, ValueError):
        raise HTTPException(status_code=502, detail="模型测试失败，请检查 Base URL、模型 ID 和服务状态。") from None
    return {"ok": True, "model": route.model}


def _source_health() -> dict[str, dict[str, Any]]:
    """Per provider, across every active project: series bound, how many are failing or stale, last success.
    Each custom source also gets its own entry, under "source:<name>"."""
    health: dict[str, dict[str, Any]] = {}
    custom_feeds = {n for n, _, _ in news.CUSTOM_FEEDS}

    def slot(pid: str) -> dict[str, Any]:
        return health.setdefault(pid, {"series": 0, "errors": 0, "stale": 0, "last_ok": None, "last_error": None, "projects": set()})

    def tally(keys: list[str], project: str, status: str, stale: bool, last_ok: str | None, error: dict | None) -> None:
        for key in keys:
            h = slot(key)
            h["series"] += 1
            h["projects"].add(project)
            h["errors"] += status == "error"
            h["stale"] += stale and status != "error"
            if status == "ok" and last_ok and (h["last_ok"] is None or last_ok > h["last_ok"]):
                h["last_ok"] = last_ok
            if status == "error" and error:
                h["last_error"] = error

    with connect() as reg:
        ids = projects.active_ids(reg)
    for project in ids:
        with project_conn(project) as c:
            store = OntologyStore(c, actor="human")
            for nid, items in store.series.node_data().items():
                for d in items:
                    src = sources_mod.SOURCES.get(d["source"])
                    prov = src.provider if src else "internal"
                    keys = [prov] + ([f"source:{d['source']}"] if prov == custom_sources.PROVIDER else [])
                    tally(keys, project, d["status"], bool(d.get("stale")), d["last_fetch_at"],
                          {"series": d["label"], "error": d["last_error"]} if d["last_error"] else None)
            feeds = c.execute("SELECT name, status, last_fetch_at, last_error FROM feeds").fetchall()
            for f in feeds:
                ts = f["last_fetch_at"].isoformat() if f["last_fetch_at"] else None
                keys = [f"source:{f['name']}"] if f["name"] in custom_feeds else ["rss"]
                tally(keys, project, f["status"], False, ts,
                      {"series": f"RSS · {f['name']}", "error": f["last_error"]} if f["last_error"] else None)
    for h in health.values():
        h["projects"] = sorted(h["projects"])
    return health


EMPTY_HEALTH = {"series": 0, "errors": 0, "stale": 0, "last_ok": None, "last_error": None, "projects": []}


@app.get("/api/sources")
async def list_sources_page() -> dict[str, Any]:
    health = await asyncio.to_thread(_source_health)
    out = []
    for p in providers_mod.PROVIDERS.values():
        srcs = [s.describe() | {"label_example": s.title} for s in sources_mod.SOURCES.values() if s.provider == p.id]
        details = p.describe()
        if projects.workspace_owner.get():
            details.pop("key_hint", None)
        out.append(details | {"sources": srcs, "health": health.get(p.id, EMPTY_HEALTH)})
    can_manage = not projects.workspace_owner.get()
    custom = []
    if can_manage:
        with connect() as reg:
            custom = [c | {"health": health.get(f"source:{c['name']}", EMPTY_HEALTH)} for c in custom_sources.list_all(reg)]
    return {"providers": out, "categories": providers_mod.CATEGORIES, "custom": custom, "can_manage": can_manage}


# ------------------------------------------------------------------ custom data sources
# Defined before /api/sources/{provider_id}/… so that "custom" is not taken for a provider id.


def _manage_sources(request: Request) -> None:
    """Custom sources make this server fetch the URLs they name: only the local workbench and desktop app."""
    if projects.workspace_owner.get() or not request.client or request.client.host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(status_code=403, detail="自定义数据源只能在本机的工作台或桌面应用里管理")


def _custom(fn: Callable[[psycopg.Connection], Any]) -> Any:
    try:
        with connect() as reg:
            return fn(reg)
    except OntologyError as e:
        msg = str(e)
        raise HTTPException(status_code=404 if msg.startswith("no custom source") else 400, detail=msg) from None


@app.post("/api/sources/custom")
async def create_custom_source(body: dict[str, Any], request: Request) -> dict[str, Any]:
    _manage_sources(request)
    return await asyncio.to_thread(_custom, lambda reg: custom_sources.create(reg, body))


@app.post("/api/sources/custom/test")
async def test_custom_source(body: dict[str, Any], request: Request) -> dict[str, Any]:
    """Fetch a definition once, saved or not, and show what came back."""
    _manage_sources(request)
    started = time.monotonic()
    try:
        out = await asyncio.to_thread(custom_sources.test, body.get("definition") or {}, body.get("params") or {})
    except OntologyError as e:
        return {"ok": False, "message": str(e)[:400], "seconds": round(time.monotonic() - started, 1)}
    return {"ok": True, **out, "seconds": round(time.monotonic() - started, 1)}


@app.put("/api/sources/custom/{name}")
async def update_custom_source(name: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    _manage_sources(request)
    return await asyncio.to_thread(_custom, lambda reg: custom_sources.update(reg, name, body))


@app.delete("/api/sources/custom/{name}")
async def delete_custom_source(name: str, request: Request) -> dict[str, Any]:
    """Bindings to it keep the points they have; the collector stops fetching them."""
    _manage_sources(request)
    await asyncio.to_thread(_custom, lambda reg: custom_sources.delete(reg, name))
    return {"deleted": name}


# ------------------------------------------------------------------ MCP servers the user connects
# Their tools go to the agent, and "MCP 工具" data sources call them. Header values never come back to a page.


def _manage_mcp(request: Request) -> None:
    if projects.workspace_owner.get() or not request.client or request.client.host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(status_code=403, detail="MCP 服务只能在本机的工作台或桌面应用里管理")


def _mcp_call(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except OntologyError as e:
        msg = str(e)
        raise HTTPException(status_code=404 if msg.startswith("no MCP server") else 400, detail=msg) from None


def _mcp_users(name: str) -> list[str]:
    """Titles of the custom data sources that call this server."""
    with connect() as reg:
        return [c["title"] for c in custom_sources.list_all(reg) if c["kind"] == "mcp" and c["config"].get("server") == name]


@app.get("/api/mcp/servers")
async def list_mcp_servers(request: Request) -> dict[str, Any]:
    _manage_mcp(request)
    servers = _mcp_call(mcp_clients.list_servers)
    users = await asyncio.to_thread(lambda: {s["name"]: _mcp_users(s["name"]) for s in servers})
    return {"servers": [s | {"used_by": users[s["name"]]} for s in servers], "transports": mcp_clients.TRANSPORTS,
            "file": str(mcp_clients.config_file())}


@app.post("/api/mcp/servers")
async def create_mcp_server(body: dict[str, Any], request: Request) -> dict[str, Any]:
    _manage_mcp(request)
    return _mcp_call(lambda: mcp_clients.create(body))


@app.post("/api/mcp/servers/test")
async def test_mcp_server(body: dict[str, Any], request: Request) -> dict[str, Any]:
    """Connect with a definition (saved or not) and list its tools. Editing: `name` fills in the kept header values."""
    _manage_mcp(request)
    started = time.monotonic()
    try:
        before = mcp_clients.server(body["name"]) if body.get("name") else None
        s = mcp_clients.clean(body.get("definition") or {}, before)
        tools = await mcp_clients.fetch_tools(s)
    except OntologyError as e:
        return {"ok": False, "message": str(e)[:400], "seconds": round(time.monotonic() - started, 1)}
    return {"ok": True, "tools": [{"name": t["name"], "description": t["description"][:200]} for t in tools],
            "seconds": round(time.monotonic() - started, 1)}


@app.get("/api/mcp/servers/{name}/tools")
async def mcp_server_tools(name: str, request: Request) -> dict[str, Any]:
    _manage_mcp(request)
    try:
        tools = await mcp_clients.tools(name, fresh=True)
    except OntologyError as e:
        raise HTTPException(status_code=404 if str(e).startswith("no MCP server") else 502, detail=str(e)) from None
    return {"tools": tools}


@app.put("/api/mcp/servers/{name}")
async def update_mcp_server(name: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    _manage_mcp(request)
    return _mcp_call(lambda: mcp_clients.update(name, body))


@app.delete("/api/mcp/servers/{name}")
async def delete_mcp_server(name: str, request: Request) -> dict[str, Any]:
    _manage_mcp(request)
    _mcp_call(lambda: mcp_clients.delete(name))
    return {"deleted": name}


def _test_provider(provider_id: str) -> dict[str, Any]:
    """One cheap live request to the provider, timed."""
    p = providers_mod.PROVIDERS.get(provider_id)
    if not p:
        raise HTTPException(status_code=404, detail=f"no provider '{provider_id}'")
    if not p.configured():
        where = "在「平台设置」里填写" if p.settings_view == "settings" else f"请在 .env 里设置 {p.auth_env}，然后重启工作台"
        return {"ok": False, "message": f"未配置：{where}"}
    started = time.monotonic()
    now = datetime.now(UTC)
    checks = {
        "fred": lambda: f"DGS10 最新 {sources_mod.source('fred').fetch({'series': 'DGS10'}, now - timedelta(days=14))[-1].value}",
        "ths": lambda: "检索「贵州茅台」→ " + ", ".join(f"{i['name']} {i['thscode']}" for i in ths.search("贵州茅台", "a-share", 1)),
        "mineru": mineru.check,
        "rss": lambda: f"拉到 {len(news.parse_feed(httpx.get(news.FEEDS[1][1], timeout=15, headers={'User-Agent': sources_mod.USER_AGENT}).text))} 篇（{news.FEEDS[1][0]}）",
        "grok": lambda: "magpie 里的 Grok 模型：" + (", ".join(
            m["id"] for m in model_settings.catalog(model_settings.environment_route(), include_url=False)["models"]
            if m["id"].startswith("grok/")
        ) or "没有"),
    }
    if provider_id not in checks:
        return {"ok": True, "message": "这个来源不需要连接外部服务"}
    try:
        message = checks[provider_id]()
        ok = not (provider_id == "grok" and message.endswith("没有"))
        return {"ok": ok, "message": message, "seconds": round(time.monotonic() - started, 1)}
    except (OntologyError, httpx.HTTPError, IndexError, KeyError, ValueError) as e:
        return {"ok": False, "message": str(e)[:300] or type(e).__name__, "seconds": round(time.monotonic() - started, 1)}


@app.post("/api/sources/{provider_id}/test")
async def test_source(provider_id: str) -> dict[str, Any]:
    return await asyncio.to_thread(_test_provider, provider_id)


class KeyIn(BaseModel):
    key: str = ""


@app.put("/api/sources/{provider_id}/key")
async def set_source_key(provider_id: str, body: KeyIn, request: Request) -> dict[str, Any]:
    """Save a provider's API key into .env. Only from this machine: the key never leaves it."""
    if projects.workspace_owner.get() or not request.client or request.client.host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(status_code=403, detail="只能在运行工作台的这台电脑上设置密钥")
    try:
        p = providers_mod.save_key(provider_id, body.key)
    except OntologyError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return p.describe()


@app.get("/api/mcp")
async def mcp_info() -> dict[str, Any]:
    """What an MCP client gets: the tool list, and how to start the server for a project."""
    if projects.workspace_owner.get():
        raise HTTPException(status_code=403, detail="云端工作区仅支持页面内的 Agent。")
    tools = await mcp_server.server.list_tools()
    base = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")
    models = (await asyncio.to_thread(model_settings.catalog, model_settings.environment_route(), include_url=False)
              if base else {"models": [], "error": None})
    return {
        "python": sys.executable,
        "cwd": str(Path(__file__).resolve().parent.parent),
        "env_file": str(providers_mod.ENV_FILE),
        # What the MCP server itself needs besides the client's own model: a gateway only for search_x.
        "gateway": {"base_url": base or None, "magpie": ":3425" in base, "error": models.get("error"),
                    "grok": [m["id"] for m in models["models"] if m["id"].startswith("grok/")]},
        "ths": providers_mod.PROVIDERS["ths"].configured(),
        "instructions": mcp_server.INSTRUCTIONS,
        "tools": [{"name": t.name, "description": t.description or "",
                   "params": list((t.input_schema or {}).get("properties", {}))} for t in tools],
    }


# ------------------------------------------------------------- one project's graph


def record_history(project: str, message: str) -> dict[str, Any] | None:
    """Commit the accepted graph if this action changed it; history must never break the action itself."""
    try:
        with project_conn(project) as c:
            made = history.commit_if_changed(OntologyStore(c, actor="human"), "human", message)
        if made:
            asyncio.get_running_loop().create_task(broadcast(project, {"type": "history", "commit": made}))
        return made
    except Exception:
        log.exception("could not record history for %s", project)
        return None


@app.get("/api/graph")
def get_graph(project: str = Project, at: str | None = None) -> dict[str, Any]:
    """The live graph, or with `at` (a commit id) the accepted graph as it was at that commit."""
    if not at:
        return snapshot(project)
    with human_store(project) as s:
        try:
            past = history.graph_at(s, at)
        except OntologyError as e:
            raise HTTPException(status_code=404, detail=str(e)) from None
        meta = projects.get(s.conn, project)
    return {"type": "graph", "project": {"id": meta["id"], "name": meta["name"], "description": meta["description"], "packs": meta["packs"]},
            "graph": past["graph"], "commit": past["commit"], "review": [], "proposed_types": []}


@app.get("/api/history")
def get_history(project: str = Project) -> dict[str, Any]:
    """Commits of the accepted graph and report versions, oldest first, for the timeline."""
    with human_store(project) as s:
        return {"commits": history.log(s.conn),
                "reports": [r for r in reversed(reports.versions(s.conn)) if r["status"] == "ready"]}


@app.get("/api/nodes/{node_id}/news")
def get_node_news(node_id: str, project: str = Project) -> list[dict[str, Any]]:
    with human_store(project) as s:
        return news.node_news(s.conn, node_id)


@app.get("/api/links/{link_id}/evidence")
def get_link_evidence(link_id: str, project: str = Project) -> dict[str, Any]:
    with human_store(project) as s:
        return {**evidence.link_evidence(s.conn, link_id), "groundings": s.groundings(target_id=link_id)}


@app.get("/api/changesets/{cs}")
def get_changeset(cs: str, project: str = Project) -> dict[str, Any]:
    return _human_action(project, lambda s: s.get_changeset(cs))


@app.post("/api/changesets/{cs}/{action}")
async def decide_changeset(cs: str, action: str, project: str = Project) -> dict[str, Any]:
    ops = {"accept": OntologyStore.accept_changeset, "reject": OntologyStore.reject_changeset,
           "revert": OntologyStore.revert_changeset}
    if action not in ops:
        raise HTTPException(status_code=404)
    result = _human_action(project, lambda s: ops[action](s, cs))
    verb = {"accept": "接受", "reject": "拒绝", "revert": "撤销"}[action]
    record_history(project, f"{verb}变更集「{result.get('summary') or cs}」")
    await broadcast(project, snapshot(project))
    if action == "accept" and os.environ.get("REPORT_AUTO", "0") == "1":
        _spawn(_auto_report(project))  # a newly accepted graph gets its report now, not on the next loop pass
    return result


@app.post("/api/refresh")
async def refresh_data(project: str = Project) -> dict[str, Any]:
    """Fetch the latest data for this project now, then push the updated graph to its open pages."""
    _registry(lambda c: projects.get(c, project))

    def run() -> dict[str, Any]:
        with projects.project_connect(project) as conn:
            return collector.refresh_now(conn)

    try:
        async with collect_lock(project):
            result = await asyncio.to_thread(run)
    except OntologyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    await broadcast(project, snapshot(project))
    task = asyncio.create_task(_refresh_news(project))  # news sites can be slow; the button does not wait for them
    _background.add(task)
    task.add_done_callback(_background.discard)
    result["news"] = "background"
    return result


_background: set[asyncio.Task] = set()


async def _refresh_news(project: str) -> None:
    def run() -> int:
        with projects.project_connect(project) as conn:
            return collector.refresh_news_now(conn)

    try:
        async with collect_lock(project):
            articles = await asyncio.to_thread(run)
        await broadcast(project, snapshot(project) | {"news_refreshed": articles})
    except Exception:
        log.exception("news refresh failed for %s", project)


@app.get("/api/report")
async def get_report(project: str = Project, id: str | None = None, at: str | None = None) -> dict[str, Any]:
    """The latest report (or a given version, or the one in force at a moment), and whether it is current."""
    def run() -> dict[str, Any]:
        with project_conn(project) as c:
            if id:
                return reports.get(c, id)
            return reports.latest(c, datetime.fromisoformat(at) if at else None)

    try:
        report = await asyncio.to_thread(run)
    except OntologyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    return {"report": report, "state": await asyncio.to_thread(_report_state, project)}


@app.post("/api/report/regenerate")
async def regenerate_report(project: str = Project) -> dict[str, Any]:
    _registry(lambda c: projects.get(c, project))
    try:
        return await write_report(project, "manual")
    except OntologyError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"研报生成失败：{e}") from None


@app.post("/api/undo")
async def undo_last(project: str = Project) -> dict[str, Any]:
    result = _human_action(project, lambda s: s.revert_changeset())
    record_history(project, f"撤销变更集「{result.get('summary') or result.get('id')}」")
    await broadcast(project, snapshot(project))
    return result


@app.post("/api/types/{kind}/{name}/{action}")
async def decide_type(kind: str, name: str, action: str, project: str = Project) -> dict[str, Any]:
    if action not in ("accept", "reject"):
        raise HTTPException(status_code=404)
    _human_action(project, lambda s: s.decide_type(kind, name, accept=action == "accept"))
    record_history(project, f"{'接受' if action == 'accept' else '拒绝'}类型「{name}」")
    agent_store = mcp_server._store or mcp_server._stores.get(project)
    if action == "accept" and agent_store is not None:
        agent_store.registry = agent_store.load_registry()
    await broadcast(project, snapshot(project))
    return {"kind": kind, "name": name, "status": f"{action}ed"}


@contextmanager
def project_conn(project: str):
    conn = projects.project_connect(project)
    try:
        yield conn
    finally:
        conn.close()


def session_message(project: str, session_id: str | None) -> dict[str, Any]:
    """The conversation to show: the given one, else the most recent, plus the list for the history menu."""
    with project_conn(project) as c:
        s = (sessions.get(c, session_id) if session_id else None) or sessions.latest_or_create(c)
        return {"type": "session", "session": s, "events": sessions.events(c, s["id"]), "sessions": sessions.recent(c)}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, project: str = projects.DEFAULT, session: str | None = None) -> None:
    try:
        first = snapshot(project)
    except HTTPException:
        await ws.close(code=1008)
        return
    current = session_message(project, session)
    session_id = current["session"]["id"]
    pconn = await asyncio.to_thread(projects.project_connect, project)  # this page's own connection, for its chat

    def record(event: dict[str, Any]) -> None:
        sessions.append(pconn, session_id, event)

    async def emit(event: dict[str, Any]) -> None:
        await ws.send_json(event)
        await asyncio.to_thread(record, event)
        if event.get("type") == "tool" and event.get("ok"):
            if event.get("writes"):
                await broadcast(project, snapshot(project))
            if event.get("name") in agent_mod.LIBRARY_TOOLS:
                await broadcast(project, {"type": "library"})
            if event.get("name") in agent_mod.MANUSCRIPT_TOOLS:
                await broadcast(project, {"type": "manuscript"})

    try:
        await ws.accept()
        clients.setdefault(project, set()).add(ws)
        await ws.send_json(first)
        await ws.send_json(current)
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")
            if kind in ("new_session", "open_session"):
                if kind == "new_session":
                    new_id = sessions.create(pconn)["id"]
                current = session_message(project, new_id if kind == "new_session" else msg.get("id"))
                session_id = current["session"]["id"]
                await ws.send_json(current)
                continue
            if kind == "set_model":
                model = str(msg.get("model") or "").strip()[:120] or None
                sessions.set_model(pconn, session_id, model)
                await ws.send_json({"type": "model", "model": model})
                continue
            if kind != "chat" or not str(msg.get("text", "")).strip():
                continue
            text = msg["text"].strip()
            await asyncio.to_thread(record, {"type": "user", "text": text})
            if msg.get("context"):  # what the page shows (a source, an excerpt, a note), for the agent only
                try:
                    text = await asyncio.to_thread(library_api.chat_context, project, msg["context"]) + text
                except HTTPException:
                    pass
            if agent_lock.locked():
                await ws.send_json({"type": "status", "busy": True, "text": "另一个对话正在修改图，稍等一下。"})
            async with agent_lock:
                await ws.send_json({"type": "status", "busy": True})
                token = mcp_server.current_project.set(project)  # the agent's tools act on this page's project
                agent = None
                try:
                    agent = await asyncio.to_thread(make_agent)
                    agent.messages = sessions.load_messages(pconn, session_id)
                    agent.model = (sessions.get(pconn, session_id) or {}).get("model") or agent.model
                    await agent.run(text, emit)
                except model_settings.SettingsError as error:
                    await emit({"type": "error", "text": str(error)})
                except anthropic.AuthenticationError:
                    await emit({"type": "error", "text": "模型服务拒绝了凭据，请检查平台设置中的 API Key。"})
                except anthropic.APIConnectionError:
                    await emit({"type": "error", "text": "连不上模型服务，请检查平台设置中的 URL 和路由状态。"})
                except anthropic.APIStatusError as e:
                    await emit({"type": "error", "text": f"模型服务返回 HTTP {e.status_code}，请检查模型配置。"})
                except agent_mod.BrokenReply:
                    await emit({"type": "error", "text": "模型服务的回复中途断了（重试了 3 次）。已完成的步骤都保留着，发一句「继续」就能接着做。"})
                except Exception as e:  # keep the socket alive; show the user what broke
                    log.error("agent run failed (%s)", type(e).__name__)
                    await emit({"type": "error", "text": "Agent 执行失败，请重试。"})
                finally:
                    mcp_server.current_project.reset(token)
                    if agent is not None:
                        try:
                            sessions.save_messages(pconn, session_id, agent.export_messages())
                        finally:
                            await agent.close()
                    await ws.send_json({"type": "status", "busy": False})
                    await broadcast(project, snapshot(project))
    except WebSocketDisconnect:
        pass
    finally:
        clients.get(project, set()).discard(ws)
        pconn.close()


library_api.install(app, broadcast=broadcast, snapshot=snapshot, record_history=record_history)
manuscripts_api.install(app, broadcast=broadcast)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=os.environ.get("WORKBENCH_HOST", "127.0.0.1"), port=int(os.environ.get("WORKBENCH_PORT", "8766")))


if __name__ == "__main__":
    providers_mod.load_env_file()
    main()
