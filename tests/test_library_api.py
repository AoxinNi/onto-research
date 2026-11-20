import json

import pytest
from fastapi.testclient import TestClient

from ontology import mcp_server, projects, resolve
from ontology.store import OntologyStore
from tests.conftest import TEST_URL
from tests.fake_claude import FakeClaude
from tests.pdfgen import make_pdf
from workbench import server
from workbench.agent import WorkbenchAgent


@pytest.fixture
def client(conn, monkeypatch):
    projects.create(conn, "睡眠研究", project_id=projects.DEFAULT, pack="research")
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    monkeypatch.setattr(mcp_server, "_store", None)
    monkeypatch.setattr(mcp_server, "_stores", {})
    monkeypatch.setattr(server, "make_agent", lambda: WorkbenchAgent(client=FakeClaude()))
    with TestClient(server.app) as c:
        yield c


CSL = {"type": "article-journal", "title": "Screens and Sleep", "author": [{"family": "Zhang", "given": "Wei"}],
       "issued": {"date-parts": [[2021]]}, "DOI": "10.1000/xyz"}


def doi_service(monkeypatch, csl=CSL):
    monkeypatch.setattr(resolve, "_fetch", lambda url, *a, **k: (url, "application/json", json.dumps(csl).encode()))


def test_items_files_text_and_excerpts(client, monkeypatch):
    doi_service(monkeypatch)
    r = client.post("/api/library", json={"identifier": "10.1000/xyz", "tags": ["睡眠"]})
    assert r.status_code == 200, r.text
    item = r.json()["item"]
    assert item["citekey"] == "zhang2021screens" and item["tags"] == ["睡眠"]
    assert client.post("/api/library", json={"identifier": "10.1000/XYZ"}).json()["created"] is False
    assert client.post("/api/library", json={"identifier": "nonsense words"}).status_code == 422

    pdf = make_pdf(["Screens and sleep.\nTeens slept less after phone use.", "Page two about melatonin."])
    r = client.post(f"/api/library/{item['id']}/files", content=pdf,
                    headers={"X-Filename": "zhang%202021.pdf", "Content-Type": "application/pdf"})
    assert r.status_code == 200, r.text
    f = r.json()["file"]
    assert f["filename"] == "zhang 2021.pdf" and f["pages"] == 2

    got = client.get(f"/api/library/files/{f['id']}")
    assert got.content == pdf and got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["content-disposition"].startswith("attachment;") and "filename*=UTF-8''zhang%202021.pdf" in \
        got.headers["content-disposition"]
    assert "sandbox" in got.headers["content-security-policy"]

    text = client.get(f"/api/library/{item['id']}/text", params={"page_from": 2}).json()
    assert [c["page"] for c in text["chunks"]] == [2]
    assert client.get("/api/library/search", params={"q": "melatonin"}).json()[0]["passages"][0]["page"] == 2

    ex = client.post(f"/api/library/{item['id']}/excerpts", json={"quote": "Teens slept less", "page": 1}).json()
    assert ex["verified"]
    assert client.patch(f"/api/excerpts/{ex['id']}", json={"comment": "关键"}).json()["comment"] == "关键"

    listing = client.get("/api/library").json()
    assert listing["total"] == 1 and listing["items"][0]["files"] == 1 and listing["tags"][0]["tag"] == "睡眠"
    patched = client.patch(f"/api/library/{item['id']}", json={"reading": "read"}).json()
    assert patched["reading"] == "read"
    assert client.get("/api/library/it_000000000000").status_code == 404


def test_file_size_cap(client, monkeypatch):
    monkeypatch.setenv("WORKBENCH_LIBRARY_FILE_MB", "1")
    r = client.post("/api/library/upload", content=b"%PDF-" + b"x" * (1024 * 1024 + 10), headers={"X-Filename": "big.pdf"})
    assert r.status_code == 413


