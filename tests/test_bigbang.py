import pytest

from ontology import bigbang, projects
from ontology.errors import OntologyError
from ontology.library import Library
from ontology.store import OntologyStore
from tests.conftest import TEST_URL
from tests.pdfgen import make_pdf

PDF = make_pdf(["Attention is the mechanism that relates positions of a sequence."], title="Attention")
ATTN = {"type": "article-journal", "title": "Attention Is All You Need", "DOI": "10.1000/attn",
        "author": [{"family": "Vaswani", "given": "A"}], "issued": {"date-parts": [[2017]]}}
PRIOR = {"kind": "user"}


def open_project(pid):
    pc = projects.project_connect(pid, TEST_URL)
    return pc, OntologyStore(pc, actor="test-agent"), OntologyStore(pc, actor="human"), Library(pc)


def accept(agent, human):
    human.accept_changeset(agent.submit_changeset("x")["id"])


def node(s, type, name):
    props = {"origin": "project"} if type == "Claim" else None
    return s.upsert_node(type, name, PRIOR, "x", props=props, force_new=True)["node"]["id"]


@pytest.fixture
def two(conn):
    """Two projects that share a concept and a paper, and whose citekeys clash for two different papers."""
    a = projects.create(conn, "注意力研究")
    pc, s, human, lib = open_project(a["id"])
    att = node(s, "Concept", "注意力")
    claim = node(s, "Claim", "注意力决定表现")
    s.create_link("about", claim, att, PRIOR, "x")
    accept(s, human)
    paper = lib.add_item(ATTN, {"kind": "manual"})["item"]
    lib.add_file(paper["id"], "attn.pdf", PDF)
    smith = lib.add_item({"type": "article-journal", "title": "Paper One", "author": [{"family": "Smith"}],
                          "issued": {"date-parts": [[2020]]}}, {"kind": "manual"}, citekey="smith2020")["item"]
    ex = lib.add_excerpt(paper["id"], "relates positions of a sequence", require_found=False)
    human.ground_direct(att, paper["id"], "supports", "原文", ex["id"])
    lib.write_note("A 的笔记", f"[[{att}]] 见 [@smith2020] 和 [[{paper['id']}]]")
    pc.close()

    b = projects.create(conn, "记忆研究")
    pc, s, human, lib = open_project(b["id"])
    att_b = node(s, "Concept", "注意力")
    mem = node(s, "Concept", "记忆")
    claim_b = node(s, "Claim", "记忆依赖注意力")
    s.create_link("about", claim_b, mem, PRIOR, "x")
    s.create_link("about", claim_b, att_b, PRIOR, "x")
    accept(s, human)
    paper_b = lib.add_item(ATTN, {"kind": "manual"}, tags=["经典"])["item"]
    lib.add_file(paper_b["id"], "same.pdf", PDF)
    lib.add_item({"type": "article-journal", "title": "Paper Two", "author": [{"family": "Smith"}],
                  "issued": {"date-parts": [[2020]]}}, {"kind": "manual"}, citekey="smith2020")
    lib.write_note("B 的笔记", f"[[{att_b}]] 与 [@smith2020] 不同于 [@{paper_b['citekey']}]")
    gone = lib.write_note("归档的", "x")
    lib.update_note(gone["id"], archived=True)
    pc.close()
    return {"a": a, "b": b, "att": att, "att_b": att_b, "paper": paper}


