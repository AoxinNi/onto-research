import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from ontology import mcp_server
from ontology.store import OntologyStore

EXPECTED = {
    "get_schema", "search_nodes", "get_node", "get_neighbors", "find_paths", "validate_graph",
    "upsert_node", "update_props", "create_link", "merge_nodes", "archive",
    "submit_changeset", "list_changesets", "revert_changeset", "propose_type",
    "list_sources", "list_bindings", "get_latest", "get_series", "propose_binding", "search_news",
    "get_link_evidence", "add_evidence_note", "search_x",
    "a_share_search", "a_share_quote", "a_share_financials", "a_share_index_members", "list_projects", "use_project",
}


def test_tool_set_has_no_human_only_actions():
    names = {t.name for t in asyncio.run(mcp_server.server.list_tools())}
    assert names == EXPECTED
    assert not {"accept_changeset", "reject_changeset", "accept_type"} & names


def test_ontology_errors_reach_the_agent(conn, monkeypatch):
    s = OntologyStore(conn, actor="mcp")
    s.sync_seed_types()
    monkeypatch.setattr(mcp_server, "_store", s)
    with pytest.raises(ToolError, match="unknown object type"):
        mcp_server.upsert_node("Planet", "Mars", {"kind": "llm_prior"}, "x")
    r = asyncio.run(mcp_server.server.call_tool(
        "upsert_node",
        {"type": "Asset", "name": "BTC", "provenance": {"kind": "user"}, "rationale": "core", "props": {"symbol": "BTC"}},
    ))
    assert not r.is_error
    assert s.search_nodes("btc")[0]["name"] == "BTC"
