"""Research reports written from the graph, sentence by sentence, each sentence tied to its evidence.

A report is a list of sections; every sentence names the nodes and links it rests on (`nodes`, `links`) and
the earlier sentences it is inferred from (`from`). Following `from` backwards gives the reasoning chain the
workbench highlights. The model only sees a digest of the accepted graph (nodes with their latest data, links
with rationale and evidence, recent news), and every id it cites is checked against that digest.

A report is regenerated when the graph's material state changes: a fingerprint over which nodes and links
exist, their evidence verdicts and coarse data moves, so small ticks in prices do not rewrite the report.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .errors import OntologyError
from .evidence import summaries
from .store import OntologyStore

MAX_NODES = 150
MAX_NEWS = 12

REPORT_TOOL = {
    "name": "write_report",
    "description": "Write the research report. Every sentence cites the node ids and link ids it rests on, and/or the ids of earlier sentences it is inferred from.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "report title, Chinese, specific to what the graph says now"},
            "summary": {"type": "string", "description": "two or three sentences: the bottom line"},
            "sections": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "sentences": {"type": "array", "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "s1, s2, … unique across the report, in reading order"},
                            "text": {"type": "string", "description": "one sentence"},
                            "nodes": {"type": "array", "items": {"type": "string"}, "description": "node ids this sentence states facts about"},
                            "links": {"type": "array", "items": {"type": "string"}, "description": "link ids this sentence relies on"},
                            "from": {"type": "array", "items": {"type": "string"}, "description": "ids of EARLIER sentences this one is inferred from"},
                        },
                        "required": ["id", "text"],
                    }},
                },
                "required": ["heading", "sentences"],
            }},
        },
        "required": ["title", "summary", "sections"],
    },
}

SYSTEM = """You are the research desk's writer. Write a concise research report in Chinese from the knowledge graph
digest you are given, the way a sell-side strategist writes a market note: lead with the conclusion, then the
drivers, then risks and what to watch.
Rules:
- Use only facts in the digest. Quote numbers exactly as given (with their units and dates); never invent data.
- Every sentence must either cite the node and link ids it rests on, or list in `from` the earlier sentences it is
  inferred from (or both). Conclusions and judgements should be inferences with `from`, so the reasoning chain is
  visible. Do not cite ids that are not in the digest.
- Relationships rest on links: cite the link id when a sentence uses one. Weak or "仅模型常识" links should be
  stated with hedging; strong evidence can be stated plainly. Association is not causation.
