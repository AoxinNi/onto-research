"""The text layer: news in, mentions and narrative heat out, notable events promoted for review.

Pipeline (one pass runs inside the collector loop):
1. fetch    RSS/Atom feeds every 15 minutes into `documents` (deduplicated by URL)
2. screen   keyword pre-filter against node names and a BTC vocabulary; the rest never reaches a model
3. extract  a model maps each article to nodes (sentiment, relevance, a short quote) and flags discrete events
4. signals  per-node heat and sentiment series, bound to the node automatically
5. promote  events reported by two outlets, or severe enough, become Event nodes in a changeset for review

Mentions are not reviewed (too many); anything that changes the semantic graph is.
"""

from __future__ import annotations

import hashlib
import html
import logging
import math
import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import psycopg

from .errors import OntologyError
from .sources import USER_AGENT, Obs
from .store import OntologyStore, normalize

log = logging.getLogger("news")

FEEDS = [
    ("coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", "en"),
    ("cointelegraph", "https://cointelegraph.com/rss", "en"),
    ("decrypt", "https://decrypt.co/feed", "en"),
    ("theblock", "https://www.theblock.co/rss.xml", "en"),
    ("bitcoinmagazine", "https://bitcoinmagazine.com/feed", "en"),
    ("panews", "https://www.panewslab.com/rss.xml?lang=zh", "zh"),
]
FEED_POLL = timedelta(minutes=15)
MAX_AGE = timedelta(days=3)       # older articles are stored but not sent for extraction
BATCH = 8                         # articles per model call
MAX_BATCHES = 4                   # per pass, so a backlog drains over a few minutes
TAU = timedelta(hours=72)         # heat half-life is TAU * ln 2, about two days
SIGNAL_HOURS = 168                # heat / sentiment points kept fresh: the last 7 days, hourly
MIN_MENTIONS = 2                  # a node needs this many mentions in 30 days to get heat / sentiment series
PROMOTE_FEEDS = 2                 # distinct outlets that make an event worth a node
PROMOTE_SEVERITY = 4              # or one report this severe
EVENT_CATEGORIES = ["regulation", "macro_release", "hack", "etf", "halving", "whale", "policy", "corporate", "other"]

VOCABULARY = [
    "bitcoin", "btc", "比特币", "etf", "fed", "federal reserve", "美联储", "fomc", "powell", "鲍威尔", "cpi", "通胀",
    "inflation", "rate cut", "rate hike", "降息", "加息", "treasury", "美债", "stablecoin", "稳定币", "tether", "usdt",
    "blackrock", "贝莱德", "ibit", "strategy", "saylor", "miner", "矿工", "hashrate", "算力", "halving", "减半",
    "liquidation", "爆仓", "sec", "监管", "dollar", "美元", "macro", "宏观",
]


# ---------------------------------------------------------------------- feeds


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    raw = "".join(el.itertext()) if len(el) else (el.text or "")
    raw = re.sub(r"<[^>]+>", " ", raw)
    return " ".join(html.unescape(raw).split())


