import asyncio
import os

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from ontology import localfiles, mcp_server, projects
from ontology.store import OntologyStore
from tests.pdfgen import make_pdf


def call(name, args):
    try:
        r = asyncio.run(mcp_server.server.call_tool(name, args))
    except ToolError as e:
        return True, str(e)
    out = r.structured_content
    return r.is_error, out["result"] if set(out) == {"result"} else out


@pytest.fixture
def papers(conn, monkeypatch, tmp_path):
    s = OntologyStore(conn, actor="mcp")
    s.sync_seed_types(["research"])
    monkeypatch.setattr(mcp_server, "_store", s)
    root = tmp_path / "论文库"
    (root / "01_方法").mkdir(parents=True)
    (root / ".cache").mkdir()
    (root / "01_方法" / "agent.pdf").write_bytes(make_pdf(["Report agents\nThey write reports."], title="Report Agents"))
    (root / "notes.md").write_text("# Reading notes\nKnowledge editing.", encoding="utf-8")
    (root / "slides.pptx").write_bytes(b"PK\x03\x04")
    (root / ".DS_Store").write_bytes(b"\x00")
    (root / ".cache" / "hidden.pdf").write_bytes(make_pdf(["hidden"]))
    outside = tmp_path / "private.txt"
    outside.write_text("not for the agent", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)
    monkeypatch.setenv(localfiles.ENV, str(root))
    return root, outside


def test_without_an_allowed_folder_the_agent_is_told_how_to_get_one(conn, monkeypatch):
    monkeypatch.setattr(mcp_server, "_store", OntologyStore(conn, actor="mcp"))
    monkeypatch.delenv(localfiles.ENV, raising=False)
    failed, text = call("list_local_files", {})
    assert failed and localfiles.ENV in text


def test_the_agent_lists_and_adds_papers_from_an_allowed_folder(papers):
    root, _ = papers
    failed, listing = call("list_local_files", {})
    assert not failed
    assert listing["folders"] == [str(root.resolve())]
    assert [(f["folder"], f["name"], f["in_library"]) for f in listing["files"]] == [
        (".", "notes.md", False), ("01_方法", "agent.pdf", False)]

    pdf = listing["files"][1]["path"]
    failed, added = call("add_local_files", {"paths": [pdf, "notes.md"], "tags": ["01_方法"]})
    assert not failed
    assert [a["ok"] for a in added] == [True, True]
    assert added[0]["item"]["title"] == "Report Agents" and added[0]["item"]["tags"] == ["01_方法"]
    assert added[0]["text"] == "ok" and added[0]["pages"] == 1

    failed, again = call("add_local_files", {"paths": [pdf]})
    assert again[0]["already_in_library"] and again[0]["item"]["id"] == added[0]["item"]["id"]
    _, items = call("search_library", {"query": "Report"})
    assert len(items) == 1
    _, listing = call("list_local_files", {"folder": "01_方法"})
    assert [(f["name"], f["in_library"], f["item_id"]) for f in listing["files"]] == [
        ("agent.pdf", True, added[0]["item"]["id"])]


def test_nothing_outside_the_allowed_folder_can_be_read(papers):
    root, outside = papers
    failed, added = call("add_local_files", {"paths": [
        str(outside), "../private.txt", str(root / "link.txt"), str(root / "slides.pptx"), str(root / "missing.pdf")]})
    assert not failed
    assert [a["ok"] for a in added] == [False] * 5
    assert "only PDF" in added[3]["error"]
    failed, text = call("list_local_files", {"folder": str(outside.parent)})
    assert failed and "not in a folder the user allowed" in text
    _, items = call("search_library", {"query": "agent"})
    assert items == []


def test_cloud_workspaces_never_reach_the_servers_disk(papers):
    token = projects.workspace_owner.set("user-1")
    try:
        with pytest.raises(Exception, match="only available in the local workbench"):
            localfiles.allowed_roots()
    finally:
        projects.workspace_owner.reset(token)


def test_relative_and_missing_folders_in_the_setting_are_ignored(tmp_path, monkeypatch):
    real = tmp_path / "a"
    real.mkdir()
    monkeypatch.setenv(localfiles.ENV, os.pathsep.join(["relative/dir", str(tmp_path / "nope"), str(real), str(real)]))
    assert localfiles.roots() == [real.resolve()]
