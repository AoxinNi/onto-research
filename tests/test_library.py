"""Library items, files and text, excerpts, notes, and groundings that tie sources into the reviewed graph."""

import pytest

from ontology import history, projects
from ontology.errors import OntologyError
from ontology.library import Library, citekey_base, fold, parse_refs
from ontology.store import OntologyStore
from tests.conftest import TEST_URL
from tests.pdfgen import make_pdf

LLM = {"kind": "llm_prior"}
PAPER = {"type": "article-journal", "title": "Screen use before bed and sleep onset in adolescents",
         "author": [{"family": "Zhang", "given": "Wei"}, {"family": "Li", "given": "Na"}],
         "issued": {"date-parts": [[2023, 4]]}, "container-title": "Journal of Sleep Research",
         "DOI": "https://doi.org/10.1111/JSR.13800"}
PAGES = ["Screen use before bed and sleep onset\nWe surveyed 1,200 students in Shanghai.\n"
         "DOI: 10.1111/jsr.13800",
         "Results\nEvening screen time predicted a later sleep onset (r = 0.32).\nThe effect was larger for infor-\n"
         "mation seeking than for messaging.",
         "Discussion\nEvening screen time predicted a later sleep onset in girls only."]


@pytest.fixture
def research(conn):
    p = projects.create(conn, "睡眠")
    c = projects.project_connect(p["id"], TEST_URL)
    yield {"agent": OntologyStore(c, actor="workbench-agent"), "human": OntologyStore(c, actor="human"),
           "lib": Library(c, actor="human"), "agent_lib": Library(c, actor="workbench-agent"), "conn": c}
    c.close()


def paper(lib, **kw):
    return lib.add_item({**PAPER, **kw}, origin={"kind": "manual"})["item"]


def test_items_get_metadata_fields_citekeys_and_doi_dedupe(research):
    lib = research["lib"]
    a = paper(lib)
    assert a["citekey"] == "zhang2023screen" and a["kind"] == "article" and a["year"] == 2023
    assert a["doi"] == "10.1111/jsr.13800" and a["authors"] == "Zhang, Wei; Li, Na" and a["added_by"] == "human"
    again = lib.add_item({**PAPER, "title": "Different title, same DOI"}, origin={"kind": "manual"})
    assert not again["created"] and again["item"]["id"] == a["id"]
    b = paper(lib, DOI=None)
    assert b["citekey"] == "zhang2023screena"
    zh = lib.add_item({"title": "青少年睡眠与屏幕使用", "author": [{"family": "张", "given": "三"}],
                       "issued": {"date-parts": [[2021]]}, "type": "thesis"}, origin={"kind": "manual"})["item"]
    assert zh["citekey"] == "张2021青少" and zh["kind"] == "thesis"
    assert citekey_base({"title": "On the origin"}) == "anonndorigin"
    with pytest.raises(OntologyError, match="title"):
        lib.add_item({"author": []}, origin={"kind": "manual"})
    with pytest.raises(OntologyError, match="already used"):
        lib.update_item(b["id"], citekey=a["citekey"])
    updated = lib.update_item(b["id"], citekey="zhang2023b", tags=["睡眠", "屏幕", "睡眠"], reading="read", kind="preprint")
    assert updated["citekey"] == "zhang2023b" and updated["tags"] == ["睡眠", "屏幕"] and updated["kind"] == "preprint"
    assert updated["csl"]["type"] == "article"
    assert lib.list_items(tag="屏幕")["total"] == 1
    assert {i["id"] for i in lib.list_items(q="屏幕")["items"]} == {b["id"], zh["id"]}  # tag or title
    assert lib.list_items(reading="read")["items"][0]["id"] == b["id"]
    assert lib.list_items(kind="thesis")["items"][0]["id"] == zh["id"]
    assert {t["tag"] for t in lib.tags()} == {"睡眠", "屏幕"}


