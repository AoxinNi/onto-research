import asyncio
import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from ontology import export, mcp_server, projects
from ontology.library import Library
from ontology.store import OntologyStore
from tests.conftest import TEST_URL
from tests.fake_claude import FakeClaude
from workbench import server
from workbench.agent import WorkbenchAgent

PAPERS = [
    {"type": "article-journal", "title": "睡眠剥夺对工作记忆的影响", "author": [{"literal": "张三"}, {"literal": "李四"}],
     "container-title": "心理学报", "issued": {"date-parts": [[2023]]}, "volume": "55", "issue": "3", "page": "301-312"},
    {"type": "book", "title": "Why We Sleep", "author": [{"family": "Walker", "given": "Matthew"}],
     "publisher": "Scribner", "publisher-place": "New York", "issued": {"date-parts": [[2017]]}},
]
needs_pandoc = pytest.mark.skipif(export.pandoc_path() is None, reason="pandoc is not installed")


@pytest.fixture
def client(conn, monkeypatch):
    projects.create(conn, "睡眠研究", project_id=projects.DEFAULT, pack="research")
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    monkeypatch.setattr(mcp_server, "_store", None)
    monkeypatch.setattr(mcp_server, "_stores", {})
    monkeypatch.setattr(server, "make_agent", lambda: WorkbenchAgent(client=FakeClaude()))
    with TestClient(server.app) as c:
        yield c


def keys(client):
    with projects.project_connect(projects.DEFAULT, TEST_URL) as pc:
        lib = Library(pc)
        return [lib.add_item(p, {"kind": "manual"})["item"]["citekey"] for p in PAPERS]


def paper(client, body):
    m = client.post("/api/manuscripts", json={"title": "睡眠与记忆", "meta": {"authors": [{"name": "王五"}],
                                                                          "abstract": "综述。", "keywords": ["睡眠"]}}).json()
    sec = m["sections"][0]
    saved = client.patch(f"/api/sections/{sec['id']}", json={"base_rev": sec["rev"], "body": body})
    assert saved.status_code == 200, saved.text
    return m, saved.json()


def test_editing_conflicts_and_suggestions_over_http(client):
    listing = client.get("/api/manuscripts").json()
    assert listing["manuscripts"] == [] and {s["id"] for s in listing["styles"]} >= {"gb7714-numeric", "apa"}
    m, saved = paper(client, "第一稿")
    sec_id = saved["id"]
    stale = client.patch(f"/api/sections/{sec_id}", json={"base_rev": 1, "body": "另一页的旧稿"})
    assert stale.status_code == 409
    assert stale.json()["conflict"] is True and stale.json()["current"]["body"] == "第一稿"
    assert client.patch(f"/api/sections/{sec_id}", json={"base_rev": 2, "body": "x" * 200_001}).status_code == 409
    assert client.get(f"/api/sections/{sec_id}/revisions").json()[0]["body"] == "第一稿"

    added = client.post(f"/api/manuscripts/{m['id']}/sections", json={"heading": "附录", "level": 2}).json()
    assert client.delete(f"/api/sections/{added['id']}").json()["deleted"] == added["id"]
    assert client.patch(f"/api/manuscripts/{m['id']}", json={"csl_style": "nope"}).status_code == 409
    assert client.patch(f"/api/manuscripts/{m['id']}", json={"csl_style": "apa"}).json()["csl_style"] == "apa"

    with projects.project_connect(projects.DEFAULT, TEST_URL) as pc:
        from ontology.manuscripts import Manuscripts
        g = Manuscripts(pc, actor="workbench-agent").suggest_edit(sec_id, 2, "Agent 改写的第一稿", "更通顺")
    assert client.get(f"/api/manuscripts/{m['id']}").json()["suggestions"][0]["id"] == g["id"]
    assert client.post(f"/api/suggestions/{g['id']}/accept").status_code == 200
    assert client.get(f"/api/sections/{sec_id}").json()["body"] == "Agent 改写的第一稿"
    assert client.post(f"/api/suggestions/{g['id']}/reject").status_code == 409
    assert client.get("/api/manuscripts/ms_000000000000").status_code == 404


