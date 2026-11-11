"""MCP servers the user connects to this workbench (Streamable HTTP), for the agent's tools and for data sources.

Each server has a name, a URL and request headers. Headers usually carry API keys, so the list lives in a local file
readable only by this user (~/.config/onto-research/mcp.json, or WORKBENCH_LOCAL_MCP_SERVERS), never in the
database, and pages only ever see header values masked. A signed-in cloud workspace has none: there the server's
disk and network are not the user's.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from . import projects
from .errors import OntologyError

ENV = "WORKBENCH_LOCAL_MCP_SERVERS"
TRANSPORTS = {"streamable_http": "Streamable HTTP"}
NAME = re.compile(r"[a-z][a-z0-9_]{1,29}")
HEADER = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,100}")
MAX_HEADERS = 20
CALL_SECONDS = 120
TOOLS_TTL = 300  # seconds a server's tool list is reused before it is asked again
TEST_TRANSPORT: httpx2.AsyncBaseTransport | None = None  # tests serve an MCP app in-process through this

_tools: dict[str, tuple[float, list[dict[str, Any]]]] = {}


# ------------------------------------------------------------------ the local file


def config_file() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return Path(os.environ.get(ENV) or base / "onto-research" / "mcp.json")


def _read() -> list[dict[str, Any]]:
    try:
        data = json.loads(config_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    servers = data.get("servers") if isinstance(data, dict) else None
    return [s for s in servers or [] if isinstance(s, dict) and NAME.fullmatch(str(s.get("name") or ""))]


def _write(servers: list[dict[str, Any]]) -> None:
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        try:
            json.dump({"servers": servers}, handle, ensure_ascii=False, indent=2)
            handle.close()
            os.chmod(handle.name, 0o600)
            os.replace(handle.name, path)
        finally:
            Path(handle.name).unlink(missing_ok=True)


def available() -> bool:
    """Whether this process may use local MCP servers at all."""
    return not projects.workspace_owner.get()


def _local_only() -> None:
    if not available():
        raise OntologyError("MCP 服务只能在本机的工作台或桌面应用里使用")


def mask(value: str) -> str:
    return f"••••{value[-4:]}" if len(value) >= 8 else "••••"


def _public(s: dict[str, Any]) -> dict[str, Any]:
    return {"name": s["name"], "transport": s.get("transport") or "streamable_http", "url": s["url"],
            "enabled": s.get("enabled", True), "headers": [{"key": h["key"], "hint": mask(h["value"])} for h in s["headers"]]}


def list_servers() -> list[dict[str, Any]]:
    """The servers as pages see them: header values masked."""
    _local_only()
    return [_public(s) for s in _read()]


def server(name: str) -> dict[str, Any]:
    """One server with its real headers, for connecting."""
    _local_only()
    for s in _read():
        if s["name"] == name:
            return s
    raise OntologyError(f"no MCP server '{name}'")


def clean(defn: dict[str, Any], before: dict[str, Any] | None = None) -> dict[str, Any]:
    """A checked server definition. A header sent with keep=true and no value keeps its saved value."""
    name = before["name"] if before else str(defn.get("name") or "").strip()
    if not NAME.fullmatch(name):
        raise OntologyError("名称只能用小写字母、数字和下划线，以字母开头，2 到 30 个字符")
    transport = str(defn.get("transport") or "streamable_http")
    if transport not in TRANSPORTS:
        raise OntologyError(f"类型应为 {' / '.join(TRANSPORTS.values())}")
    url = " ".join(str(defn.get("url") or "").split())
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.username is not None or len(url) > 1000:
        raise OntologyError("URL 应以 http:// 或 https:// 开头，且不带用户名密码")
    saved = {h["key"].lower(): h["value"] for h in (before or {}).get("headers", [])}
    headers, seen = [], set()
    items = defn.get("headers") or []
    if not isinstance(items, list) or len(items) > MAX_HEADERS:
        raise OntologyError(f"请求头最多 {MAX_HEADERS} 个")
    for h in items:
        key = str((h or {}).get("key") or "").strip()
        value = str((h or {}).get("value") or "")
        if not key and not value:
            continue
        if not HEADER.fullmatch(key):
            raise OntologyError(f"请求头名称「{key}」不合法：只能用字母、数字和 - _ 等符号")
        if key.lower() in seen:
            raise OntologyError(f"请求头「{key}」重复了")
        if not value and h.get("keep") and key.lower() in saved:
            value = saved[key.lower()]
        if not value or len(value) > 4000 or any(c in value for c in "\r\n"):
            raise OntologyError(f"请求头「{key}」的值不能为空、不能换行，最多 4000 个字符")
        seen.add(key.lower())
        headers.append({"key": key, "value": value})
    return {"name": name, "transport": transport, "url": url, "headers": headers,
            "enabled": bool(defn.get("enabled", True))}


def create(defn: dict[str, Any]) -> dict[str, Any]:
    _local_only()
    s = clean(defn)
    servers = _read()
    if any(x["name"] == s["name"] for x in servers):
        raise OntologyError(f"已经有名为「{s['name']}」的 MCP 服务")
    _write(servers + [s])
    _tools.pop(s["name"], None)
    return _public(s)


def update(name: str, defn: dict[str, Any]) -> dict[str, Any]:
    before = server(name)
    s = clean(defn, before)
    _write([s if x["name"] == name else x for x in _read()])
    _tools.pop(name, None)
    return _public(s)


def delete(name: str) -> None:
    server(name)
    _write([x for x in _read() if x["name"] != name])
    _tools.pop(name, None)


# ------------------------------------------------------------------ talking to a server


class _Refused(Exception):
    """The server answered with an HTTP error; the MCP client only reports a generic one."""

    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


STATUS_HINTS = {
    401: "未授权，检查请求头里的密钥",
    403: "被拒绝，检查请求头里的密钥和权限",
    404: "地址不存在，检查 URL 的路径（常见的是以 /mcp 结尾）",
    405: "这个地址不接受 MCP 请求，检查 URL",
    421: "服务拒绝了这个主机名，换成它认可的地址",
}


@asynccontextmanager
async def connect(s: dict[str, Any]) -> AsyncIterator[Client]:
    headers = {h["key"]: h["value"] for h in s["headers"]}
    statuses: list[int] = []

    async def note(response: Any) -> None:
        if response.status_code >= 400:
            statuses.append(response.status_code)

    http = httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(30, read=CALL_SECONDS),
                              event_hooks={"response": [note]},
                              **({"transport": TEST_TRANSPORT} if TEST_TRANSPORT else {}))
    try:
        async with http, Client(streamable_http_client(s["url"], http_client=http)) as client:
            yield client
    except Exception as e:
        if statuses:
            raise _Refused(statuses[-1]) from e
        raise


def _reason(e: BaseException) -> str:
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    if isinstance(e, _Refused):
        return f"HTTP {e.status}" + (f"，{STATUS_HINTS[e.status]}" if e.status in STATUS_HINTS else "")
    if isinstance(e, httpx2.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    return str(e)[:300] or type(e).__name__


async def fetch_tools(s: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        with anyio.fail_after(CALL_SECONDS):
            async with connect(s) as client:
                listed = await client.list_tools()
    except OntologyError:
        raise
    except BaseException as e:  # connection errors arrive wrapped in task-group exception groups
        if isinstance(e, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            raise
        raise OntologyError(f"连不上 MCP 服务「{s['name']}」：{_reason(e)}") from None
    return [{"name": t.name, "description": (t.description or "").strip(), "input_schema": t.input_schema or {}}
            for t in listed.tools]


async def tools(name: str, fresh: bool = False) -> list[dict[str, Any]]:
    """A server's tools, reused for TOOLS_TTL seconds."""
    hit = _tools.get(name)
    if hit and not fresh and time.monotonic() - hit[0] < TOOLS_TTL:
        return hit[1]
    out = await fetch_tools(server(name))
    _tools[name] = (time.monotonic(), out)
    return out


def result_payload(result: Any) -> tuple[Any, str, bool]:
    """(structured data or the text parsed as JSON when it is JSON, the text, is_error) of a tool result."""
    texts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
    text = "\n".join(texts)
    data = getattr(result, "structured_content", None)
    if data is None and texts:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    return data, text, bool(getattr(result, "is_error", False))


async def call(name: str, tool: str, arguments: dict[str, Any]) -> tuple[Any, str, bool]:
    s = server(name)
    if not s.get("enabled", True):
        raise OntologyError(f"MCP 服务「{name}」已停用")
    try:
        with anyio.fail_after(CALL_SECONDS):
            async with connect(s) as client:
                result = await client.call_tool(tool, arguments)
    except BaseException as e:
        if isinstance(e, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            raise
        raise OntologyError(f"调用 MCP 工具 {name}/{tool} 失败：{_reason(e)}") from None
    return result_payload(result)


def call_sync(name: str, tool: str, arguments: dict[str, Any]) -> tuple[Any, str, bool]:
    """call() from a worker thread (the collector), which has no event loop of its own."""
    return asyncio.run(call(name, tool, arguments))