def _when(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_feed(xml_text: str) -> list[dict[str, Any]]:
    """RSS 2.0 items or Atom entries as {url, title, summary, published_at}."""
    root = ET.fromstring(xml_text)
    out = []
    atom = "{http://www.w3.org/2005/Atom}"
    for item in root.iter("item"):
        url = _text(item.find("link")) or _text(item.find("guid"))
        when = _when(_text(item.find("pubDate")) or _text(item.find("{http://purl.org/dc/elements/1.1/}date")))
        out.append({"url": url, "title": _text(item.find("title")), "summary": _text(item.find("description")),
                    "published_at": when})
    for entry in root.iter(f"{atom}entry"):
        link = entry.find(f"{atom}link")
        when = _when(_text(entry.find(f"{atom}published")) or _text(entry.find(f"{atom}updated")))
        out.append({"url": link.get("href", "") if link is not None else "", "title": _text(entry.find(f"{atom}title")),
                    "summary": _text(entry.find(f"{atom}summary")) or _text(entry.find(f"{atom}content")),
                    "published_at": when})
    return [{**d, "summary": d["summary"][:800]} for d in out if d["url"] and d["title"] and d["published_at"]]


def _doc_id(url: str) -> str:
    return "d_" + hashlib.sha1(url.encode()).hexdigest()[:12]


def store_documents(conn: psycopg.Connection, feed: str, lang: str, docs: list[dict[str, Any]]) -> int:
    n = 0
    with conn.transaction(), conn.cursor() as cur:
        for d in docs:
            cur.execute(
                """INSERT INTO documents (id, feed, url, title, summary, lang, published_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (url) DO NOTHING""",
                (_doc_id(d["url"]), feed, d["url"], d["title"][:500], d["summary"], lang, d["published_at"]),
            )
            n += cur.rowcount
    return n


def fetch_feeds(conn: psycopg.Connection, now: datetime, force: bool = False, timeout: float = 20) -> int:
    """Fetch due feeds (all of them with force) in parallel; store new articles. Returns how many were new."""
    for name, url, lang in FEEDS:
        conn.execute("INSERT INTO feeds (name, url, lang) VALUES (%s, %s, %s) ON CONFLICT (name) DO UPDATE "
                     "SET url = EXCLUDED.url, lang = EXCLUDED.lang", (name, url, lang))
    names = {n for n, _, _ in FEEDS}
    due = [f for f in conn.execute("SELECT * FROM feeds ORDER BY name").fetchall()
           if f["name"] in names and (force or not f["last_fetch_at"] or now - f["last_fetch_at"] >= FEED_POLL)]

    def get(f: dict[str, Any]):
        try:
            r = httpx.get(f["url"], timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            return f, parse_feed(r.text), None
        except (httpx.HTTPError, ET.ParseError) as e:
            return f, [], str(e)[:300] or type(e).__name__

    new = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        for f, docs, err in pool.map(get, due):
            if err:
                conn.execute("UPDATE feeds SET last_fetch_at = now(), status = 'error', last_error = %s WHERE name = %s",
                             (err, f["name"]))
                log.warning("feed %s: %s", f["name"], err)
                continue
            new += store_documents(conn, f["name"], f["lang"], docs)
            conn.execute("UPDATE feeds SET last_fetch_at = now(), status = 'ok', last_error = NULL WHERE name = %s",
                         (f["name"],))
    return new


# --------------------------------------------------------------------- screen


def vocabulary(conn: psycopg.Connection) -> list[re.Pattern]:
    """Node names and aliases plus the fixed BTC vocabulary, as patterns. Latin words match whole words only."""
    terms = set(VOCABULARY)
    for r in conn.execute("SELECT terms FROM objects WHERE status = 'active'"):
        terms.update(t for t in r["terms"] if len(t) >= 2)
    pats = []
    for t in sorted(terms):
        if re.fullmatch(r"[\x00-\x7f]+", t):
            if len(t) >= 3 or t in ("oi",):
                pats.append(re.compile(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])"))
        else:
            pats.append(re.compile(re.escape(t)))
    return pats


def screen(conn: psycopg.Connection) -> int:
    pats = vocabulary(conn)
    rows = conn.execute("SELECT id, title, summary FROM documents WHERE relevant IS NULL").fetchall()
    for r in rows:
        text = normalize(f"{r['title']} {r['summary'] or ''}")
        conn.execute("UPDATE documents SET relevant = %s WHERE id = %s", (any(p.search(text) for p in pats), r["id"]))
    return len(rows)


# -------------------------------------------------------------------- extract

EXTRACT_TOOL = {
    "name": "record_extraction",
    "description": "Record, for every article, which ontology nodes it is about and whether it reports a discrete event.",
    "input_schema": {
        "type": "object",
        "properties": {"docs": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "doc": {"type": "integer", "description": "article number as given"},
                "mentions": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "node_id": {"type": "string"},
                        "sentiment": {"type": "number", "description": "-1 very bearish … 0 neutral … 1 very bullish, for BTC holders"},
                        "relevance": {"type": "number", "description": "0-1, how central this node is to the article"},
                        "quote": {"type": "string", "description": "at most 20 words from the article that support it"},
                    },
                    "required": ["node_id", "sentiment", "relevance"],
                }},
                "event": {"type": ["object", "null"], "properties": {
                    "title": {"type": "string", "description": "short canonical Chinese name; reuse an existing event name if it is the same event"},
                    "category": {"type": "string", "enum": EVENT_CATEGORIES},
                    "severity": {"type": "integer", "description": "1 minor … 5 market-wide shock"},
                    "node_ids": {"type": "array", "items": {"type": "string"}},
                }, "required": ["title", "category", "severity"]},
            },
            "required": ["doc", "mentions"],
        }}},
        "required": ["docs"],
    },
}

