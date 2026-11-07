import httpx
import pytest
from fastapi.testclient import TestClient

from ontology import mcp_server, projects
from ontology.errors import OntologyError
from ontology.store import OntologyStore
from tests.conftest import TEST_URL
from tests.fake_claude import FakeClaude
from workbench import server
from workbench.agent import WorkbenchAgent


@pytest.fixture
def conn(conn):
    projects.create(conn, "测试研究项目", project_id=projects.DEFAULT)
    return conn


def in_project(pid):
    return OntologyStore(projects.project_connect(pid, TEST_URL), actor="test-agent")


def test_projects_are_isolated_and_summarised(conn):
    p = projects.create(conn, "Macro Lab 宏观", "测试用")
    assert p["id"].startswith("macro-lab") and p["schema_name"].startswith(projects.PROJECT_SCHEMA_PREFIX)
    s = in_project(p["id"])
    assert s.registry.object_types  # seeded
    btc = s.upsert_node("Asset", "比特币", {"kind": "llm_prior"}, "x", props={"symbol": "BTC", "asset_class": "spot"})["node"]
    fed = s.upsert_node("Entity", "美联储", {"kind": "llm_prior"}, "x", props={"kind": "central_bank"})["node"]
    rate = s.upsert_node("MacroIndicator", "利率", {"kind": "llm_prior"}, "x")["node"]
    s.create_link("sets", fed["id"], rate["id"], {"kind": "llm_prior"}, "x")
    s.create_link("drives", rate["id"], btc["id"], {"kind": "llm_prior"}, "x")
    s.submit_changeset("seed")

    default = in_project(projects.DEFAULT)
    assert default.search_nodes("比特币") == []  # nothing leaks into the default project

    listed = {x["id"]: x for x in projects.list_all(conn)}
    assert set(listed) == {projects.DEFAULT, p["id"]}
    st = listed[p["id"]]["stats"]
    assert st["nodes"] == 3 and st["links"] == 2 and st["pending"] == 1
    sample = listed[p["id"]]["sample"]
    assert len(sample["nodes"]) == 3 and len(sample["links"]) == 2
    assert sample["nodes"][0][1] == 2  # highest degree first (利率)


def test_export_import_round_trip_and_atomic_failure(conn):
    p = projects.create(conn, "源", None)
    s, human = in_project(p["id"]), None
    a = s.upsert_node("Asset", "比特币", {"kind": "user"}, "x", aliases=["BTC"], props={"symbol": "BTC", "asset_class": "spot"})["node"]
    b = s.upsert_node("MacroIndicator", "联邦基金利率", {"kind": "llm_prior"}, "x")["node"]
    s.create_link("drives", b["id"], a["id"], {"kind": "llm_prior"}, "降息利好", confidence=0.6)
    human = OntologyStore(s.conn, actor="human")
    human.accept_changeset(s.submit_changeset("x")["id"])
    human.bind_direct(b["id"], "fred", {"series": "DFF"}, "value", "利率", unit="%")
    s.propose_type("object", "Thesis", "研究观点", "需要", layer="company")
    human.decide_type("object", "Thesis", accept=True)

    doc = projects.export(conn, p["id"])
    assert doc["format"] == projects.FORMAT and len(doc["nodes"]) == 2 and doc["bindings"][0]["params"] == {"series": "DFF"}
    assert [t["name"] for t in doc["types"]] == ["Thesis"]

    copy = projects.import_doc(conn, doc, name="副本")
    assert copy["name"] == "副本" and copy["stats"]["nodes"] == 2 and copy["stats"]["links"] == 1
    assert copy["stats"]["feeds"] == 1 and copy["stats"]["pending"] == 0  # imported content is accepted
    c = in_project(copy["id"])
    assert "Thesis" in c.registry.object_types
    assert c.search_nodes("btc")[0]["provenance"] == {"kind": "user"}

    tmpl = projects.import_doc(conn, projects.template())
    assert tmpl["stats"] == {**tmpl["stats"], "nodes": 0, "links": 0, "feeds": 0}

    bad = projects.template() | {"links": [{"type": "drives", "from": "rate", "to": "nowhere", "rationale": "x"}]}
    before = {x["id"] for x in projects.list_all(conn)}
    with pytest.raises(OntologyError, match="import failed"):
        projects.import_doc(conn, bad)
    assert {x["id"] for x in projects.list_all(conn)} == before  # nothing half-imported
    with pytest.raises(OntologyError, match="not a ontology-researchreport"):
        projects.import_doc(conn, {"nodes": []})


