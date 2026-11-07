"""MCP server exposing the ontology tools to agents.

Run with `python -m ontology.mcp_server` (stdio). Accepting changesets and types is deliberately
not a tool: those are human decisions, made in the workbench UI or with `python -m ontology.cli`.
"""

from __future__ import annotations

import asyncio
import functools
import os
from contextvars import ContextVar
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import evidence, news, projects, ths, xsearch
from .sources import SOURCES
from .db import connect
from .errors import OntologyError
from .providers import load_env_file
from .store import OntologyStore, Provenance, _parse_dt

INSTRUCTIONS = """\
You maintain a typed ontology for the user’s scientific research project. Distinguish hypotheses, observations and conclusions.
Rules:
1. Call get_schema first. Only use the object and link types it lists. If something truly does not fit, call
   propose_type and tell the user; a human must accept it before you can use it.
2. Before creating a node, call search_nodes with its name and common aliases (中文 and English). Reuse what exists.
3. Every node and link needs provenance: kind=user when the user told you, llm_prior when it is your own background
   knowledge, data_source (with ref) when it comes from data. Every link needs a rationale.
4. Your writes collect into a changeset. When a request is done, call submit_changeset with a one-line summary so
   the user can review and accept it.
5. Nodes can be bound to live data series (prices, rates, flows). Read data with get_latest / get_series; check
   list_bindings before proposing one. propose_binding (see list_sources for what can be collected) is reviewed
   like any other write. Never invent numbers: quote values from these tools, with their timestamp.
6. search_news returns what the news desk has read, already mapped to nodes. Use it for "what is happening" questions.
7. Links carry evidence (get_link_evidence). Before asserting a relationship is strong, check it; when your own
   reading supports or contradicts one, record it with add_evidence_note.
8. For what people say on X (Twitter), call search_x; it searches live and returns real post URLs. Never describe
   posts you did not get from it.
9. A-share research: a_share_search resolves names to thscodes (600519.SH, 886042.TI); model listed companies as
   Asset with asset_class equity and the thscode as symbol, indices and sectors as asset_class index. Bind any relevant series only if the user asks. a_share_quote / a_share_financials / a_share_index_members answer questions.
10. Projects: several ontologies live side by side. list_projects shows them and the current one; use_project
   switches (in the workbench the page fixes it). Say which project you are working in when it is not obvious.
"""

server = MCPServer("onto-research", instructions=INSTRUCTIONS)
_store: OntologyStore | None = None  # a fixed store overrides project selection (tests)
_stores: dict[str, OntologyStore] = {}
# Which project tool calls act on. The workbench pins it per conversation (current_project); an MCP client picks
# one with use_project, starting from ONTOLOGY_PROJECT if its config sets one, else the default project.
current_project: ContextVar[str | None] = ContextVar("current_project", default=None)
_selected: str | None = None


def active_project() -> str:
    return current_project.get() or _selected or os.environ.get("ONTOLOGY_PROJECT") or projects.DEFAULT


def store() -> OntologyStore:
    if _store is not None:
        return _store
    pid = active_project()
    s = _stores.get(pid)
    if s is None or s.conn.closed:
        s = OntologyStore(projects.project_connect(pid), actor=os.environ.get("ONTOLOGY_ACTOR", "workbench-agent"))
        if not s.registry.object_types:
            s.sync_seed_types()
        _stores[pid] = s
    return s


def ontology_tool(fn):
    """Register a tool and turn OntologyError into a message the agent can read."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except OntologyError as e:
            raise ToolError(str(e)) from e

    return server.tool()(wrapper)


def ontology_tool_async(fn):
    """ontology_tool for coroutines, for tools that wait on the network and must not block the server."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except OntologyError as e:
            raise ToolError(str(e)) from e

    return server.tool()(wrapper)


# ---------------------------------------------------------------------- read


@ontology_tool
def get_schema() -> dict[str, Any]:
    """List every object type and link type (with properties and allowed endpoints), plus pending type proposals."""
    return store().get_schema()