def test_merge_joins_shared_things_and_rewrites_references(conn, two):
    out = bigbang.merge(conn, [two["a"]["id"], two["b"]["id"]])
    assert out["name"] == "Big Bang · 注意力研究 + 记忆研究"
    m = out["merge"]
    assert (m["nodes"], m["nodes_joined"], m["links"]) == (4, 1, 3)
    assert (m["sources"], m["sources_joined"], m["citekeys_renamed"]) == (3, 1, 1)
    assert (m["files"], m["excerpts"], m["groundings"], m["notes"]) == (1, 1, 1, 3)
    assert out["stats"]["nodes"] == 4 and out["stats"]["links"] == 3 and out["stats"]["pending"] == 0

    pc, s, _, lib = open_project(out["id"])
    concepts = {n["name"] for n in s.search_nodes("注意力")}
    assert "注意力" in concepts
    keys = {r["title"]: r["citekey"] for r in pc.execute("SELECT title, citekey FROM library_items").fetchall()}
    assert keys["Paper One"] == "smith2020" and keys["Paper Two"] != "smith2020"
    tags = pc.execute("SELECT tags FROM library_items WHERE id = %s", (two["paper"]["id"],)).fetchone()["tags"]
    assert tags == ["经典"]
    notes = {r["title"]: r for r in pc.execute("SELECT title, body, refs, status FROM notes").fetchall()}
    assert notes["A 的笔记"]["body"] == f"[[{two['att']}]] 见 [@smith2020] 和 [[{two['paper']['id']}]]"
    assert notes["B 的笔记"]["body"] == (f"[[{two['att']}]] 与 [@{keys['Paper Two']}] 不同于 "
                                         f"[@{two['paper']['citekey']}]")  # the joined node and the renamed citekey
    assert notes["B 的笔记"]["refs"]["nodes"] == [two["att"]]
    assert notes["归档的"]["status"] == "archived"
    g = pc.execute("SELECT node_id, excerpt_id FROM groundings").fetchone()
    assert g["node_id"] == two["att"] and g["excerpt_id"]
    pc.close()


def test_merge_stores_each_pdf_once(conn, two):
    out = bigbang.merge(conn, [two["a"]["id"], two["b"]["id"]])
    assert conn.execute("SELECT count(*) AS n FROM onto_research.file_blobs").fetchone()["n"] == 1
    for pid in (two["a"]["id"], two["b"]["id"], out["id"]):  # every project reads the one copy
        pc, _, _, lib = open_project(pid)
        f = pc.execute("SELECT id, bytes FROM library_files").fetchone()
        assert f["bytes"] is None and lib.file_bytes(f["id"])["bytes"] == PDF
        assert pc.execute("SELECT count(*) AS n FROM library_chunks").fetchone()["n"] > 0
        pc.close()

    projects.drop(conn, out["id"])  # the originals still read it
    assert conn.execute("SELECT count(*) AS n FROM onto_research.file_blobs").fetchone()["n"] == 1
    for pid in (two["a"]["id"], two["b"]["id"]):
        pc, _, _, lib = open_project(pid)
        lib.delete_file(pc.execute("SELECT id FROM library_files").fetchone()["id"])
        pc.close()
    assert conn.execute("SELECT count(*) AS n FROM onto_research.file_blobs").fetchone()["n"] == 0


def test_the_originals_are_unchanged(conn, two):
    before = {p["id"]: p["stats"] for p in projects.list_all(conn)}
    bigbang.merge(conn, [two["a"]["id"], two["b"]["id"]], name="合并", description="d")
    after = {p["id"]: p["stats"] for p in projects.list_all(conn)}
    for pid in (two["a"]["id"], two["b"]["id"]):
        assert after[pid] == before[pid]
    assert {p["name"] for p in projects.list_all(conn)} == {"注意力研究", "记忆研究", "合并"}


def test_a_failed_merge_leaves_nothing_behind(conn, two, monkeypatch):
    with pytest.raises(OntologyError):
        bigbang.merge(conn, [two["a"]["id"]])
    with pytest.raises(OntologyError):
        bigbang.merge(conn, [two["a"]["id"], "no-such-project"])

    def boom(self):
        raise OntologyError("boom")
    monkeypatch.setattr(bigbang._Merge, "notes", boom)
    with pytest.raises(OntologyError, match="boom"):
        bigbang.merge(conn, [two["a"]["id"], two["b"]["id"]])
    assert len(projects.list_all(conn)) == 2
    assert conn.execute("SELECT count(*) AS n FROM onto_research.file_blobs").fetchone()["n"] == 0
    pc, _, _, _ = open_project(two["a"]["id"])  # the move of its file was rolled back
    assert pc.execute("SELECT bytes IS NOT NULL AS here FROM library_files").fetchone()["here"]
    pc.close()