- 4 to 6 sections, 12 to 30 sentences in total. Short sentences. No bullet characters inside sentences."""


# ------------------------------------------------------------------ digest and fingerprint


def _fmt(v: float | None) -> str:
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e8:
        return f"{v / 1e8:,.2f}亿"
    if a >= 1000:
        return f"{v:,.0f}"
    return f"{v:.4g}"


def digest(store: OntologyStore) -> dict[str, Any]:
    """The accepted graph, compact: what the writer may cite."""
    conn = store.conn
    accepted = "(o.changeset_id IS NULL OR c.status = 'accepted')"
    nodes = conn.execute(
        f"""SELECT o.id, o.type, o.name, o.aliases, o.description, o.props,
                   (SELECT count(*) FROM links l WHERE l.status = 'active' AND (l.from_id = o.id OR l.to_id = o.id)) AS deg
            FROM objects o LEFT JOIN changesets c ON c.id = o.changeset_id
            WHERE o.status = 'active' AND {accepted} ORDER BY deg DESC, o.created_at LIMIT %s""",
        (MAX_NODES,),
    ).fetchall()
    ids = {n["id"] for n in nodes}
    links = conn.execute(
        f"""SELECT l.id, l.type, l.from_id, l.to_id, l.rationale, l.confidence, l.provenance
            FROM links l LEFT JOIN changesets c ON c.id = l.changeset_id
            WHERE l.status = 'active' AND {accepted.replace('o.', 'l.')} ORDER BY l.created_at""",
    ).fetchall()
    links = [l for l in links if l["from_id"] in ids and l["to_id"] in ids]
    ev = summaries(conn)
    data = store.series.node_data(list(ids))
    out_nodes = []
    for n in nodes:
        entry: dict[str, Any] = {"id": n["id"], "type": n["type"], "name": n["name"]}
        if n["aliases"]:
            entry["aliases"] = n["aliases"][:3]
        if n["description"]:
            entry["desc"] = n["description"][:120]
        if n["props"]:
            entry["props"] = n["props"]
        series = []
        for d in data.get(n["id"], []):
            if not d.get("latest"):
                continue
            ch = d.get("change") or {}
            pct = ch.get("pct")
            series.append(f"{d['label']}：{_fmt(d['latest']['value'])}{d['unit'] or ''}"
                          f"（{d['latest']['ts'][:10]}{'，' + ch['window'] + ' ' + format(pct, '+.2f') + '%' if pct is not None else ''}）")
        if series:
            entry["data"] = series[:4]
        out_nodes.append(entry)
    out_links = []
    for l in links:
        e = ev.get(l["id"])
        entry = {"id": l["id"], "type": l["type"], "from": l["from_id"], "to": l["to_id"], "rationale": (l["rationale"] or "")[:120]}
        if (l["provenance"] or {}).get("kind") == "llm_prior" and not (e and e["strength"] > 0):
            entry["basis"] = "仅模型常识"
        if e:
            entry["evidence"] = f"强度 {e['strength']:.2f}；" + "；".join(i["summary"] for i in e["items"][:2])
        out_links.append(entry)
    news = conn.execute(
        """SELECT d.title, d.feed, d.published_at, array_agg(DISTINCT m.node_id) AS nodes
           FROM documents d JOIN mentions m ON m.document_id = d.id
           WHERE d.published_at > now() - interval '7 days' AND m.node_id = ANY(%s)
           GROUP BY d.id ORDER BY d.published_at DESC LIMIT %s""",
        (list(ids), MAX_NEWS),
    ).fetchall()
    return {
        "as_of": datetime.now(UTC).isoformat(timespec="minutes"),
        "nodes": out_nodes,
        "links": out_links,
        "news": [{"title": r["title"], "source": r["feed"], "date": r["published_at"].date().isoformat(), "nodes": r["nodes"]}
                 for r in news],
    }


def fingerprint(store: OntologyStore, d: dict[str, Any] | None = None) -> str:
    """Changes when the report would say something different: which nodes and links exist, their evidence,
    and data moves in 2% steps. Price ticks, heat and sentiment drift and timestamps do not count."""
    d = d or digest(store)
    node_ids = [n["id"] for n in d["nodes"]]
    moves = []
    for nid, items in store.series.node_data(node_ids).items():
        for b in items:
            pct = (b.get("change") or {}).get("pct")
            if pct is not None and b["role"] not in ("heat", "sentiment"):
                moves.append((nid, b["role"], round(pct / 2)))
    material = {
        "nodes": sorted(node_ids),
        "links": sorted((l["id"], l.get("basis", ""), (l.get("evidence") or "")[:8]) for l in d["links"]),
        "moves": sorted(moves),
    }
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ writing


def validate(raw: dict[str, Any], node_ids: set[str], link_ids: set[str]) -> dict[str, Any]:
    """Keep only cited ids that exist, `from` pointing at earlier sentences, and sentences that say something."""
    seen: set[str] = set()
    sections = []
    for sec in raw.get("sections") or []:
        sentences = []
        for s in sec.get("sentences") or []:
            text = str(s.get("text") or "").strip()
            sid = str(s.get("id") or "").strip()
            if not text or not sid or sid in seen:
                continue
            sentences.append({
                "id": sid, "text": text,
                "nodes": [x for x in s.get("nodes") or [] if x in node_ids],
                "links": [x for x in s.get("links") or [] if x in link_ids],
                "from": [x for x in s.get("from") or [] if x in seen],
            })
            seen.add(sid)
        if sentences:
            sections.append({"heading": str(sec.get("heading") or "").strip() or "要点", "sentences": sentences})
    if not sections:
        raise OntologyError("the model returned no usable sentences")
    return {"sections": sections}


def model_name() -> str:
    return os.environ.get("REPORT_MODEL") or os.environ.get("WORKBENCH_MODEL", "claude-opus-5-5")


def write(store: OntologyStore, client: Any = None, trigger: str = "manual") -> dict[str, Any]:
    """Generate and store a new report version from the current graph. Failures are stored too."""
    import anthropic

    started = time.monotonic()
    d = digest(store)
    graph_hash = fingerprint(store, d)
    model = model_name()
    rid = f"r_{uuid.uuid4().hex[:12]}"
    version = (store.conn.execute("SELECT coalesce(max(version), 0) AS v FROM reports").fetchone()["v"] or 0) + 1
    if not d["nodes"]:
        raise OntologyError("图里还没有已接受的节点：研报只根据已接受的内容来写，先接受变更集")
    try:
        client = client or anthropic.Anthropic()
        r = client.messages.create(
            model=model, max_tokens=12000, system=SYSTEM, tools=[REPORT_TOOL],
            tool_choice={"type": "tool", "name": REPORT_TOOL["name"]},
            messages=[{"role": "user", "content": "图谱摘要（JSON）：\n" + json.dumps(d, ensure_ascii=False)}],
        )
        raw = next((b.input for b in r.content if b.type == "tool_use"), None)
        if raw is None:
            raise OntologyError(f"the model did not call write_report (stop_reason={r.stop_reason})")
        body = validate(raw, {n["id"] for n in d["nodes"]}, {l["id"] for l in d["links"]})
        store.conn.execute(
            """INSERT INTO reports (id, version, status, title, summary, body, graph_hash, model, trigger, seconds)
               VALUES (%s, %s, 'ready', %s, %s, %s, %s, %s, %s, %s)""",
            (rid, version, str(raw.get("title") or "研究报告")[:120], str(raw.get("summary") or "")[:800], Jsonb(body),
             graph_hash, model, trigger, round(time.monotonic() - started, 1)),
        )
    except Exception as e:  # keep a record of the failure; the next attempt waits for the usual interval
        store.conn.execute(
            """INSERT INTO reports (id, version, status, graph_hash, model, trigger, error, seconds)
               VALUES (%s, %s, 'failed', %s, %s, %s, %s, %s)""",
            (rid, version, graph_hash, model, trigger, f"{type(e).__name__}: {e}"[:500], round(time.monotonic() - started, 1)),
        )
        raise
    return get(store.conn, rid)


def _out(r: dict[str, Any]) -> dict[str, Any]:
    return {**r, "created_at": r["created_at"].isoformat()}


def get(conn: psycopg.Connection, report_id: str) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM reports WHERE id = %s", (report_id,)).fetchone()
    if not r:
        raise OntologyError(f"no report '{report_id}'")
    return _out(r)


def latest(conn: psycopg.Connection, at: datetime | None = None) -> dict[str, Any] | None:
    """The newest ready report (as of `at`, for the timeline)."""
    r = conn.execute(
        """SELECT * FROM reports WHERE status = 'ready' AND (%s::timestamptz IS NULL OR created_at <= %s)
           ORDER BY created_at DESC LIMIT 1""", (at, at),
    ).fetchone()
    return _out(r) if r else None


def last_attempt(conn: psycopg.Connection) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM reports ORDER BY created_at DESC LIMIT 1").fetchone()
    return _out(r) if r else None


def versions(conn: psycopg.Connection, limit: int = 200) -> list[dict[str, Any]]:
    return [{**r, "created_at": r["created_at"].isoformat()} for r in conn.execute(
        "SELECT id, version, status, title, graph_hash, created_at FROM reports ORDER BY created_at DESC LIMIT %s", (limit,))]
