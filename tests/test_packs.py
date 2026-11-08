"""Type packs: new projects are research projects; market projects keep their types and can add research ones."""

import pytest
from fastapi.testclient import TestClient

from ontology import mcp_server, projects
from ontology.errors import OntologyError
from ontology.registry import build_registry, load_seed, merge_types, parse_type
from ontology.store import OntologyStore
from tests.conftest import TEST_URL
from workbench import server

LLM = {"kind": "llm_prior"}


def in_project(pid):
    return OntologyStore(projects.project_connect(pid, TEST_URL), actor="test-agent")


def test_both_packs_load_and_merge_into_a_valid_registry():
    research = build_registry(load_seed("research"))
    assert {"Question", "Hypothesis", "Claim", "Observation", "Concept", "Method"} <= set(research.object_types)
    assert research.object_types["Claim"].layer == "reasoning" and research.object_types["Method"].layer == "subject"
    assert "Asset" not in research.object_types
    both = build_registry(load_seed(["market", "research"]))
    assert {"Asset", "Signal", "Hypothesis"} <= set(both.object_types)
    with pytest.raises(OntologyError, match="unknown type pack"):
        load_seed("nope")


def test_merge_keeps_everything_either_definition_accepts():
    a = parse_type("object", "Thing", {"layer": "world", "description": "a", "properties": {
        "kind": {"type": "enum", "values": ["x", "y"], "required": True},
        "only_a": {"type": "string", "required": True},
        "clash": {"type": "number"}}})
    b = parse_type("object", "Thing", {"layer": "subject", "description": "b", "properties": {
        "kind": {"type": "enum", "values": ["y", "z"], "required": True},
        "clash": {"type": "string"}}})
    m = merge_types(a, b)
    assert m.layer == "world" and m.description == "a"
    assert m.properties["kind"].values == ["x", "y", "z"] and m.properties["kind"].required
    assert not m.properties["only_a"].required  # b's nodes never had it
    assert m.properties["clash"].type == "number"  # first definition wins on a type clash
    m.validate_props({"kind": "z"})
    la = parse_type("link", "near", {"description": "", "from": ["A"], "to": ["B"]})
    lb = parse_type("link", "near", {"description": "", "from": ["C"], "to": ["*"]})
    ml = merge_types(la, lb)
    assert ml.from_types == ["A", "C"] and ml.to_types == ["*"]


def test_new_projects_default_to_research_and_build_a_reasoning_graph(conn):
    p = projects.create(conn, "睡眠研究")
    assert p["packs"] == ["research"]
    s = in_project(p["id"])
    assert s.project_packs() == ["research"] and "Asset" not in s.registry.object_types
    q = s.upsert_node("Question", "社交媒体使用是否影响青少年睡眠", LLM, "x", props={"status": "open"})["node"]
    h = s.upsert_node("Hypothesis", "睡前使用时长与入睡时间正相关", LLM, "x", props={"status": "proposed"})["node"]
    o = s.upsert_node("Observation", "样本中 r=0.32", LLM, "x", props={"sample": "n=1200 初中生"})["node"]
    c = s.upsert_node("Concept", "睡眠质量", LLM, "x", props={"category": "construct"})["node"]
    m = s.upsert_node("Method", "PSQI 量表", LLM, "x", props={"category": "instrument"})["node"]
    s.create_link("addresses", h["id"], q["id"], LLM, "x")
    s.create_link("supports", o["id"], h["id"], LLM, "x", props={"strength": 0.4})
    s.create_link("about", q["id"], c["id"], LLM, "x")
    s.create_link("measures", m["id"], c["id"], LLM, "x")
    with pytest.raises(OntologyError, match="requires properties"):
        s.upsert_node("Claim", "某断言", LLM, "x")  # origin is required
    with pytest.raises(OntologyError):
        s.create_link("supports", q["id"], h["id"], LLM, "x")  # a question supports nothing
    assert s.validate_graph()["violations"] == []