def test_pdf_text_is_extracted_paged_and_searchable(research, monkeypatch):
    lib = research["lib"]
    item = paper(lib)
    out = lib.add_file(item["id"], "../../paper.pdf", make_pdf(PAGES, title="Screen use"), "application/pdf")
    f = out["file"]
    assert f["extract_status"] == "ok" and f["pages"] == 3 and f["filename"] == "paper.pdf"
    assert out["detected_doi"] == "10.1111/jsr.13800" and out["pdf_meta"]["title"] == "Screen use"
    assert not lib.add_file(item["id"], "copy.pdf", make_pdf(PAGES, title="Screen use"))["created"]

    page2 = lib.text(item["id"], page_from=2, page_to=2)
    assert [c["page"] for c in page2["chunks"]] == [2] and "r = 0.32" in page2["chunks"][0]["text"]
    assert [c["page"] for c in lib.text(item["id"], query="SLEEP  onset")["chunks"]] == [1, 2, 3]
    assert lib.text(item["id"], max_chars=10)["truncated"]

    hits = lib.search("later sleep onset")
    assert hits[0]["id"] == item["id"] and hits[0]["passages"][0]["page"] == 2
    assert lib.search("Zhang")[0]["id"] == item["id"]

    with pytest.raises(OntologyError, match="PDF and UTF-8"):
        lib.add_file(item["id"], "x.bin", b"\x89PNG\r\n\x1a\n\x00\xff\xfe")
    monkeypatch.setenv("WORKBENCH_LIBRARY_FILE_MB", "0")
    with pytest.raises(OntologyError, match="larger than"):
        lib.add_file(item["id"], "big.txt", b"x" * 10)


def test_excerpts_are_found_despite_spacing_hyphenation_and_width(research):
    lib, agent_lib = research["lib"], research["agent_lib"]
    item = paper(lib)
    lib.add_file(item["id"], "p.pdf", make_pdf(PAGES))
    ex = agent_lib.add_excerpt(item["id"], "larger for information   seeking than for messaging", comment="关键")
    assert ex["verified"] and ex["page"] == 2 and ex["author"] == "workbench-agent"
    chunk = lib.text(item["id"], page_from=2, page_to=2)["chunks"][0]["text"]
    assert chunk[ex["anchor"]["start"]:ex["anchor"]["end"]].startswith("larger for infor-")
    # the same sentence opens pages 2 and 3; the page given decides which one is anchored
    assert agent_lib.add_excerpt(item["id"], "Evening screen time predicted a later sleep onset", page=3)["page"] == 3
    assert agent_lib.add_excerpt(item["id"], "ＥＶＥＮＩＮＧ screen time predicted")["page"] == 2  # full-width letters
    with pytest.raises(OntologyError, match="not in the source"):
        agent_lib.add_excerpt(item["id"], "screen time causes insomnia in every adolescent")
    with pytest.raises(OntologyError, match="at least"):
        agent_lib.add_excerpt(item["id"], "sleep")
    human = lib.add_excerpt(item["id"], "a paraphrase I want to keep", page=7)
    assert not human["verified"] and human["page"] == 7 and human["anchor"] is None
    bare = paper(lib, DOI="10.1/other", title="No file")
    with pytest.raises(OntologyError, match="no extracted text"):
        agent_lib.add_excerpt(bare["id"], "anything at all here")
    assert len(lib.get_item(item["id"])["excerpts"]) == 4
    lib.delete_excerpt(human["id"])
    assert lib.update_excerpt(ex["id"], color="green")["color"] == "green"


def test_notes_keep_back_links(research):
    lib = research["lib"]
    item = paper(lib)
    refs = parse_refs("见 [@zhang2023screen, p. 3] 与 @li2020；联系 me@example.com；节点 [[n_0123456789ab]]")
    assert refs["citekeys"] == ["zhang2023screen", "li2020"] and refs["nodes"] == ["n_0123456789ab"]
    note = lib.write_note("睡眠与屏幕", f"读 [@{item['citekey']}] 的想法，另见 [[{item['id']}]]")
    assert lib.get_item(item["id"])["notes"][0]["id"] == note["id"]
    assert "补充" in lib.append_note(note["id"], "补充一句")["body"]
    lib.update_note(note["id"], archived=True)
    assert lib.list_notes() == [] and lib.get_item(item["id"])["notes"] == []


