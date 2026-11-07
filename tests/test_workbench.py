import pytest
from fastapi.testclient import TestClient

from ontology import mcp_server, projects
from tests.conftest import TEST_URL
from tests.fake_claude import LINKS, NODES, FakeClaude
from workbench import server
from workbench.agent import WorkbenchAgent, describe_call


@pytest.fixture
def client(conn, monkeypatch):
    projects.create(conn, "测试研究项目", project_id=projects.DEFAULT)
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    monkeypatch.setattr(mcp_server, "_store", None)
    monkeypatch.setattr(mcp_server, "_stores", {})
    monkeypatch.setattr(server, "make_agent", lambda: WorkbenchAgent(client=FakeClaude()))
    with TestClient(server.app) as c:
        yield c


def drain(ws):
    """Read events until the agent reports it is idle again."""
    events = []
    while True:
        msg = ws.receive_json()
        events.append(msg)
        if msg["type"] == "status" and msg["busy"] is False:
            return events


def test_chat_builds_graph_and_review_accepts_it(client):
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "graph" and first["graph"]["nodes"] == []
        ws.send_json({"type": "chat", "text": "帮我搭一个以 BTC 为核心的概念图"})
        events = drain(ws)

    tools = [e for e in events if e["type"] == "tool"]
    assert len(tools) == len(NODES) + len(LINKS) + 1 and all(t["ok"] for t in tools), [t for t in tools if not t["ok"]]
    assert any(e["type"] == "assistant" and "审核" in e["text"] for e in events)

    snap = client.get("/api/graph").json()
    assert len(snap["graph"]["nodes"]) == len(NODES) and len(snap["graph"]["links"]) == len(LINKS)
    assert all(n["pending"] for n in snap["graph"]["nodes"])
    [cs] = snap["review"]
    assert cs["status"] == "pending" and cs["summary"] == "以比特币为核心的概念图"

    assert client.post(f"/api/changesets/{cs['id']}/accept").status_code == 200
    snap = client.get("/api/graph").json()
    assert snap["review"] == [] and not any(n["pending"] for n in snap["graph"]["nodes"])

    assert client.post("/api/undo").status_code == 200
    assert client.get("/api/graph").json()["graph"]["nodes"] == []


def test_human_errors_come_back_as_409(client):
    r = client.post("/api/changesets/cs_missing/accept")
    assert r.status_code == 409 and "no changeset" in r.json()["detail"]


def test_describe_call_labels():
    assert describe_call("upsert_node", {"type": "Asset", "name": "比特币"}) == "upsert_node Asset 比特币"
    assert describe_call("search_nodes", {"query": "btc"}) == "search_nodes btc"
    assert describe_call("validate_graph", {}) == "validate_graph"


def test_agent_retries_an_unreadable_reply(monkeypatch):
    import asyncio
    import json as _json
    from types import SimpleNamespace as NS

    from workbench import agent as agent_mod

    monkeypatch.setattr(agent_mod, "BROKEN_RETRIES", (0, 0))
    calls = []

    class Client:
        class beta:
            class messages:
                @staticmethod
                async def create(**kw):
                    calls.append(1)
                    if len(calls) < 3:
                        raise _json.JSONDecodeError("Expecting value", "", 0)
                    return NS(stop_reason="end_turn", content=[NS(type="text", text="好了")])

    a = agent_mod.WorkbenchAgent(client=Client())
    out = []

    async def emit(e):
        out.append(e)

    asyncio.run(a.run("hi", emit))
    assert len(calls) == 3 and out == [{"type": "assistant", "text": "好了"}]

    class Dead(Client):
        class beta:
            class messages:
                @staticmethod
                async def create(**kw):
                    raise _json.JSONDecodeError("Expecting value", "", 0)

    import pytest
    with pytest.raises(agent_mod.BrokenReply):
        asyncio.run(agent_mod.WorkbenchAgent(client=Dead()).run("hi", emit))


def test_first_start_is_empty_and_new_projects_have_no_default_bindings(conn, monkeypatch):
    from pathlib import Path

    from fastapi.testclient import TestClient

    from ontology import projects, series
    from tests.conftest import TEST_URL
    from workbench.server import app

    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    monkeypatch.setattr(series, "TEMPLATES", Path(series.__file__).parent / "schema" / "bindings.yaml")
    with TestClient(app) as client:
        assert client.get("/api/projects").json() == []
        assert client.get("/api/projects/template").json()["bindings"] == []
        created = client.post("/api/projects", json={"name": "验证研究项目"})
        assert created.status_code == 200
        project = created.json()
        assert project["stats"]["nodes"] == 0
        assert project["stats"]["feeds"] == 0
        with projects.project_connect(project["id"], TEST_URL) as db:
            assert db.execute("SELECT count(*) AS n FROM schema_migrations").fetchone()["n"] == 9
            assert db.execute("SELECT count(*) AS n FROM observations").fetchone()["n"] == 0
            assert series.load_templates() == []