def test_market_projects_add_research_types_without_breaking_nodes(conn):
    p = projects.create(conn, "BTC", pack="market")
    s = in_project(p["id"])
    proto = s.upsert_node("Concept", "工作量证明", LLM, "x", props={"category": "protocol"})["node"]
    s.upsert_node("Entity", "美联储", LLM, "x", props={"kind": "central_bank"})
    s.upsert_node("Event", "减半", LLM, "x", props={"category": "halving"})

    result = projects.add_pack(conn, p["id"], "research")
    assert result["packs"] == ["market", "research"]
    assert "object:Hypothesis" in result["added"] and "object:Concept" in result["merged"]
    assert projects.get(conn, p["id"])["packs"] == ["market", "research"]
    with pytest.raises(OntologyError, match="already uses"):
        projects.add_pack(conn, p["id"], "research")

    s = in_project(p["id"])
    assert s.validate_graph()["violations"] == []
    assert {"Asset", "Hypothesis"} <= set(s.registry.object_types)
    h = s.upsert_node("Hypothesis", "工作量证明保障安全", LLM, "x")["node"]
    s.create_link("about", h["id"], proto["id"], LLM, "x")
    s.upsert_node("Concept", "社会资本", LLM, "x", props={"category": "theory"})  # research value on a market type
    s.sync_seed_types()  # a later re-sync (cli init) reads both packs and keeps the merged definitions
    assert s.validate_graph()["violations"] == []


def test_exports_carry_packs_and_old_files_import_as_market(conn):
    p = projects.create(conn, "研究", pack="research")
    doc = projects.export(conn, p["id"])
    assert doc["project"]["packs"] == ["research"]
    assert projects.template()["project"]["packs"] == ["research"]
    again = projects.import_doc(conn, doc, "副本")
    assert again["packs"] == ["research"]

    old = {"format": projects.FORMAT, "project": {"name": "旧"}, "types": [], "links": [], "bindings": [],
           "nodes": [{"ref": "a", "type": "Asset", "name": "比特币", "props": {"symbol": "BTC", "asset_class": "spot"}}]}
    imported = projects.import_doc(conn, old)
    assert imported["packs"] == ["market"] and imported["stats"]["nodes"] == 1

    both = projects.import_doc(conn, {**old, "project": {"name": "混合", "packs": ["market", "research"]}})
    assert both["packs"] == ["market", "research"]
    with pytest.raises(OntologyError, match="known type packs"):
        projects.import_doc(conn, {**old, "project": {"name": "坏", "packs": ["nope"]}})


def test_an_older_registry_is_upgraded_and_its_projects_stay_market(conn):
    p = projects.create(conn, "旧项目", pack="market")
    conn.execute("ALTER TABLE onto_research.projects DROP COLUMN packs")  # as a registry from before type packs
    conn.execute("DELETE FROM onto_research.schema_migrations WHERE version = '002_project_packs'")
    assert projects.upgrade_registry(conn) == ["002_project_packs"]  # what startup (upgrade_all) applies
    assert projects.get(conn, p["id"])["packs"] == ["market"]
    assert projects.create(conn, "新项目")["packs"] == ["research"]


def test_pack_api(conn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    monkeypatch.setattr(mcp_server, "_store", None)
    monkeypatch.setattr(mcp_server, "_stores", {})
    with TestClient(server.app) as client:
        assert {p["id"]: p["default"] for p in client.get("/api/packs").json()} == {"research": True, "market": False}
        made = client.post("/api/projects", json={"name": "研究台"}).json()
        assert made["packs"] == ["research"]
        market = client.post("/api/projects", json={"name": "行情", "pack": "market"}).json()
        assert client.post("/api/projects", json={"name": "x", "pack": "nope"}).status_code == 409
        added = client.post(f"/api/projects/{market['id']}/packs/research").json()
        assert added["packs"] == ["market", "research"]
        assert client.post(f"/api/projects/{market['id']}/packs/research").status_code == 409
        snap = client.get(f"/api/graph?project={market['id']}").json()
        assert snap["project"]["packs"] == ["market", "research"]  # the page hides the news desk outside market
        assert snap["proposed_types"] == []


def test_schema_carries_pack_guidance_and_reports_follow_the_pack(conn):
    from ontology import reports
    from tests.test_reports import FakeModel

    p = projects.create(conn, "综述")
    s = in_project(p["id"])
    schema = s.get_schema()
    assert schema["packs"] == ["research"] and "research project" in schema["guidance"]
    assert "search_x" not in schema["guidance"]
    market = in_project(projects.create(conn, "行情", pack="market")["id"]).get_schema()
    assert "search_x" in market["guidance"]

    q = s.upsert_node("Question", "问题", LLM, "x")["node"]
    h = s.upsert_node("Hypothesis", "假设", LLM, "x")["node"]
    s.create_link("addresses", h["id"], q["id"], LLM, "x")
    OntologyStore(s.conn, actor="human").accept_changeset(s.submit_changeset("x")["id"])
    fake = FakeModel(lambda kw: {"title": "进展", "summary": "s", "sections": [
        {"heading": "问题", "sentences": [{"id": "s1", "text": "有一个假设。", "nodes": [h["id"]]}]}]})
    reports.write(s, client=fake)
    assert fake.calls[0]["system"].startswith(reports.STYLES["research"])