def test_edit_and_archive(conn):
    p = projects.create(conn, "临时", None)
    assert projects.update(conn, p["id"], name="正式", description="说明")["name"] == "正式"
    assert projects.update(conn, p["id"], status="archived")["status"] == "archived"
    assert p["id"] not in {x["id"] for x in projects.list_all(conn, include_archived=False)}
    with pytest.raises(OntologyError, match="already exists"):
        projects.create(conn, "x", project_id=p["id"])


@pytest.fixture
def client(conn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    monkeypatch.setattr(mcp_server, "_store", None)
    monkeypatch.setattr(mcp_server, "_stores", {})
    monkeypatch.setattr(server, "make_agent", lambda: WorkbenchAgent(client=FakeClaude()))
    with TestClient(server.app) as c:
        yield c


def test_pages_and_project_api(client):
    assert "本体项目" in client.get("/").text
    assert client.get(f"/p/{projects.DEFAULT}").status_code == 200
    assert client.get("/p/nope").status_code == 409
    made = client.post("/api/projects", json={"name": "研究台", "description": "d"}).json()
    assert made["stats"]["nodes"] == 0
    assert client.post("/api/projects", json={"name": " "}).status_code == 409
    assert client.patch(f"/api/projects/{made['id']}", json={"name": "研究台 2"}).json()["name"] == "研究台 2"
    tmpl = client.get("/api/projects/template")
    assert "attachment" in tmpl.headers["content-disposition"]
    imported = client.post("/api/projects/import", json=tmpl.json()).json()
    assert imported["stats"]["nodes"] == 0
    exported = client.get(f"/api/projects/{imported['id']}/export").json()
    assert exported["nodes"] == exported["bindings"] == []
    assert client.post("/api/projects/import", content=b"not json").status_code == 400
    info = client.get("/api/mcp").json()
    assert {"propose_binding", "search_news", "get_link_evidence"} <= {t["name"] for t in info["tools"]}
    ids = [p["id"] for p in client.get("/api/projects").json()]
    assert set(ids) == {projects.DEFAULT, made["id"], imported["id"]}


def test_chat_writes_into_the_pages_project(client):
    pid = client.post("/api/projects", json={"name": "沙盒"}).json()["id"]
    with client.websocket_connect(f"/ws?project={pid}") as ws:
        first = ws.receive_json()
        assert first["project"]["id"] == pid and first["graph"]["nodes"] == []
        ws.send_json({"type": "chat", "text": "帮我搭一个 BTC 概念图"})
        while True:
            msg = ws.receive_json()
            if msg["type"] == "status" and msg["busy"] is False:
                break
    assert len(client.get(f"/api/graph?project={pid}").json()["graph"]["nodes"]) == 12
    assert client.get("/api/graph").json()["graph"]["nodes"] == []  # the default project is untouched
    assert mcp_server.current_project.get() is None


def test_refresh_fetches_now_and_pushes_the_graph(client, monkeypatch):
    from ontology import sources
    from tests.test_data import FakeSource

    fake = FakeSource()
    monkeypatch.setitem(sources.SOURCES, "fake", fake)
    s = in_project(projects.DEFAULT)
    btc = s.upsert_node("Asset", "比特币", {"kind": "user"}, "x", props={"symbol": "BTC"})["node"]  # no asset_class: keeps the live Binance template out
    human = OntologyStore(s.conn, actor="human")
    human.accept_changeset(s.submit_changeset("x")["id"])
    human.bind_direct(btc["id"], "fake", {"symbol": "BTC"}, "price", "价格")

    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "graph"
        assert ws.receive_json()["type"] == "session"
        r = client.post("/api/refresh").json()
        assert r["series"] == 1 and r["series_updated"] == 1 and r["points"] > 40 and r["errors"] == []
        pushed = ws.receive_json()
        assert pushed["type"] == "graph" and pushed["graph"]["data"][btc["id"]][0]["latest"]
    # Not due by its poll interval, but the button fetches anyway.
    assert client.post("/api/refresh").status_code == 200
    assert len(fake.calls) == 2


def drain_until_idle(ws):
    out = []
    while True:
        m = ws.receive_json()
        out.append(m)
        if m["type"] == "status" and m["busy"] is False:
            return out


def test_conversation_survives_a_reload_and_can_be_switched(client):
    from workbench import sessions

    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "graph"
        first = ws.receive_json()
        assert first["type"] == "session" and first["events"] == []
        ws.send_json({"type": "chat", "text": "帮我搭一个 BTC 概念图"})
        drain_until_idle(ws)
    sid = first["session"]["id"]

    with client.websocket_connect("/ws") as ws:  # a reload
        ws.receive_json()
        back = ws.receive_json()
        assert back["session"]["id"] == sid and back["session"]["title"] == "帮我搭一个 BTC 概念图"
        kinds = [e["type"] for e in back["events"]]
        assert kinds[0] == "user" and "tool" in kinds and kinds[-1] == "assistant"
        assert [s["id"] for s in back["sessions"]] == [sid]
        with projects.project_connect(projects.DEFAULT, TEST_URL) as c:
            history = sessions.load_messages(c, sid)
        assert history[0] == {"role": "user", "content": "帮我搭一个 BTC 概念图"}
        assert any(b.get("type") == "tool_use" for m in history if isinstance(m["content"], list) for b in m["content"])

        ws.send_json({"type": "new_session"})
        fresh = ws.receive_json()
        assert fresh["session"]["id"] != sid and fresh["events"] == []
        ws.send_json({"type": "open_session", "id": sid})
        assert ws.receive_json()["session"]["id"] == sid


def test_model_choice_is_per_conversation_and_used(client, monkeypatch):
    made = []

    def factory():
        a = WorkbenchAgent(client=FakeClaude())
        made.append(a)
        return a

    monkeypatch.setattr(server, "make_agent", factory)
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        assert ws.receive_json()["session"]["model"] is None
        ws.send_json({"type": "set_model", "model": "codex/gpt-6-sol"})
        assert ws.receive_json() == {"type": "model", "model": "codex/gpt-6-sol"}
        ws.send_json({"type": "chat", "text": "帮我搭一个 BTC 概念图"})
        drain_until_idle(ws)
    assert made[-1].model == "codex/gpt-6-sol"
    with client.websocket_connect("/ws") as ws:  # the choice is remembered with the conversation
        ws.receive_json()
        assert ws.receive_json()["session"]["model"] == "codex/gpt-6-sol"


def test_models_endpoint_reads_the_model_list(client, monkeypatch):
    original_client = httpx.Client

    def provider(request):
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [
            {"id": "codex/gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "owned_by": "codex", "context_window": 500000},
            {"id": "claude/claude-opus-5-5", "display_name": "Claude Opus 5.5", "owned_by": "claude"},
        ]})

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-catalog-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://models.example")
    monkeypatch.setattr(server.model_settings, "_stored", lambda user_id: {})
    monkeypatch.setattr(httpx, "Client",
                        lambda **kwargs: original_client(transport=httpx.MockTransport(provider), **kwargs))
    data = client.get("/api/models").json()
    ids = [m["id"] for m in data["models"]]
    assert "codex/gpt-6.1-sol" in ids and "claude/claude-opus-5-5" in ids and data["default"] in ids


