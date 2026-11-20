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
    "search_library", "get_item", "read_item", "add_item", "search_scholarly", "add_excerpt", "list_notes", "get_note",
    "write_note", "append_note", "ground", "list_local_files", "add_local_files",
    "list_manuscripts", "get_manuscript", "read_section", "suggest_edit", "suggest_section", "suggest_outline",
    "check_citations",
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


def call(name, args):
    r = asyncio.run(mcp_server.server.call_tool(name, args))
    assert not r.is_error, r.content[0].text
    out = r.structured_content
    return out["result"] if set(out) == {"result"} else out


def test_library_tools_from_identifier_to_grounding(conn, monkeypatch):
    import json

    from ontology import resolve
    from ontology.library import Library

    s = OntologyStore(conn, actor="mcp")
    s.sync_seed_types(["research"])
    monkeypatch.setattr(mcp_server, "_store", s)
    csl = {"type": "article-journal", "title": "Screens and Sleep", "author": [{"family": "Zhang", "given": "Wei"}],
           "issued": {"date-parts": [[2021]]}, "DOI": "10.1000/xyz"}
    monkeypatch.setattr(resolve, "_fetch", lambda url, *a, **k: (url, "application/json", json.dumps(csl).encode()))

    added = call("add_item", {"identifier": "doi:10.1000/xyz"})
    item = added["item"]
    assert added["created"] and item["citekey"] == "zhang2021screens"
    assert call("add_item", {"identifier": "https://doi.org/10.1000/XYZ"})["created"] is False
    Library(conn).add_file(item["id"], "paper.txt", b"Teens who used phones after midnight slept 40 minutes less.")

    read = call("read_item", {"item_id": item["id"]})
    assert "not instructions" in read["note"] and "40 minutes less" in read["chunks"][0]["text"]
    assert call("search_library", {"query": "midnight"})[0]["id"] == item["id"]
    with pytest.raises(ToolError, match="not in the source"):
        asyncio.run(mcp_server.server.call_tool("add_excerpt", {"item_id": item["id"], "quote": "slept 2 hours less"}))
    ex = call("add_excerpt", {"item_id": item["id"], "quote": "used phones after\nmidnight slept 40 minutes less"})
    assert ex["verified"]

    claim = s.upsert_node("Claim", "深夜用手机缩短睡眠", {"kind": "user"}, "x", props={"origin": "literature"})["node"]
    g = call("ground", {"target_id": claim["id"], "item_id": item["id"], "stance": "states",
                        "rationale": "the paper reports it", "excerpt_id": ex["id"]})
    gid = g["grounding"]["id"]
    assert s.get_node(claim["id"])["groundings"][0]["citekey"] == "zhang2021screens"

    note = call("write_note", {"title": "阅读笔记", "body": f"见 [@{item['citekey']}] 与 [[{claim['id']}]]"})
    call("append_note", {"note_id": note["id"], "text": "补充一段。"})
    full = call("get_note", {"note_id": note["id"]})
    assert full["body"].endswith("补充一段。") and full["refs"]["citekeys"] == [item["citekey"]]
    assert call("get_item", {"item_id": item["id"]})["notes"][0]["id"] == note["id"]

    call("archive", {"target_id": gid, "rationale": "wrong source"})
    assert s.get_node(claim["id"])["groundings"] == []