EXTRACT_SYSTEM = """You read crypto and macro news for a BTC research desk and map each article onto the desk's ontology.
For every article:
- mentions: the listed nodes the article is genuinely about (relevance >= 0.3). Match by meaning, not only by name:
  "rate cut" concerns the Fed and the rate nodes. sentiment is the article's implication for BTC holders. Use only
  node ids from the list. Leave mentions empty for articles unrelated to the nodes. News about other coins (ETH,
  SOL, altcoin ETFs, memecoins) only counts when it bears directly on BTC; otherwise leave it out.
- event: only when the article reports a specific occurrence with a date (an approval, a hack, a rate decision, a
  record ETF flow day, a law passed). Not for opinion, analysis or price commentary. Give the affected node ids.
Return one entry per article, through the record_extraction tool."""


class Extractor:
    def __init__(self, client: Any = None, model: str | None = None):
        import anthropic

        self.client = client or anthropic.Anthropic()
        self.model = model or os.environ.get("NEWS_MODEL") or os.environ.get("WORKBENCH_MODEL", "claude-opus-5-5")

    def extract(self, docs: list[dict[str, Any]], nodes: list[dict[str, Any]], events: list[str]) -> list[dict[str, Any]]:
        node_lines = "\n".join(f"{n['id']} | {n['type']} | {n['name']} | {', '.join(n['aliases'])}" for n in nodes)
        doc_lines = "\n\n".join(
            f"[{i}] ({d['feed']}, {d['published_at']:%Y-%m-%d}) {d['title']}\n{d['summary'] or ''}" for i, d in enumerate(docs)
        )
        prompt = (f"Nodes (id | type | name | aliases):\n{node_lines}\n\n"
                  f"Existing events: {', '.join(events) or 'none'}\n\nArticles:\n{doc_lines}")
        r = self.client.messages.create(
            model=self.model, max_tokens=8000, system=EXTRACT_SYSTEM, tools=[EXTRACT_TOOL],
            tool_choice={"type": "tool", "name": EXTRACT_TOOL["name"]},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in r.content:
            if block.type == "tool_use":
                return block.input.get("docs", [])
        raise OntologyError(f"extraction returned no tool call (stop_reason={r.stop_reason})")


_extractor: Extractor | None = None


def extractor_from_env() -> Extractor | None:
    """The shared extractor, if a model endpoint is configured (an API key, or magpie's placeholder key)."""
    global _extractor
    if _extractor is None and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        _extractor = Extractor()
    return _extractor


def run_extraction(conn: psycopg.Connection, extractor: Extractor, now: datetime) -> int:
    nodes = conn.execute(
        "SELECT id, type, name, aliases FROM objects WHERE status = 'active' ORDER BY type, name LIMIT 400"
    ).fetchall()
    known = {n["id"] for n in nodes}
    # Event nodes plus recent candidate titles, so reports of the same event from different outlets share a name.
    events = [r["name"] for r in conn.execute(
        """(SELECT name FROM objects WHERE status = 'active' AND type = 'Event' ORDER BY created_at DESC LIMIT 60)
           UNION (SELECT DISTINCT title FROM event_candidates WHERE created_at > %s ORDER BY title LIMIT 80)""",
        (now - timedelta(days=3),),
    )]
    done = 0
    for _ in range(MAX_BATCHES):
        docs = conn.execute(
            """SELECT * FROM documents WHERE relevant AND extracted_at IS NULL AND published_at > %s
               ORDER BY published_at DESC LIMIT %s""",
            (now - MAX_AGE, BATCH),
        ).fetchall()
        if not docs:
            break
        try:
            results = extractor.extract(docs, nodes, events)
        except Exception as e:  # the model or the network; try again next pass
            msg = f"{type(e).__name__}: {e}"[:300]
            conn.execute("UPDATE documents SET extract_error = %s WHERE id = ANY(%s)", (msg, [d["id"] for d in docs]))
            log.warning("extraction failed: %s", msg)
            break
        by_doc = {r.get("doc"): r for r in results if isinstance(r, dict)}
        with conn.transaction():
            for i, d in enumerate(docs):
                r = by_doc.get(i) or {}
                for m in r.get("mentions") or []:
                    if m.get("node_id") not in known:
                        continue
                    conn.execute(
                        """INSERT INTO mentions (document_id, node_id, sentiment, relevance, quote)
                           VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                        (d["id"], m["node_id"], max(-1.0, min(1.0, float(m.get("sentiment") or 0))),
                         max(0.0, min(1.0, float(m.get("relevance") or 0))), (m.get("quote") or "")[:300] or None),
                    )
                ev = r.get("event")
                if isinstance(ev, dict) and ev.get("title"):
                    conn.execute(
                        """INSERT INTO event_candidates (document_id, key, title, category, severity, node_ids)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (d["id"], normalize(ev["title"]), ev["title"][:120],
                         ev.get("category") if ev.get("category") in EVENT_CATEGORIES else "other",
                         max(1, min(5, int(ev.get("severity") or 1))),
                         [x for x in ev.get("node_ids") or [] if x in known]),
                    )
                conn.execute("UPDATE documents SET extracted_at = now(), extract_error = NULL WHERE id = %s", (d["id"],))
        done += len(docs)
    return done


# -------------------------------------------------------------------- signals


def heat_and_sentiment(mentions: list[dict[str, Any]], hours: list[datetime]) -> tuple[list[Obs], list[Obs]]:
    """Exponentially decayed heat, and decay-weighted mean sentiment, at each hour."""
    heat, sent = [], []
    for t in hours:
        h = num = den = 0.0
        for m in mentions:
            age = t - m["published_at"]
            if age < timedelta(0) or age > timedelta(days=14):
                continue
            w = math.exp(-age / TAU) * m["relevance"]
            h += w * (0.5 + 0.5 * abs(m["sentiment"]))
            num += w * m["sentiment"]
            den += w
        if den > 0:
            heat.append(Obs(t, round(h, 4)))
            if den > 0.05:
                sent.append(Obs(t, round(num / den, 4)))
    return heat, sent


def update_signals(store: OntologyStore, now: datetime) -> list[str]:
    """Refresh heat and sentiment series for every node mentioned often enough; bind new ones directly."""
    conn = store.conn
    rows = conn.execute(
        """SELECT m.node_id, o.name, d.published_at, m.sentiment, m.relevance
           FROM mentions m JOIN documents d ON d.id = m.document_id JOIN objects o ON o.id = m.node_id
           WHERE o.status = 'active' AND d.published_at > %s ORDER BY m.node_id, d.published_at""",
        (now - timedelta(days=30),),
    ).fetchall()
    by_node: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_node.setdefault(r["node_id"], []).append(r)
    top = now.replace(minute=0, second=0, microsecond=0)
    hours = [top - timedelta(hours=h) for h in range(SIGNAL_HOURS - 1, -1, -1)]
    changed = []
    for node_id, ms in by_node.items():
        if len(ms) < MIN_MENTIONS:
            continue
        heat, sent = heat_and_sentiment(ms, hours)
        for role, src, points in (("heat", "news_heat", heat), ("sentiment", "news_sentiment", sent)):
            b = conn.execute(
                """SELECT b.status, b.series_id, s.source FROM bindings b JOIN series s ON s.id = b.series_id
                   WHERE b.node_id = %s AND b.role = %s AND (b.status = 'active' OR b.provenance->>'ref' = 'news')
                   ORDER BY b.status = 'active' DESC LIMIT 1""",
                (node_id, role),
            ).fetchone()
            if b and (b["status"] != "active" or b["source"] != src):
                continue  # a human removed it, or the role is taken by another series
            series_id = b["series_id"] if b else store.bind_direct(
                node_id, src, {"node": node_id, "name": ms[0]["name"]}, role, "新闻提及自动生成",
                {"kind": "data_source", "ref": "news"},
            )["binding"]["series_id"]
            s = {"id": series_id}
            if store.series.write(s["id"], points):
                changed.append(s["id"])
            store.series.mark(s["id"], None)
    return changed


# -------------------------------------------------------------------- promote

AFFECTS = {"Narrative", "Asset"}
INVOLVES = {"Entity"}


def promote_events(conn: psycopg.Connection, now: datetime) -> list[str]:
    """Turn well-reported events into Event nodes with their links, in one changeset for the human to review."""
    groups = conn.execute(
        """SELECT c.key, array_agg(c.id) AS ids, array_agg(DISTINCT d.feed) AS feeds, max(c.severity) AS severity,
                  min(d.published_at) AS first_seen, array_agg(d.url ORDER BY d.published_at) AS urls,
                  (array_agg(c.title ORDER BY c.severity DESC, d.published_at))[1] AS title,
                  (array_agg(c.category ORDER BY c.severity DESC))[1] AS category,
                  array_agg(DISTINCT n) FILTER (WHERE n IS NOT NULL) AS node_ids
           FROM event_candidates c JOIN documents d ON d.id = c.document_id
           LEFT JOIN LATERAL unnest(c.node_ids) n ON true
           WHERE c.state = 'open' AND d.published_at > %s
           GROUP BY c.key""",
        (now - timedelta(days=3),),
    ).fetchall()
    store = OntologyStore(conn, actor="news-agent")
    promoted = []
    for g in groups:
        if len(g["feeds"]) < PROMOTE_FEEDS and g["severity"] < PROMOTE_SEVERITY:
            continue
        if not g["node_ids"]:
            continue  # nothing in the graph is affected; not our business
        reports = len(set(g["urls"]))
        prov = {"kind": "data_source", "ref": g["urls"][0], "note": f"{reports} 篇报道（{', '.join(g['feeds'])}）"}
        try:
            node = store.upsert_node(
                "Event", g["title"], prov, f"新闻晋升：{reports} 篇报道，最高严重度 {g['severity']}", key=g["key"],
                props={"category": g["category"], "severity": g["severity"], "status": "happened",
                       "occurred_at": g["first_seen"].isoformat()},
            )["node"]
        except OntologyError as e:  # most likely a near-duplicate of an existing event; leave it for the human
            log.info("not promoting %s: %s", g["title"], e)
            conn.execute("UPDATE event_candidates SET state = 'promoted' WHERE id = ANY(%s)", (g["ids"],))
            continue
        for target in conn.execute(
            "SELECT id, type, name FROM objects WHERE id = ANY(%s) AND status = 'active'", (g["node_ids"] or [],)
        ).fetchall():
            ltype = "affects" if target["type"] in AFFECTS else "involves" if target["type"] in INVOLVES else None
            if not ltype:
                continue
            try:
                store.create_link(ltype, node["id"], target["id"], prov, f"新闻报道：{g['title']}",
                                  confidence=min(0.95, 0.5 + 0.1 * reports))
            except OntologyError as e:
                log.info("skipping link %s -> %s: %s", g["title"], target["name"], e)
        conn.execute("UPDATE event_candidates SET state = 'promoted', promoted_to = %s WHERE id = ANY(%s)",
                     (node["id"], g["ids"]))
        promoted.append(g["title"])
    if promoted:
        store.submit_changeset(f"新闻晋升 {len(promoted)} 个事件：" + "、".join(promoted[:3]) + ("…" if len(promoted) > 3 else ""))
    return promoted


# ----------------------------------------------------------------------- pass


def run(conn: psycopg.Connection, extractor: Extractor | None = None, fetch: bool = True) -> list[str]:
    """One pass of the text pipeline. Returns ids of series that changed, plus 'news' if the graph did."""
    now = datetime.now(UTC)
    if fetch:
        fetch_feeds(conn, now)
    screen(conn)
    ex = extractor or extractor_from_env()
    changed: list[str] = []
    if ex and run_extraction(conn, ex, now):
        changed.append("news")
    changed += update_signals(OntologyStore(conn, actor="system"), now)
    if promote_events(conn, now):
        changed.append("news")
    return changed


def node_news(conn: psycopg.Connection, node_id: str, days: int = 14, limit: int = 30) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT d.id, d.feed, d.url, d.title, d.published_at, m.sentiment, m.relevance, m.quote
           FROM mentions m JOIN documents d ON d.id = m.document_id
           WHERE m.node_id = %s AND d.published_at > now() - make_interval(days => %s)
           ORDER BY d.published_at DESC LIMIT %s""",
        (node_id, days, limit),
    ).fetchall()
    return [{**r, "published_at": r["published_at"].isoformat()} for r in rows]


def search_news(conn: psycopg.Connection, query: str | None = None, node_id: str | None = None, days: int = 7,
                limit: int = 20) -> list[dict[str, Any]]:
    """Recent relevant articles, optionally about one node or containing some text, with the nodes they mention."""
    rows = conn.execute(
        """SELECT d.id, d.feed, d.url, d.title, d.summary, d.published_at,
                  coalesce(json_agg(json_build_object('node_id', m.node_id, 'name', o.name, 'sentiment', m.sentiment,
                                                      'relevance', m.relevance)) FILTER (WHERE m.node_id IS NOT NULL), '[]') AS mentions
           FROM documents d LEFT JOIN mentions m ON m.document_id = d.id LEFT JOIN objects o ON o.id = m.node_id
           WHERE d.relevant AND d.published_at > now() - make_interval(days => %(days)s)
             AND (%(q)s::text IS NULL OR d.title ILIKE %(pat)s OR d.summary ILIKE %(pat)s)
             AND (%(node)s::text IS NULL OR EXISTS (SELECT 1 FROM mentions x WHERE x.document_id = d.id AND x.node_id = %(node)s))
           GROUP BY d.id ORDER BY d.published_at DESC LIMIT %(limit)s""",
        {"days": days, "q": query, "pat": f"%{query}%" if query else None, "node": node_id, "limit": min(limit, 100)},
    ).fetchall()
    return [{**r, "published_at": r["published_at"].isoformat(), "summary": (r["summary"] or "")[:300]} for r in rows]
