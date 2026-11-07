"""Search X (Twitter) through Grok's server-side x_search tool, routed by magpie.

Only the Responses endpoint with the x_search tool actually searches X; a plain chat request to Grok answers
from memory. Every post is checked against its status id: X ids encode their creation time (snowflake), so a
link whose id does not decode to a sane time is dropped rather than shown. Posts found are stored as documents,
so the news pipeline maps them to nodes and counts them toward heat like any other article.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg

from .errors import OntologyError
from .news import store_documents

MODEL = os.environ.get("X_SEARCH_MODEL", "grok/grok-4.7")
STATUS = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/(\w{1,30})/status/(\d{15,20})")
TWITTER_EPOCH_MS = 1288834974657

PROMPT = """Use the x_search tool to find posts on X matching: {query}
Only posts from the last {hours} hours. Up to {limit} posts, most relevant and most recent first; skip spam and pure ads.
Answer with a JSON array only, no prose, each item:
{{"handle": "@user", "author": "display name", "text": "the post text (translate nothing)", "url": "https://x.com/user/status/<id>", "likes": <int or null>}}
Only include posts you actually found with the tool, with their real URLs."""


def created_at(status_id: str) -> datetime:
    return datetime.fromtimestamp(((int(status_id) >> 22) + TWITTER_EPOCH_MS) / 1000, UTC)


def _message_text(resp: dict[str, Any]) -> tuple[str, set[str]]:
    text, cited = [], set()
    for item in resp.get("output", []):
        if item.get("type") != "message":
            continue
        for c in item.get("content", []):
            text.append(c.get("text", ""))
            for a in c.get("annotations", []) or []:
                if a.get("url"):
                    cited.add(a["url"])
    for u in resp.get("citations", []) or []:
        cited.add(u if isinstance(u, str) else u.get("url", ""))
    return "".join(text), cited


def parse_posts(resp: dict[str, Any], hours: int, now: datetime | None = None) -> list[dict[str, Any]]:
    """Posts from a Responses API result, each verified against the time its status id encodes."""
    now = now or datetime.now(UTC)
    text, cited = _message_text(resp)
    m = re.search(r"\[.*\]", text, re.S)
    try:
        items = json.loads(m.group(0)) if m else []
    except json.JSONDecodeError:
        items = []
    posts, seen = [], set()
    for it in items if isinstance(items, list) else []:
        sm = STATUS.search(str(it.get("url", "")))
        if not sm or sm.group(2) in seen:
            continue
        ts = created_at(sm.group(2))
        # A real id decodes to a past moment inside the window (with slack); anything else is not a found post.
        if ts > now + timedelta(minutes=5) or ts < now - timedelta(hours=hours, days=1):
            continue
        seen.add(sm.group(2))
        url = f"https://x.com/{sm.group(1)}/status/{sm.group(2)}"
        posts.append({
            "handle": "@" + sm.group(1), "author": it.get("author") or sm.group(1), "text": str(it.get("text", "")).strip(),
            "url": url, "posted_at": ts.isoformat(), "likes": it.get("likes"),
            "cited": url in cited or any(sm.group(2) in c for c in cited),
        })
    return posts


async def search(query: str, hours: int = 24, limit: int = 10, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    if not query.strip():
        raise OntologyError("query is empty")
    hours, limit = max(1, min(hours, 168)), max(1, min(limit, 25))
    base = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")
    if not base:
        raise OntologyError("search_x needs ANTHROPIC_BASE_URL pointing at magpie, with a Grok provider configured")
    key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or ""
    body = {"model": MODEL, "input": PROMPT.format(query=query, hours=hours, limit=limit), "tools": [{"type": "x_search"}]}
    own = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10))
    try:
        r = await client.post(f"{base}/v1/responses", json=body,
                              headers={"x-api-key": key, "authorization": f"Bearer {key}", "content-type": "application/json"})
    except httpx.HTTPError as e:
        raise OntologyError(f"X search failed: {e or type(e).__name__}") from None
    finally:
        if own:
            await client.aclose()
    if r.status_code != 200:
        raise OntologyError(f"X search failed: HTTP {r.status_code} {r.text[:200]}")
    resp = r.json()
    searched = any(o.get("type") in ("custom_tool_call", "x_search_call", "tool_call") for o in resp.get("output", []))
    posts = parse_posts(resp, hours) if searched else []  # without a search, anything listed is from memory
    return {"query": query, "hours": hours, "model": MODEL, "searched": searched, "posts": posts,
            "note": None if searched else "the model did not run x_search; no posts are reported"}


def store(conn: psycopg.Connection, posts: list[dict[str, Any]]) -> int:
    """Keep found posts as documents (feed 'x'), so mentions, heat and evidence pick them up."""
    docs = [{"url": p["url"], "title": f"{p['handle']}: {p['text'][:180]}", "summary": p["text"][:800],
             "published_at": datetime.fromisoformat(p["posted_at"])} for p in posts if p["text"]]
    return store_documents(conn, "x", "x", docs)