@ontology_tool
def search_nodes(query: str, types: list[str] | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Find nodes by name, key or alias (case-insensitive, substring). Call before creating any node."""
    return store().search_nodes(query, types, limit)


@ontology_tool
def get_node(node_id: str) -> dict[str, Any]:
    """One node with its properties, provenance and its links."""
    return store().get_node(node_id)


@ontology_tool
def get_neighbors(
    node_id: str,
    depth: int = 1,
    link_types: list[str] | None = None,
    direction: Literal["out", "in", "both"] = "both",
    limit: int = 200,
) -> dict[str, Any]:
    """The subgraph within `depth` (1-3) hops of a node, as {nodes, links}."""
    return store().get_neighbors(node_id, depth, link_types, direction, limit)


@ontology_tool
def find_paths(
    from_id: str, to_id: str, max_depth: int = 4, link_types: list[str] | None = None, limit: int = 5
) -> list[list[dict[str, Any]]]:
    """Shortest relationship chains between two nodes (direction ignored), e.g. how a rate cut reaches BTC."""
    return store().find_paths(from_id, to_id, max_depth, link_types, limit)


@ontology_tool
def validate_graph() -> dict[str, Any]:
    """Report orphan nodes, likely duplicates and rows that break the current schema."""
    return store().validate_graph()


# --------------------------------------------------------------------- write


@ontology_tool
def upsert_node(
    type: str,
    name: str,
    provenance: Provenance,
    rationale: str,
    key: str | None = None,
    aliases: list[str] | None = None,
    description: str | None = None,
    props: dict[str, Any] | None = None,
    force_new: bool = False,
) -> dict[str, Any]:
    """Create a node, or update the existing one with the same type and key (key defaults to the name).
    Refuses when the name or an alias matches another node of the same type; pass that node's key to update it."""
    return store().upsert_node(type, name, provenance, rationale, key, aliases, description, props, force_new)


@ontology_tool
def update_props(
    node_id: str,
    rationale: str,
    props: dict[str, Any] | None = None,
    description: str | None = None,
    add_aliases: list[str] | None = None,
) -> dict[str, Any]:
    """Patch a node's properties (null removes one), description or aliases."""
    return store().update_props(node_id, rationale, props, description, add_aliases)


@ontology_tool
def create_link(
    type: str,
    from_id: str,
    to_id: str,
    provenance: Provenance,
    rationale: str,
    confidence: float | None = None,
    props: dict[str, Any] | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
) -> dict[str, Any]:
    """Create a typed link between two nodes (or update the same link). The link type's allowed
    from/to object types are enforced. confidence is 0-1; valid_from/valid_to are ISO datetimes."""
    return store().create_link(type, from_id, to_id, provenance, rationale, confidence, props, valid_from, valid_to)


@ontology_tool
def merge_nodes(keep_id: str, drop_id: str, rationale: str) -> dict[str, Any]:
    """Merge a duplicate node into another of the same type. Links move over; names become aliases."""
    return store().merge_nodes(keep_id, drop_id, rationale)


@ontology_tool
def archive(target_id: str, rationale: str) -> dict[str, Any]:
    """Soft-delete a node (with its links) or a link. Nothing is physically deleted."""
    return store().archive(target_id, rationale)


@ontology_tool
def submit_changeset(summary: str) -> dict[str, Any]:
    """Send your current changes to the user for review. Call once per request, with a one-line summary."""
    return store().submit_changeset(summary)


@ontology_tool
def list_changesets(
    status: Literal["open", "pending", "accepted", "rejected", "reverted"] | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """Recent changesets with their status and number of operations."""
    return store().list_changesets(status, limit)


@ontology_tool
def revert_changeset(changeset_id: str | None = None) -> dict[str, Any]:
    """Undo a whole changeset; with no id, the most recent one still in effect. Use when the user says undo."""
    return store().revert_changeset(changeset_id)


# ---------------------------------------------------------------------- data


@ontology_tool
def list_sources() -> list[dict[str, Any]]:
    """Data sources that can be bound to nodes, with the params each one takes."""
    return [src.describe() for src in SOURCES.values()]


@ontology_tool
def list_bindings(node_id: str | None = None) -> list[dict[str, Any]]:
    """Data bindings of one node (or all), each with its series, latest value, change and health."""
    return store().list_bindings(node_id)


@ontology_tool
def get_latest(node_id: str, role: str | None = None) -> dict[str, Any]:
    """Latest value of a node's bound series (default: its first binding), with change and freshness."""
    s = store()
    out = s.series.summary(s.node_series(node_id, role))
    out.pop("spark")
    return out


@ontology_tool
def get_series(
    node_id: str, role: str | None = None, start: str | None = None, end: str | None = None, limit: int = 200
) -> dict[str, Any]:
    """Points of a node's bound series, oldest first. start/end are ISO datetimes; limit keeps the newest points."""
    s = store()
    row = s.node_series(node_id, role)
    return {"series_id": row["id"], "label": row["label"], "unit": row["unit"], "frequency": row["frequency"],
            "points": s.series.points(row["id"], _parse_dt(start, "start"), _parse_dt(end, "end"), limit)}


@ontology_tool
def search_news(query: str | None = None, node_id: str | None = None, days: int = 7, limit: int = 20) -> list[dict[str, Any]]:
    """Recent BTC-relevant news (titles, short summaries, URLs) with the nodes each article mentions and its
    sentiment. Filter by text, by node, or both. Cite the URL when you rely on an article."""
    return news.search_news(store().conn, query, node_id, days, limit)


@ontology_tool
def get_link_evidence(link_id: str) -> dict[str, Any]:
    """What backs a link: series co-movement (correlation, lead/lag), news that mentions both ends, agent notes,
    and a combined strength 0-1. Association, not causation."""
    return evidence.link_evidence(store().conn, link_id)


@ontology_tool
def add_evidence_note(
    link_id: str, summary: str, verdict: Literal["supports", "contradicts", "weak"], strength: float,
    refs: list[str] | None = None,
) -> dict[str, Any]:
    """Record your own evidence for or against a link, e.g. after reading articles or a series window. Cite what you
    used in refs (URLs, series ids with a time window). strength is 0-1. This annotates the link; it is not reviewed."""
    s = store()
    return evidence.add_note(s.conn, link_id, summary, verdict, strength, refs, s.actor)


@ontology_tool_async
async def search_x(query: str, hours: int = 24, limit: int = 10) -> dict[str, Any]:
    """Search X (Twitter) live through Grok's x_search: posts from the last `hours` (max 168) matching the query,
    each with handle, text, time and URL. Takes 15-30 s. Found posts are kept for the news desk (mentions, heat).
    Quote handles and URLs; if `searched` is false, there are no posts, say so instead of guessing."""
    result = await xsearch.search(query, hours, limit)
    if result["posts"]:
        await asyncio.to_thread(xsearch.store, store().conn, result["posts"])
    return result


@ontology_tool_async
async def a_share_search(query: str, asset_type: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """Find A-share stocks, indices, 同花顺 sectors, funds… by name or code (同花顺). Returns thscodes such as
    600519.SH (stocks) or 886042.TI (sectors). asset_type: a-share, a-share-index, fund-etf, fund-otc, futures…
    Use the thscode as the Asset's symbol (asset_class equity or index) so price data binds automatically."""
    return await asyncio.to_thread(ths.search, query, asset_type, limit)


@ontology_tool_async
async def a_share_quote(thscodes: list[str]) -> list[dict[str, Any]]:
    """Latest price, change and (for stocks) PE/PB/PS/PCF of up to 50 A-shares or indices (同花顺)."""
    return await asyncio.to_thread(ths.quote, thscodes)


@ontology_tool_async
async def a_share_financials(thscode: str, report: str | None = None) -> dict[str, Any]:
    """Growth, profitability, solvency and cash-flow indicators of an A-share for a report period
    (yyyy-1 Q1, yyyy-2 half year, yyyy-3 Q3, yyyy-4 annual); default: the latest published (同花顺)."""
    return await asyncio.to_thread(ths.financials, thscode, report)


@ontology_tool_async
async def a_share_index_members(thscode: str) -> list[dict[str, Any]]:
    """Current constituents of an index or 同花顺 sector, e.g. 000300.SH (沪深300) or 886042.TI (同花顺)."""
    return await asyncio.to_thread(ths.index_members, thscode)


@ontology_tool
def propose_binding(
    node_id: str, source: str, params: dict[str, Any], role: str, rationale: str,
    provenance: Provenance | None = None,
) -> dict[str, Any]:
    """Bind a node to a data series, e.g. 比特币 -> binance_klines {symbol: BTCUSDT} as role price. One series per
    node and role; proposing again replaces it. Reviewed like other writes; collection starts once accepted."""
    return store().propose_binding(node_id, source, params, role, rationale,
                                   provenance.model_dump(exclude_none=True) if provenance else None)


# -------------------------------------------------------------------- schema


@ontology_tool
def propose_type(
    kind: Literal["object", "link"],
    name: str,
    description: str,
    justification: str,
    layer: Literal["world", "company"] | None = None,
    properties: dict[str, dict[str, Any]] | None = None,
    from_types: list[str] | None = None,
    to_types: list[str] | None = None,
) -> dict[str, Any]:
    """Propose a new object type (needs layer) or link type (needs from_types and to_types). It cannot be used
    until a human accepts it. Properties look like {"unit": {"type": "string"}}; types: string, text, number,
    integer, boolean, datetime, enum (with "values"), string_list. Explain why existing types do not fit."""
    return store().propose_type(kind, name, description, justification, layer, properties, from_types, to_types)


# ------------------------------------------------------------------ projects


@ontology_tool
def list_projects() -> dict[str, Any]:
    """List the ontology projects (id, name, description, size) and which one tool calls act on now."""
    with connect() as conn:
        rows = projects.list_all(conn, include_archived=False)
    return {"current": active_project(), "pinned": current_project.get() is not None,
            "projects": [{"id": p["id"], "name": p["name"], "description": p.get("description"),
                          "nodes": p["stats"]["nodes"], "links": p["stats"]["links"]} for p in rows]}


@ontology_tool
def use_project(project_id: str) -> dict[str, Any]:
    """Switch the project that every following tool call reads and writes. Call list_projects first. In the
    workbench the page fixes the project and this is refused."""
    global _selected
    if current_project.get() is not None:
        raise OntologyError(f"这个对话固定在项目 {current_project.get()}，不能切换")
    with connect() as conn:
        p = projects.get(conn, project_id)
    if p["status"] != "active":
        raise OntologyError(f"project {project_id} is archived")
    _selected = project_id
    s = store()
    return {"current": project_id, "name": p["name"], "nodes": s.conn.execute(
        "SELECT count(*) AS n FROM objects WHERE status = 'active'").fetchone()["n"]}


def main() -> None:
    load_env_file()  # DATABASE_URL, THS_API_KEY, the model gateway… come from the project's .env
    store()
    server.run("stdio")


if __name__ == "__main__":
    main()