def test_groundings_are_reviewed_undone_and_recorded_in_history(research):
    agent, human, lib = research["agent"], research["human"], research["lib"]
    item = paper(lib)
    lib.add_file(item["id"], "p.pdf", make_pdf(PAGES))
    ex = lib.add_excerpt(item["id"], "Evening screen time predicted a later sleep onset (r = 0.32)")
    h = agent.upsert_node("Hypothesis", "睡前屏幕使用推迟入睡", LLM, "x")["node"]
    human.accept_changeset(agent.submit_changeset("node")["id"])
    first = history.commit_if_changed(human, "human", "节点")
    assert "groundings" not in history.snapshot(human)  # no key until there are groundings

    g = agent.ground(h["id"], item["id"], "supports", "第 2 页的相关结果", excerpt_id=ex["id"])["grounding"]
    assert g["pending"] and g["quote"].startswith("Evening") and g["citekey"] == item["citekey"]
    assert agent.ground(h["id"], item["id"], "supports", "again", excerpt_id=ex["id"])["created"] is False
    assert agent.get_node(h["id"])["groundings"][0]["id"] == g["id"]
    assert agent.graph()["groundings"][h["id"]] == {"pending": True, "supports": 1}
    cs = agent.submit_changeset("依据")
    assert cs["operations"][0]["table"] == "groundings" and "依据 supports" in cs["operations"][0]["label"]
    assert history.snapshot(human).get("groundings") is None  # pending work is not history
    human.accept_changeset(cs["id"])
    made = history.commit_if_changed(human, "human", "接受依据")
    assert made and made["stats"]["groundings_added"] == 1 and "文献依据 +1" in made["message"]
    assert first is not None

    with pytest.raises(OntologyError, match="grounds 1"):
        lib.archive_item(item["id"])
    with pytest.raises(OntologyError, match="grounds a node"):
        lib.delete_excerpt(ex["id"])
    with pytest.raises(OntologyError, match="stance"):
        agent.ground(h["id"], item["id"], "proves", "x")
    other = paper(lib, DOI="10.2/x", title="Other paper")
    with pytest.raises(OntologyError, match="not from"):
        agent.ground(h["id"], other["id"], "supports", "x", excerpt_id=ex["id"])

    # archiving the node takes its groundings along; reverting brings both back
    archived = agent.archive(h["id"], "不再需要")["archived"]
    assert g["id"] in archived and agent.groundings(target_id=h["id"]) == []
    agent.revert_changeset()
    assert [x["id"] for x in agent.groundings(target_id=h["id"])] == [g["id"]]

    # a person's own grounding is in effect at once, removed at once, and goes with a node an undo removes
    c = agent.upsert_node("Claim", "蓝光抑制褪黑素", LLM, "x", props={"origin": "literature"})["node"]
    direct = human.ground_direct(c["id"], other["id"], "states", "这篇文献提出的")["grounding"]
    assert not direct["pending"] and direct["provenance"] == {"kind": "user"}
    agent.revert_changeset()
    assert human.groundings(item_id=other["id"]) == []
    lib.archive_item(other["id"])

    # merging moves groundings to the node that stays
    h2 = agent.upsert_node("Hypothesis", "屏幕使用推迟入睡（重复）", LLM, "x")["node"]
    agent.ground(h2["id"], item["id"], "qualifies", "只在女生中")
    agent.merge_nodes(h["id"], h2["id"], "重复")
    assert {x["stance"] for x in agent.groundings(target_id=h["id"])} == {"supports", "qualifies"}


def test_a_link_can_be_grounded_and_goes_with_its_link(research):
    agent, lib = research["agent"], research["lib"]
    item = paper(lib)
    a = agent.upsert_node("Concept", "屏幕使用", LLM, "x")["node"]
    b = agent.upsert_node("Concept", "入睡时间", LLM, "x")["node"]
    link = agent.create_link("influences", a["id"], b["id"], LLM, "x", props={"direction": "positive"})["link"]
    g = agent.ground(link["id"], item["id"], "supports", "相关研究")["grounding"]
    assert g["target_id"] == link["id"] and agent.grounding_summary()[link["id"]]["supports"] == 1
    assert g["id"] in agent.archive(link["id"], "x")["archived"]
    assert lib.get_item(item["id"])["groundings"] == []


def test_fold_ignores_spacing_and_joins_hyphenation():
    folded, index = fold("Infor-\nmation  “seeking”")
    assert folded == 'information"seeking"'
    assert len(index) == len(folded)