def test_upload_uses_the_doi_printed_in_the_pdf(client, monkeypatch):
    doi_service(monkeypatch)
    pdf = make_pdf(["Screens and Sleep\nhttps://doi.org/10.1000/xyz\nAbstract text."])
    r = client.post("/api/library/upload", content=pdf, headers={"X-Filename": "download.pdf"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["detected_doi"] == "10.1000/xyz" and not out["needs_metadata"]
    assert out["item"]["title"] == "Screens and Sleep" and out["item"]["citekey"] == "zhang2021screens"
    again = client.post("/api/library/upload", content=pdf + b"\n", headers={"X-Filename": "copy.pdf"}).json()
    assert again["existing"] and again["item"]["id"] == out["item"]["id"]
    assert client.get("/api/library").json()["total"] == 1
    assert len(client.get(f"/api/library/{out['item']['id']}").json()["files"]) == 2

    plain = client.post("/api/library/upload", content="My notes on sleep".encode(),
                        headers={"X-Filename": "notes.md"}).json()
    assert plain["needs_metadata"] and plain["item"]["title"] == "notes"


def test_import_and_bibtex_export(client):
    bib = "@article{li2020, author={Li, Ming}, title={Blue light at night}, journal={Sleep}, year={2020}}"
    r = client.post("/api/library/import", content=bib.encode(), headers={"X-Filename": "refs.bib"})
    assert r.status_code == 200, r.text
    assert r.json()["format"] == "bibtex" and r.json()["added"][0]["citekey"] == "li2020"
    exported = client.get("/api/library/export.bib")
    assert "@article{li2020," in exported.text and "attachment" in exported.headers["content-disposition"]


def test_notes_groundings_and_graph(client, conn):
    item = client.post("/api/library", json={"csl": {"title": "Why We Sleep", "type": "book",
                                                     "author": [{"family": "Walker"}]}}).json()["item"]
    with projects.project_connect(projects.DEFAULT, TEST_URL) as pc:
        s = OntologyStore(pc, actor="human")
        claim = s.upsert_node("Claim", "睡眠影响记忆", {"kind": "user"}, "x", props={"origin": "literature"})["node"]
        cs = s.submit_changeset("x")
        s.accept_changeset(cs["id"])

    note = client.post("/api/notes", json={"title": "读书", "body": f"[@{item['citekey']}] 支持 [[{claim['id']}]]"}).json()
    assert client.get("/api/notes/backlinks", params={"item_id": item["id"]}).json()[0]["id"] == note["id"]
    assert client.get("/api/notes/backlinks", params={"node_id": claim["id"]}).json()[0]["id"] == note["id"]
    assert client.patch(f"/api/notes/{note['id']}", json={"pinned": True}).json()["pinned"] is True

    with client.websocket_connect("/ws") as ws:
        ws.receive_json(), ws.receive_json()
        r = client.post("/api/groundings", json={"target_id": claim["id"], "item_id": item["id"], "stance": "supports",
                                                 "rationale": "第三章"})
        assert r.status_code == 200, r.text
        events = [ws.receive_json(), ws.receive_json()]
    graph = next(e for e in events if e["type"] == "graph")["graph"]
    assert graph["groundings"][claim["id"]] == {"supports": 1, "pending": False}

    gid = r.json()["grounding"]["id"]
    assert client.get("/api/groundings", params={"target_id": claim["id"]}).json()[0]["id"] == gid
    assert client.delete(f"/api/library/{item['id']}").status_code == 409  # still grounds a claim
    assert client.delete(f"/api/groundings/{gid}").status_code == 200
    assert client.get("/api/graph").json()["graph"]["groundings"] == {}
    assert client.delete(f"/api/library/{item['id']}").status_code == 200


def test_chat_context_and_project_export_round_trip(client, conn):
    item = client.post("/api/library", json={"csl": {"title": "Why We Sleep", "type": "book"},
                                             "citekey": "walker2017"}).json()["item"]
    line = server.library_api.chat_context(projects.DEFAULT, {"item_id": item["id"], "page": 3, "node_id": 7})
    assert line.startswith("（我正在看：文献") and "[@walker2017]" in line and "第 3 页" in line
    assert server.library_api.chat_context(projects.DEFAULT, {"item_id": "it_000000000000"}) == ""

    with projects.project_connect(projects.DEFAULT, TEST_URL) as pc:
        s = OntologyStore(pc, actor="human")
        claim = s.upsert_node("Claim", "睡眠影响记忆", {"kind": "user"}, "x", props={"origin": "literature"})["node"]
        cs = s.submit_changeset("x")
        s.accept_changeset(cs["id"])
    ex = client.post(f"/api/library/{item['id']}/excerpts", json={"quote": "sleep is the single most effective thing"}).json()
    client.post("/api/groundings", json={"target_id": claim["id"], "item_id": item["id"], "stance": "states",
                                         "rationale": "书中原话", "excerpt_id": ex["id"]})
    client.post("/api/notes", json={"title": "笔记", "body": f"见 [[{claim['id']}]] 与 [[{ex['id']}]]"})

    doc = projects.export(conn, projects.DEFAULT)
    assert [i["citekey"] for i in doc["library"]["items"]] == ["walker2017"]
    copy = projects.import_doc(conn, doc, "副本")
    with projects.project_connect(copy["id"], TEST_URL) as pc:
        s = OntologyStore(pc, actor="human")
        [node] = s.search_nodes("睡眠影响记忆")
        [g] = s.groundings(target_id=node["id"])
        assert g["citekey"] == "walker2017" and g["quote"] == "sleep is the single most effective thing"
        body = pc.execute("SELECT body FROM notes").fetchone()["body"]
        assert node["id"] in body and g["excerpt_id"] in body and claim["id"] not in body
