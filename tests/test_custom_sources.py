import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ontology import collector, custom_sources, news, projects, sources
from ontology.custom_sources import parse_csv, parse_json
from ontology.errors import OntologyError
from tests.conftest import node
from tests.test_projects import client  # noqa: F401  (the workbench app with a fake model)

DAY = 86400


@pytest.fixture(autouse=True)
def _forget_custom_sources():
    yield
    for name in list(custom_sources._loaded):
        sources.SOURCES.pop(name, None)
    custom_sources._loaded.clear()
    news.CUSTOM_FEEDS.clear()


@pytest.fixture
def reg(conn):
    projects.upgrade_registry(conn)
    return conn


@pytest.fixture
def web(monkeypatch):
    """Answers for sources._get, by URL; records the URLs asked for."""
    pages: dict[str, str] = {}
    asked: list[str] = []

    def get(url, **_kw):
        asked.append(url)
        if url not in pages:
            raise OntologyError(f"{url}: HTTP 404")
        return SimpleNamespace(text=pages[url])

    monkeypatch.setattr(sources, "_get", get)
    return SimpleNamespace(pages=pages, asked=asked)


def recent(days_ago: int) -> int:
    return int((datetime.now(UTC) - timedelta(days=days_ago)).timestamp())


QUOTES = {"name": "quotes", "kind": "json", "title": "示例行情", "url": "https://data.example/q/{symbol}.json",
          "config": {"path": "data.rows", "time": "t", "value": "close", "frequency": "1d", "unit": "元",
                     "params": {"symbol": "股票代码"}}}


def test_definitions_are_checked():
    ok = custom_sources.clean(QUOTES)
    assert ok["config"]["params"] == {"symbol": "股票代码"} and ok["config"]["poll_minutes"] == 60
    assert custom_sources.url_params("https://x.org/{a}/{b}?k={a}") == ["a", "b"]
    for bad, msg in [
        ({**QUOTES, "name": "Quotes"}, "小写字母"),
        ({**QUOTES, "name": "fred"}, "内置"),
        ({**QUOTES, "url": "ftp://data.example/q"}, "http"),
        ({**QUOTES, "url": "https://user:pw@data.example/q"}, "用户名"),
        ({**QUOTES, "url": "https://data.example/{Symbol}"}, "参数名"),
        ({**QUOTES, "kind": "xml"}, "类型"),
        ({**QUOTES, "config": {**QUOTES["config"], "frequency": "2d"}}, "频率"),
        ({**QUOTES, "kind": "csv", "config": {**QUOTES["config"], "time": ""}}, "时间字段"),  # json/mcp may omit it
        ({**QUOTES, "kind": "mcp", "config": {"tool": "quote", "value": "price"}}, "MCP 服务"),
        ({**QUOTES, "kind": "mcp", "config": {"server": "market", "tool": "quote", "value": "p",
                                              "arguments": "{not json"}}, "JSON 对象"),
        ({"name": "feed", "kind": "rss", "title": "x", "url": "https://x.org/{a}.xml"}, "RSS"),
        ({"name": "coindesk", "kind": "rss", "title": "x", "url": "https://x.org/rss"}, "内置"),
    ]:
        with pytest.raises(OntologyError, match=msg):
            custom_sources.clean(bad)


def test_json_shapes_and_time_formats():
    cfg = {"path": "data.rows", "time": "t", "value": "close", "time_format": "auto"}
    pts = parse_json(json.dumps({"data": {"rows": [
        {"t": 1793664000000, "close": 3.5}, {"t": "2026-11-01", "close": "1,234.5"}, {"t": 1793577600, "close": 2},
        {"t": None, "close": 9}, {"close": 9}]}}), cfg)
    assert [(p.ts.day, p.value) for p in pts] == [(1, 1234.5), (2, 2.0), (3, 3.5)]  # sorted; the two bad rows skipped
    assert all(p.ts.tzinfo for p in pts)

    pairs = parse_json("[[1793577600000, 10], [1793664000000, 11]]", {"time": "0", "value": "1", "time_format": "unix_ms"})
    assert [p.value for p in pairs] == [10.0, 11.0]
    mapping = parse_json('{"2026-10-01": 5, "2026-11-01": 6}', {"time": "0", "value": "1"})
    assert [p.value for p in mapping] == [5.0, 6.0]

    with pytest.raises(OntologyError, match="找不到路径"):
        parse_json('{"data": {}}', cfg)
    with pytest.raises(OntologyError, match="没有一行"):
        parse_json('{"data": {"rows": [{"t": "soon", "close": 1}]}}', cfg)
    with pytest.raises(OntologyError, match="不是 JSON"):
        parse_json("<html>", cfg)


def test_csv_columns():
    text = "﻿date,cpi,note\n2026-09-01,101.2,\n2026-10-01,,revised\n2026-10-15,abc,\n"
    pts = parse_csv(text, {"time": "date", "value": "cpi"})
    assert [(p.ts.month, p.value) for p in pts] == [(9, 101.2)]
    with pytest.raises(OntologyError, match="它的列是：date, cpi, note"):
        parse_csv(text, {"time": "Date", "value": "cpi"})