def test_files_exported_before_the_rename_still_import(conn):
    old = projects.template() | {"format": "btc-ontology/1"}
    assert projects.import_doc(conn, old)["stats"]["nodes"] == 0


def test_sources_page_lists_providers_with_status(client, monkeypatch):
    monkeypatch.delenv("THS_API_KEY", raising=False)
    data = client.get("/api/sources").json()
    by_id = {p["id"]: p for p in data["providers"]}
    assert by_id["ths"]["configured"] is False and by_id["ths"]["auth_env"] == "THS_API_KEY"
    assert {s["source"] for s in by_id["ths"]["sources"]} == {"ths_stock_daily", "ths_index_daily", "ths_valuation", "ths_fin_indicator"}
    assert by_id["binance"]["configured"] is True and by_id["binance"]["health"]["series"] == 0
    r = client.post("/api/sources/ths/test").json()
    assert r["ok"] is False and "THS_API_KEY" in r["message"]
    assert client.get("/sources").status_code == 200


def test_report_api_regenerates_and_pushes(client, monkeypatch):
    from tests.test_reports import FakeModel

    s = in_project(projects.DEFAULT)
    n = s.upsert_node("Asset", "比特币", {"kind": "user"}, "x", props={"symbol": "BTC"})["node"]
    OntologyStore(s.conn, actor="human").accept_changeset(s.submit_changeset("x")["id"])
    fake = FakeModel(lambda kw: {"title": "T", "summary": "S", "sections": [{"heading": "H", "sentences": [
        {"id": "s1", "text": "比特币。", "nodes": [n["id"]]}]}]})
    monkeypatch.setattr(server, "report_client", fake)
    first = client.get("/api/report").json()
    assert first["report"] is None and first["state"]["stale"] is True
    with client.websocket_connect("/ws") as ws:
        ws.receive_json(); ws.receive_json()
        r = client.post("/api/report/regenerate").json()
        assert r["status"] == "ready" and r["body"]["sections"][0]["sentences"][0]["nodes"] == [n["id"]]
        kinds = [ws.receive_json()["type"] for _ in range(3)]
        assert "report" in kinds
    now = client.get("/api/report").json()
    assert now["report"]["id"] == r["id"] and now["state"]["stale"] is False


