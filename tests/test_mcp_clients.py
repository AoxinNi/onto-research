"""MCP servers the user connects: saved locally with masked headers, used by the agent and by data sources."""

import asyncio
import json
import socket
import stat
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ontology import collector, custom_sources, mcp_clients, projects, sources
from ontology.errors import OntologyError
from tests.conftest import node
from tests.test_custom_sources import _forget_custom_sources, reg  # noqa: F401  (fixtures)
from tests.test_projects import client  # noqa: F401  (the workbench app with a fake model)
from workbench import agent

SEEN: list[dict[str, str]] = []  # request headers the test server received


def market_app():
    srv = MCPServer("market")

    @srv.tool()
    def quote(symbol: str) -> dict:
        """The latest price of a symbol."""
        return {"symbol": symbol, "price": 12.5}

    @srv.tool()
    def level() -> float:
        """One number."""
        return 42.0

    @srv.tool()
    def broken() -> str:
        """Always fails."""
        raise ToolError("upstream down")

    inner = srv.streamable_http_app()

    async def app(scope, receive, send):
        if scope["type"] == "http":
            SEEN.append({k.decode(): v.decode() for k, v in scope["headers"]})
        await inner(scope, receive, send)

    return app


@pytest.fixture(scope="module")
def market_url():
    """A real Streamable HTTP MCP server on a free local port."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(market_app(), log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(5)


def closed_port() -> str:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}/mcp"


def market(url, **kw):
    return mcp_clients.create({"name": "market", "url": url, "headers": [{"key": "X-Api-Key", "value": "secret-1234"}], **kw})


def test_servers_are_saved_locally_with_header_values_masked(tmp_path):
    made = market("https://mcp.example/mcp")
    assert made == {"name": "market", "transport": "streamable_http", "url": "https://mcp.example/mcp", "enabled": True,
                    "headers": [{"key": "X-Api-Key", "hint": "••••1234"}]}
    assert "secret" not in json.dumps(mcp_clients.list_servers())
    path = mcp_clients.config_file()
    assert path.parent == tmp_path and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert mcp_clients.server("market")["headers"][0]["value"] == "secret-1234"

    # Editing: a header sent back with keep and no value keeps its value; a new one needs a value.
    edited = mcp_clients.update("market", {"url": "https://mirror.example/mcp", "enabled": False, "headers": [
        {"key": "X-Api-Key", "keep": True}, {"key": "Authorization", "value": "Bearer abcdefgh"}]})
    assert edited["enabled"] is False and [h["hint"] for h in edited["headers"]] == ["••••1234", "••••efgh"]
    assert mcp_clients.server("market")["headers"][0]["value"] == "secret-1234"
    with pytest.raises(OntologyError, match="不能为空"):
        mcp_clients.update("market", {"url": "https://x.org/mcp", "headers": [{"key": "X-Other", "keep": True}]})
    assert mcp_clients.update("market", {"url": "https://x.org/mcp", "headers": []})["headers"] == []

    for bad, msg in [
        ({"name": "Market", "url": "https://x.org"}, "小写字母"),
        ({"name": "other", "url": "ftp://x.org"}, "http"),
        ({"name": "other", "url": "https://u:p@x.org"}, "用户名"),
        ({"name": "other", "url": "https://x.org", "transport": "stdio"}, "类型"),
        ({"name": "other", "url": "https://x.org", "headers": [{"key": "a b", "value": "v"}]}, "不合法"),
        ({"name": "other", "url": "https://x.org", "headers": [{"key": "K", "value": "1"}, {"key": "k", "value": "2"}]}, "重复"),
        ({"name": "other", "url": "https://x.org", "headers": [{"key": "K", "value": "a\nb"}]}, "换行"),
        ({"name": "market", "url": "https://x.org"}, "已经有"),
    ]:
        with pytest.raises(OntologyError, match=msg):
            mcp_clients.create(bad)

    mcp_clients.delete("market")
    assert mcp_clients.list_servers() == []
    with pytest.raises(OntologyError, match="no MCP server"):
        mcp_clients.delete("market")


def test_a_cloud_workspace_has_no_local_servers():
    token = projects.workspace_owner.set("user-1")
    try:
        with pytest.raises(OntologyError, match="本机"):
            mcp_clients.list_servers()
        assert asyncio.run(agent.external_tools()) == ([], {})
    finally:
        projects.workspace_owner.reset(token)


def test_tools_are_listed_and_called_with_the_headers(market_url):
    market(market_url)
    SEEN.clear()
    listed = asyncio.run(mcp_clients.tools("market"))
    assert {t["name"] for t in listed} == {"quote", "level", "broken"}
    assert next(t for t in listed if t["name"] == "quote")["input_schema"]["required"] == ["symbol"]
    assert SEEN and all(h.get("x-api-key") == "secret-1234" for h in SEEN)

    data, text, is_error = asyncio.run(mcp_clients.call("market", "quote", {"symbol": "AAPL"}))
    assert data == {"symbol": "AAPL", "price": 12.5} and not is_error
    _, text, is_error = asyncio.run(mcp_clients.call("market", "broken", {}))
    assert is_error and "upstream down" in text

    mcp_clients.update("market", {"url": market_url, "enabled": False, "headers": [{"key": "X-Api-Key", "keep": True}]})
    with pytest.raises(OntologyError, match="已停用"):
        asyncio.run(mcp_clients.call("market", "quote", {"symbol": "AAPL"}))

    down = mcp_clients.create({"name": "down", "url": closed_port()})
    with pytest.raises(OntologyError, match="连不上"):
        asyncio.run(mcp_clients.fetch_tools(mcp_clients.server(down["name"])))
    lost = mcp_clients.create({"name": "lost", "url": market_url.replace("/mcp", "/elsewhere")})
    with pytest.raises(OntologyError, match="HTTP 404，地址不存在"):  # the status, not the SDK's generic error
        asyncio.run(mcp_clients.fetch_tools(mcp_clients.server(lost["name"])))


def test_the_agent_gets_the_tools_of_enabled_servers(market_url):
    market(market_url)
    mcp_clients.create({"name": "down", "url": closed_port()})  # does not answer: left out, not fatal
    mcp_clients.create({"name": "off", "url": market_url, "enabled": False})
    defs, routes = asyncio.run(agent.external_tools())
    assert routes == {"mcp__market__quote": ("market", "quote"), "mcp__market__level": ("market", "level"),
                      "mcp__market__broken": ("market", "broken")}
    quote = next(d for d in defs if d["name"] == "mcp__market__quote")
    assert quote["description"].startswith("[MCP 服务 market]") and quote["input_schema"]["type"] == "object"

    out, is_error = asyncio.run(agent.call_external_tool("market", "quote", {"symbol": "MSFT"}))
    assert not is_error and out.startswith(agent.EXTERNAL_NOTE.format(server="market")) and '"price": 12.5' in out
    out, is_error = asyncio.run(agent.call_external_tool("off", "quote", {"symbol": "MSFT"}))
    assert is_error and "已停用" in out
    assert agent.external_name("a", "get.price/v2") == "mcp__a__get_price_v2"


def test_an_mcp_tool_is_collected_as_a_data_source(reg, store, human, market_url):
    market(market_url)
    made = custom_sources.create(reg, {"name": "px", "kind": "mcp", "title": "行情", "config": {
        "server": "market", "tool": "quote", "arguments": '{"symbol": "{symbol}"}', "value": "price",
        "frequency": "5m", "poll_minutes": 5}})
    assert made["url"] == "mcp://market/quote" and made["params"] == ["symbol"]
    assert made["config"]["arguments"] == {"symbol": "{symbol}"} and made["config"]["time"] == ""
    assert sources.SOURCES["px"].address({"symbol": "AAPL"}) == 'mcp://market/quote {"symbol": "AAPL"}'

    stock = node(store, "Asset", "苹果", props={"symbol": "AAPL", "asset_class": "equity"})
    store.propose_binding(stock["id"], "px", {"symbol": "AAPL"}, "price", "价格")
    human.accept_changeset(store.submit_changeset("node + binding")["id"])
    collector.run_once(store.conn)
    assert store.list_bindings(stock["id"])[0]["latest"]["value"] == 12.5

    # A tool that returns one number needs no value field.
    tried = custom_sources.test({"kind": "mcp", "title": "水位", "config": {"server": "market", "tool": "level"}})
    assert tried["count"] == 1 and tried["last"]["value"] == 42.0
    broken = {"kind": "mcp", "title": "坏", "config": {"server": "market", "tool": "broken", "value": "x"}}
    with pytest.raises(OntologyError, match="upstream down"):
        custom_sources.test(broken)


def test_the_page_api_manages_mcp_servers(client, market_url):
    r = client.post("/api/mcp/servers", json={"name": "market", "url": market_url,
                                              "headers": [{"key": "X-Api-Key", "value": "secret-1234"}]})
    assert r.status_code == 200, r.text
    page = client.get("/api/mcp/servers").json()
    assert page["servers"][0]["headers"] == [{"key": "X-Api-Key", "hint": "••••1234"}] and page["servers"][0]["used_by"] == []
    assert page["transports"] == {"streamable_http": "Streamable HTTP"} and "secret" not in json.dumps(page)
    assert client.post("/api/mcp/servers", json={"name": "Bad", "url": market_url}).status_code == 400

    # Testing a saved server with its kept header, and a draft that cannot connect.
    SEEN.clear()
    tried = client.post("/api/mcp/servers/test", json={"name": "market", "definition": {
        "url": market_url, "headers": [{"key": "X-Api-Key", "keep": True}]}}).json()
    assert tried["ok"] and {t["name"] for t in tried["tools"]} == {"quote", "level", "broken"}
    assert SEEN[-1].get("x-api-key") == "secret-1234"
    failed = client.post("/api/mcp/servers/test", json={"definition": {"name": "draft", "url": closed_port()}}).json()
    assert not failed["ok"] and "连不上" in failed["message"]

    assert [t["name"] for t in client.get("/api/mcp/servers/market/tools").json()["tools"]][0] == "quote"
    assert client.get("/api/mcp/servers/nope/tools").status_code == 404
    r = client.put("/api/mcp/servers/market", json={"url": market_url, "enabled": False, "headers": []})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert client.put("/api/mcp/servers/nope", json={"url": market_url}).status_code == 404
    assert client.delete("/api/mcp/servers/market").json() == {"deleted": "market"}
    assert client.delete("/api/mcp/servers/market").status_code == 404


def test_only_the_local_workbench_manages_mcp_servers():
    from fastapi import HTTPException

    from workbench import server

    server._manage_mcp(SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")))
    with pytest.raises(HTTPException):
        server._manage_mcp(SimpleNamespace(client=SimpleNamespace(host="10.0.0.8")))