def test_a_json_source_is_added_bound_collected_edited_and_removed(reg, store, human, web):
    made = custom_sources.create(reg, QUOTES)
    assert made["params"] == ["symbol"] and "quotes" in sources.SOURCES
    assert sources.SOURCES["quotes"].describe()["custom"] is True
    with pytest.raises(OntologyError, match="已经有"):
        custom_sources.create(reg, QUOTES)

    web.pages["https://data.example/q/600519.SH.json"] = json.dumps({"data": {"rows": [
        {"t": recent(2), "close": 1500}, {"t": recent(1), "close": 1510}]}})
    stock = node(store, "Asset", "贵州茅台", props={"symbol": "600519.SH", "asset_class": "equity"})
    store.propose_binding(stock["id"], "quotes", {"symbol": "600519.SH"}, "price", "收盘价")
    human.accept_changeset(store.submit_changeset("node + binding")["id"])
    collector.run_once(store.conn)
    shown = store.list_bindings(stock["id"])[0]
    assert shown["latest"]["value"] == 1510 and shown["label"] == "示例行情 · 600519.SH" and shown["unit"] == "元"

    edited = custom_sources.update(reg, "quotes", {**QUOTES, "title": "茅台行情", "kind": "rss",
                                                   "url": "https://mirror.example/q/{symbol}.json"})
    assert edited["kind"] == "json" and edited["title"] == "茅台行情"  # the kind cannot change
    assert sources.SOURCES["quotes"].address({"symbol": "a b"}) == "https://mirror.example/q/a%20b.json"
    with pytest.raises(OntologyError, match="不能增删"):
        custom_sources.update(reg, "quotes", {**QUOTES, "url": "https://data.example/q/{code}.json"})

    custom_sources.delete(reg, "quotes")
    assert "quotes" not in sources.SOURCES and custom_sources.list_all(reg) == []
    assert store.series.collectable() == [] and store.list_bindings(stock["id"])[0]["latest"]["value"] == 1510
    with pytest.raises(OntologyError, match="no custom source"):
        custom_sources.delete(reg, "quotes")


def test_a_custom_rss_feed_joins_the_news_desk(reg, store, web):
    custom_sources.create(reg, {"name": "my_feed", "kind": "rss", "title": "我的新闻", "url": "https://news.example/rss",
                                "config": {"lang": "en"}})
    assert news.CUSTOM_FEEDS == [("my_feed", "https://news.example/rss", "en")]
    assert ("my_feed", "https://news.example/rss", "en") in news.all_feeds()
    custom_sources.delete(reg, "my_feed")
    assert news.CUSTOM_FEEDS == []


def test_sync_never_shadows_a_built_in(reg):
    reg.execute("""INSERT INTO onto_research.custom_sources (name, kind, title, url, config)
                   VALUES ('fred', 'json', 'x', 'https://x.org', '{"time": "t", "value": "v"}')""")
    builtin = sources.SOURCES["fred"]
    custom_sources.sync(reg)
    assert sources.SOURCES["fred"] is builtin


def test_trying_a_draft_stores_nothing(reg, web):
    web.pages["https://data.example/cpi.csv"] = "date,cpi\n" + "\n".join(
        f"{(datetime.now(UTC) - timedelta(days=d)).date()},{100 + d}" for d in (40, 10))
    out = custom_sources.test({"kind": "csv", "title": "CPI", "url": "https://data.example/cpi.csv",
                               "config": {"time": "date", "value": "cpi", "backfill_days": 30}})
    assert out["count"] == 1 and out["last"]["value"] == 110.0
    assert custom_sources.list_all(reg) == []
    with pytest.raises(OntologyError, match="needs params"):
        custom_sources.test(QUOTES)


def test_the_page_api_manages_custom_sources(client, web):
    r = client.post("/api/sources/custom", json=QUOTES)
    assert r.status_code == 200, r.text
    page = client.get("/api/sources").json()
    assert page["can_manage"] is True
    assert [c["name"] for c in page["custom"]] == ["quotes"] and page["custom"][0]["health"]["series"] == 0
    assert "custom" not in {p["id"] for p in page["providers"]}

    assert client.post("/api/sources/custom", json={**QUOTES, "name": "Bad"}).status_code == 400
    r = client.put("/api/sources/custom/quotes", json={**QUOTES, "title": "新名字"})
    assert r.status_code == 200 and r.json()["title"] == "新名字"
    assert client.put("/api/sources/custom/nope", json=QUOTES).status_code == 404

    web.pages["https://data.example/q/X.json"] = json.dumps({"data": {"rows": [{"t": recent(1), "close": 7}]}})
    tried = client.post("/api/sources/custom/test", json={"definition": QUOTES, "params": {"symbol": "X"}}).json()
    assert tried["ok"] and tried["count"] == 1 and tried["address"] == "https://data.example/q/X.json"
    failed = client.post("/api/sources/custom/test", json={"definition": QUOTES, "params": {"symbol": "Y"}}).json()
    assert not failed["ok"] and "404" in failed["message"]

    assert client.delete("/api/sources/custom/quotes").json() == {"deleted": "quotes"}
    assert client.get("/api/sources").json()["custom"] == []


def test_only_the_local_workbench_manages_custom_sources():
    from fastapi import HTTPException

    from workbench import server

    local = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    server._manage_sources(local)
    with pytest.raises(HTTPException):
        server._manage_sources(SimpleNamespace(client=SimpleNamespace(host="10.0.0.8")))
    token = projects.workspace_owner.set("user-1")
    try:
        with pytest.raises(HTTPException):
            server._manage_sources(local)
    finally:
        projects.workspace_owner.reset(token)