def test_history_records_decisions_and_serves_past_graph(client):
    s = in_project(projects.DEFAULT)
    s.upsert_node("Asset", "比特币", {"kind": "user"}, "x", props={"symbol": "BTC"})
    cs = s.submit_changeset("加比特币")["id"]
    assert client.post(f"/api/changesets/{cs}/accept").status_code == 200
    h = client.get("/api/history").json()
    assert h["commits"] and "加比特币" in h["commits"][-1]["message"]
    first = h["commits"][-1]["id"]

    s.upsert_node("MacroIndicator", "利率", {"kind": "user"}, "x")
    cs2 = s.submit_changeset("加利率")["id"]
    client.post(f"/api/changesets/{cs2}/accept")
    assert len(client.get("/api/history").json()["commits"]) == len(h["commits"]) + 1

    past = client.get(f"/api/graph?at={first}").json()
    assert past["commit"]["id"] == first
    assert {n["name"] for n in past["graph"]["nodes"]} == {"比特币"}
    assert len(client.get("/api/graph").json()["graph"]["nodes"]) == 2
    assert client.get("/api/graph?at=nope").status_code in (400, 404)


def test_source_key_is_saved_to_env_file_and_masked(client, monkeypatch, tmp_path):
    from ontology import providers

    env = tmp_path / ".env"
    env.write_text("DATABASE_URL=x\nTHS_API_KEY=old\n", encoding="utf-8")
    monkeypatch.setattr(providers, "ENV_FILE", env)
    monkeypatch.delenv("THS_API_KEY", raising=False)

    r = client.put("/api/sources/ths/key", json={"key": " sk-test-abcd1234 "})
    assert r.status_code == 200 and r.json()["configured"] and r.json()["key_hint"] == "••••1234"
    assert "sk-test" not in r.text
    assert env.read_text(encoding="utf-8") == "DATABASE_URL=x\nTHS_API_KEY=sk-test-abcd1234\n"
    assert (env.stat().st_mode & 0o777) == 0o600

    assert client.put("/api/sources/ths/key", json={"key": "has space"}).status_code == 400
    assert client.put("/api/sources/binance/key", json={"key": "x"}).status_code == 400
    assert not client.put("/api/sources/ths/key", json={"key": ""}).json()["configured"]
    assert "THS_API_KEY" not in env.read_text(encoding="utf-8")


def test_mcp_client_switches_projects_but_the_workbench_cannot(conn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    monkeypatch.setattr(mcp_server, "_store", None)
    monkeypatch.setattr(mcp_server, "_stores", {})
    monkeypatch.setattr(mcp_server, "_selected", None)
    monkeypatch.delenv("ONTOLOGY_PROJECT", raising=False)
    p = projects.create(conn, "半导体")
    listed = mcp_server.list_projects()
    assert listed["current"] == projects.DEFAULT and p["id"] in {x["id"] for x in listed["projects"]}

    assert mcp_server.use_project(p["id"])["current"] == p["id"]
    mcp_server.upsert_node("Concept", "国产替代", {"kind": "user"}, "x", props={"category": "economics"})
    assert in_project(p["id"]).search_nodes("国产替代")
    assert not in_project(projects.DEFAULT).search_nodes("国产替代")

    token = mcp_server.current_project.set(projects.DEFAULT)
    try:
        with pytest.raises(Exception, match="固定在项目"):
            mcp_server.use_project(p["id"])
    finally:
        mcp_server.current_project.reset(token)


def test_env_file_fills_but_never_overrides(tmp_path, monkeypatch):
    from ontology.providers import load_env_file

    f = tmp_path / ".env"
    f.write_text("# comment\nA_ONE=1\nexport A_TWO='two words'\nA_THREE=from-file\n", encoding="utf-8")
    monkeypatch.delenv("A_ONE", raising=False)
    monkeypatch.delenv("A_TWO", raising=False)
    monkeypatch.setenv("A_THREE", "from-env")
    load_env_file(f)
    import os
    assert (os.environ["A_ONE"], os.environ["A_TWO"], os.environ["A_THREE"]) == ("1", "two words", "from-env")
    monkeypatch.delenv("A_ONE"); monkeypatch.delenv("A_TWO")