def test_the_agent_reads_and_suggests_but_cannot_write(client, conn):
    m, saved = paper(client, "原文")
    with projects.project_connect(projects.DEFAULT, TEST_URL) as pc:
        mcp_server._store = OntologyStore(pc, actor="workbench-agent")
        try:
            outline = mcp_server.get_manuscript(m["id"])
            assert "body" not in outline["sections"][0] and outline["sections"][0]["rev"] == 2
            sec = mcp_server.read_section(saved["id"])
            with pytest.raises(ToolError, match="read_section"):
                asyncio.run(mcp_server.server.call_tool("suggest_edit", {
                    "section_id": sec["id"], "base_rev": 1, "body": "新", "rationale": "x"}))
            g = mcp_server.suggest_edit(sec["id"], sec["rev"], "新的原文", "更好")
            assert g["status"] == "pending" and g["author"] == "workbench-agent"
            assert mcp_server.check_citations(m["id"])["counts"] == {"error": 0, "warning": 0, "info": 0}
        finally:
            mcp_server._store = None
    assert client.get(f"/api/sections/{saved['id']}").json()["body"] == "原文"


@needs_pandoc
def test_exports_and_preview_references(client):
    zhang, walker = keys(client)
    m, _ = paper(client, f"[睡眠剥夺损害工作记忆]{{.claim node=n_0123456789ab}}[@{zhang}]。另见 [@{walker}, 第 12 页]。\n\n"
                         "![秘密](/etc/passwd)")
    refs = client.get(f"/api/manuscripts/{m['id']}/references").json()
    assert refs["engine"] == "pandoc" and refs["numeric"] and refs["order"] == [zhang, walker]
    assert refs["entries"][zhang]["label"] == "[1]"
    assert refs["entries"][zhang]["text"].startswith("张三, 李四. 睡眠剥夺对工作记忆的影响[J")
    assert refs["entries"][walker]["text"] == "WALKER M. Why We Sleep[M]. New York: Scribner, 2017."
    apa = client.get(f"/api/manuscripts/{m['id']}/references", params={"style": "apa"}).json()
    assert apa["entries"][walker]["label"] == "(Walker, 2017)"

    docx = client.get(f"/api/manuscripts/{m['id']}/export", params={"format": "docx"})
    assert docx.status_code == 200, docx.text
    assert "attachment" in docx.headers["content-disposition"] and ".docx" in docx.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(docx.content))
    xml = z.read("word/document.xml").decode()
    assert "参考文献" in xml and "WALKER M. Why We Sleep[M]" in xml and "睡眠剥夺损害工作记忆" in xml
    assert not [n for n in z.namelist() if n.startswith("word/media/")]  # --sandbox: no file was read in

    tex = zipfile.ZipFile(io.BytesIO(client.get(f"/api/manuscripts/{m['id']}/export", params={"format": "tex"}).content))
    assert sorted(tex.namelist()) == ["README.txt", "main.tex", "references.bib"]
    main = tex.read("main.tex").decode()
    assert "{ctexart}" in main and "style=gb7714-2015" in main and "\\addbibresource{references.bib}" in main
    assert f"\\autocite{{{zhang}}}" in main and "\\printbibliography[title=参考文献]" in main
    bib = tex.read("references.bib").decode()
    assert f"@book{{{walker}," in bib and "langid" not in bib

    md = client.get(f"/api/manuscripts/{m['id']}/export", params={"format": "md", "formatted": True}).text
    assert "睡眠剥夺损害工作记忆[1]。另见 [2]。" in md and "[2] WALKER M. Why We Sleep[M]" in md and ".claim" not in md
    plain = zipfile.ZipFile(io.BytesIO(client.get(f"/api/manuscripts/{m['id']}/export", params={"format": "md"}).content))
    assert f"[@{zhang}]" in plain.read("paper.md").decode()
    bibtex = client.get(f"/api/manuscripts/{m['id']}/export", params={"format": "bib"}).text
    assert f"@book{{{walker}," in bibtex and zhang in bibtex


def test_without_pandoc_only_markdown_and_bibtex_export(client, monkeypatch):
    zhang, walker = keys(client)
    m, _ = paper(client, f"见 [@{zhang}; @{walker}]。")
    monkeypatch.setenv("WORKBENCH_PANDOC", "/nonexistent/pandoc")
    export._preview_cache.clear()
    assert client.get("/api/manuscripts").json()["pandoc"] is False
    r = client.get(f"/api/manuscripts/{m['id']}/export", params={"format": "docx"})
    assert r.status_code == 501 and "pandoc" in r.json()["detail"]
    md = client.get(f"/api/manuscripts/{m['id']}/export", params={"format": "md", "formatted": True}).text
    assert "见 [1, 2]。" in md and "[2] WALKER M. Why We Sleep[M]. New York: Scribner, 2017." in md
    refs = client.get(f"/api/manuscripts/{m['id']}/references").json()
    assert refs["engine"] == "plain" and refs["entries"][walker]["label"] == "[2]"
