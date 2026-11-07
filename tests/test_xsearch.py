import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ontology import mcp_server, xsearch
from ontology.errors import OntologyError

NOW = datetime(2026, 10, 20, 16, 30, tzinfo=UTC)


def status_id(ts: datetime) -> str:
    """The snowflake id X would give a post created at ts."""
    return str((int(ts.timestamp() * 1000) - xsearch.TWITTER_EPOCH_MS) << 22)


def response(items_json: str, searched=True, cited=()):
    out = [{"type": "custom_tool_call", "name": "x_search"}] if searched else []
    out.append({"type": "message", "content": [{"type": "output_text", "text": items_json,
                                                 "annotations": [{"url": u} for u in cited]}]})
    return {"output": out}


def test_snowflake_decodes_creation_time():
    assert xsearch.created_at("2112582055317946624").strftime("%Y-%m-%d %H:%M") == "2026-10-20 16:29"


def test_parse_keeps_only_posts_whose_ids_are_real_and_recent():
    good = status_id(NOW - timedelta(minutes=12))
    future = status_id(NOW + timedelta(hours=3))
    ancient = status_id(NOW - timedelta(days=40))
    text = f"""Here you go:
    [{{"handle": "@a", "author": "A", "text": "BTC breaks 83k", "url": "https://twitter.com/a/status/{good}", "likes": 5}},
     {{"handle": "@b", "text": "made up", "url": "https://x.com/b/status/{future}"}},
     {{"handle": "@c", "text": "old", "url": "https://x.com/c/status/{ancient}"}},
     {{"handle": "@d", "text": "no link", "url": "https://example.com"}},
     {{"handle": "@a", "text": "dup", "url": "https://x.com/a/status/{good}"}}]"""
    posts = xsearch.parse_posts(response(text, cited=[f"https://x.com/a/status/{good}"]), hours=24, now=NOW)
    assert [p["handle"] for p in posts] == ["@a"]
    assert posts[0]["url"] == f"https://x.com/a/status/{good}" and posts[0]["cited"] and posts[0]["likes"] == 5


class FakeClient:
    def __init__(self, payload, status=200):
        self.payload, self.status, self.sent = payload, status, None

    async def post(self, url, json, headers):
        self.sent = (url, json)
        return httpx.Response(self.status, json=self.payload)


def test_search_requires_an_actual_tool_call(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:3425")
    pid = status_id(datetime.now(UTC) - timedelta(minutes=5))
    items = f'[{{"handle": "@a", "text": "BTC", "url": "https://x.com/a/status/{pid}"}}]'
    ok = FakeClient(response(items))
    r = asyncio.run(xsearch.search("bitcoin", client=ok))
    assert r["searched"] and len(r["posts"]) == 1
    assert ok.sent[0].endswith("/v1/responses") and ok.sent[1]["tools"] == [{"type": "x_search"}]
    lazy = asyncio.run(xsearch.search("bitcoin", client=FakeClient(response(items, searched=False))))
    assert not lazy["searched"] and lazy["posts"] == [] and lazy["note"]
    with pytest.raises(OntologyError, match="HTTP 502"):
        asyncio.run(xsearch.search("bitcoin", client=FakeClient({}, status=502)))


def test_tool_stores_found_posts_for_the_news_desk(store, monkeypatch):
    pid = status_id(datetime.now(UTC) - timedelta(minutes=5))

    async def fake_search(query, hours, limit):
        return {"query": query, "searched": True, "posts": [{"handle": "@a", "author": "A", "text": "BTC ETF inflows",
                "url": f"https://x.com/a/status/{pid}", "posted_at": xsearch.created_at(pid).isoformat(), "likes": 1, "cited": True}]}

    monkeypatch.setattr(mcp_server, "_store", store)
    monkeypatch.setattr(xsearch, "search", fake_search)
    r = asyncio.run(mcp_server.server.call_tool("search_x", {"query": "BTC ETF"}))
    assert not r.is_error
    doc = store.conn.execute("SELECT feed, url, relevant FROM documents").fetchone()
    assert doc["feed"] == "x" and doc["url"].endswith(pid) and doc["relevant"] is None  # screened like any article
