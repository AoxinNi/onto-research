"""Manuscripts: sections and revisions, the agent's suggestions, the citation check, and citekey renames."""

import pytest

from ontology import cite, projects
from ontology.errors import OntologyError
from ontology.library import Library
from ontology.manuscripts import Manuscripts, SectionConflict
from ontology.store import OntologyStore
from tests.conftest import TEST_URL

LLM = {"kind": "llm_prior"}
PAPER = {"type": "article-journal", "title": "Screen use before bed", "author": [{"family": "Zhang", "given": "Wei"}],
         "issued": {"date-parts": [[2023]]}, "container-title": "Journal of Sleep Research", "DOI": "10.1111/jsr.13800"}


@pytest.fixture
def research(conn):
    p = projects.create(conn, "睡眠")
    c = projects.project_connect(p["id"], TEST_URL)
    yield {"agent": OntologyStore(c, actor="workbench-agent"), "human": OntologyStore(c, actor="human"),
           "lib": Library(c, actor="human"), "ms": Manuscripts(c, actor="human"),
           "agent_ms": Manuscripts(c, actor="workbench-agent"), "conn": c, "project": p["id"]}
    c.close()


def test_citations_and_claims_are_found_in_markdown():
    body = ("[屏幕使用推迟入睡]{.claim node=n_0123456789ab}[@zhang2023; @li2021, 第 12 页]。"
            "@wang2020 指出相反的结果。邮件 a@b.com 不是引用。`@code` 也不是。\n\n<!-- @hidden -->")
    assert cite.citekeys(body) == ["zhang2023", "li2021", "wang2020"]
    [c] = cite.claims(body)
    assert c.node == "n_0123456789ab" and c.text == "屏幕使用推迟入睡" and c.citekeys == ["zhang2023", "li2021"]
    assert cite.word_count("睡眠 sleep onset。") == 4
    renamed = cite.rename_citekey("[@zhang2023] [@zhang2023b] `@zhang2023` @zhang2023.", "zhang2023", "zhang2023x")
    assert renamed == "[@zhang2023x] [@zhang2023b] `@zhang2023` @zhang2023x."
    nested = cite.claims("前文。[睡眠 [见 @a] 很重要]{node=n_0123456789ab .claim}，后文。")
    assert nested[0].text == "睡眠 [见 @a] 很重要" and nested[0].citekeys == ["a"]


def test_sections_save_with_revisions_and_refuse_stale_writes(research):
    ms = research["ms"]
    m = ms.create("屏幕与睡眠", kind="article")
    assert [s["heading"] for s in m["sections"]][:2] == ["引言", "相关研究"]
    intro = m["sections"][0]
    saved = ms.save_section(intro["id"], 1, body="第一稿")
    assert saved["rev"] == 2
    again = ms.save_section(intro["id"], 2, body="第一稿，改了一点")
    assert again["rev"] == 3
    revs = ms.revisions(intro["id"])
    assert [r["rev"] for r in revs] == [3, 1]  # one author's saves within five minutes are one revision
    with pytest.raises(SectionConflict) as e:
        ms.save_section(intro["id"], 2, body="另一页的旧稿")
    assert e.value.current["rev"] == 3 and e.value.current["body"] == "第一稿，改了一点"
    assert ms.save_section(intro["id"], 3)["rev"] == 3  # nothing changed, nothing written

    other = Manuscripts(research["conn"], actor="someone-else")
    other.save_section(intro["id"], 3, body="合著者的修改")
    assert [r["author"] for r in ms.revisions(intro["id"])] == ["someone-else", "human", "human"]

    added = ms.add_section(m["id"], "背景", 2, after_section_id=intro["id"])
    assert [s["heading"] for s in ms.get(m["id"])["sections"]][:3] == ["引言", "背景", "相关研究"]
    order = [{"id": s["id"]} for s in reversed(ms.get(m["id"])["sections"])]
    order[0]["level"] = 2
    out = ms.reorder(m["id"], order)
    assert out["sections"][0]["level"] == 2 and out["sections"][-1]["heading"] == "引言"
    with pytest.raises(OntologyError, match="exactly once"):
        ms.reorder(m["id"], order[:-1])
    ms.delete_section(added["id"])
    assert [s["position"] for s in ms.get(m["id"])["sections"]] == list(range(6))


def test_suggestions_are_accepted_rejected_or_go_stale(research):
    ms, agent = research["ms"], research["agent_ms"]
    m = ms.create("论文", sections=[{"heading": "引言", "body": "原文。"}])
    sec = m["sections"][0]
    with pytest.raises(OntologyError, match="read_section"):
        agent.suggest_edit(sec["id"], 7, "新文", "更清楚")
    g = agent.suggest_edit(sec["id"], 1, "改写后的引言。", "更清楚")
    assert ms.get(m["id"])["suggestions"][0]["id"] == g["id"]
    out = ms.accept(g["id"])
    assert ms.read_section(sec["id"])["body"] == "改写后的引言。"
    assert out["suggestion"]["status"] == "accepted"
    assert ms.revisions(sec["id"])[0]["author"] == "workbench-agent"
    with pytest.raises(OntologyError, match="already accepted"):
        ms.accept(g["id"])

    late = agent.suggest_edit(sec["id"], 2, "Agent 的另一版。", "再改")
    ms.save_section(sec["id"], 2, body="作者先改了。")
    assert next(s for s in ms.suggestions(m["id"]) if s["id"] == late["id"])["status"] == "stale"
    with pytest.raises(OntologyError, match="compare"):
        ms.accept(late["id"])
    ms.accept(late["id"], force=True)
    assert ms.read_section(sec["id"])["body"] == "Agent 的另一版。"

    first = agent.suggest_section(m["id"], "摘要", "本文……", "缺摘要")
    ms.accept(first["id"])
    assert ms.get(m["id"])["sections"][0]["heading"] == "摘要"
    after = agent.suggest_section(m["id"], "方法", "我们……", "缺方法", after_section_id=sec["id"], level=2)
    ms.reject(after["id"])
    assert len(ms.get(m["id"])["sections"]) == 2

    ms.add_section(m["id"], "背景", 2, after_section_id=sec["id"])
    ms.add_section(m["id"], "结论", 1)
    outline = agent.suggest_outline(m["id"], [{"heading": "题记", "level": 1},
                                              {"heading": "引言", "level": 1, "intent": "已有"},
                                              {"heading": "讨论", "level": 1, "intent": "比较 -- 两类研究"},
                                              {"heading": "结论", "level": 1}], "补全结构")
    assert outline["outline"][2]["intent"] == "比较 -- 两类研究"
    ms.accept(outline["id"])
    secs = ms.get(m["id"])["sections"]
    # new headings go after the outline's previous heading and its subsections; existing ones do not move
    assert [s["heading"] for s in secs] == ["题记", "摘要", "引言", "背景", "讨论", "结论"]
    assert ms.read_section(secs[4]["id"])["body"] == "<!-- 比较 — 两类研究 -->\n"
    assert secs[4]["words"] == 0


def test_the_citation_check(research):
    ms, lib, agent, human = research["ms"], research["lib"], research["agent"], research["human"]
    zhang = lib.add_item(PAPER, {"kind": "manual"})["item"]
    li = lib.add_item({**PAPER, "title": "Another study", "DOI": "10.1/2" + "0", "author": [{"family": "Li"}]},
                      {"kind": "manual"})["item"]
    grounded = agent.upsert_node("Claim", "屏幕推迟入睡", LLM, "x", props={"origin": "literature"})["node"]
    bare = agent.upsert_node("Claim", "蓝光抑制褪黑素", LLM, "x", props={"origin": "literature"})["node"]
    agent.submit_changeset("两个论断")
    human.accept_changeset(agent.list_changesets(status="pending")[0]["id"])
    pending = agent.upsert_node("Claim", "还在审核", LLM, "x", props={"origin": "project"})["node"]
    human.ground_direct(grounded["id"], zhang["id"], "supports", "调查数据")

    body = (f"[屏幕使用推迟入睡]{{.claim node={grounded['id']}}}[@{li['citekey']}]。"
            f"[蓝光]{{.claim node={bare['id']}}}很关键 [@nobody2020]。"
            f"[待定]{{.claim node={pending['id']}}}。[不存在]{{.claim node=n_000000000000}}。[手写错了]{{.claim node=n_x}}。")
    m = ms.create("检查", sections=[{"heading": "引言", "body": body}])
    out = ms.check(m["id"])
    codes = {(i["level"], i["code"]) for i in out["issues"]}
    assert codes == {("error", "unknown_citekey"), ("error", "pending_node"), ("error", "unknown_node"),
                     ("warning", "ungrounded_claim"), ("info", "cite_not_grounding"), ("info", "grounding_not_cited")}
    assert out["counts"] == {"error": 4, "warning": 1, "info": 2}
    assert [i["level"] for i in out["issues"]] == sorted((i["level"] for i in out["issues"]),
                                                         key=["error", "warning", "info"].index)
    not_cited = next(i for i in out["issues"] if i["code"] == "grounding_not_cited")
    assert not_cited["citekeys"] == [zhang["citekey"]]


def test_renaming_a_citekey_rewrites_papers_and_notes_and_cited_sources_stay(research):
    ms, lib = research["ms"], research["lib"]
    it = lib.add_item(PAPER, {"kind": "manual"})["item"]
    key = it["citekey"]
    m = ms.create("论文", sections=[{"heading": "引言", "body": f"如 [@{key}] 所示。"}])
    note = lib.write_note("读书笔记", f"[@{key}] 很重要")
    assert lib.get_item(it["id"])["cited_in"][0]["manuscript_id"] == m["id"]
    lib.update_item(it["id"], citekey="zhang2023sleep")
    sec = ms.read_section(m["sections"][0]["id"])
    assert sec["body"] == "如 [@zhang2023sleep] 所示。" and sec["rev"] == 2
    assert lib.get_note(note["id"])["body"] == "[@zhang2023sleep] 很重要"
    with pytest.raises(OntologyError, match="引用了这篇文献"):
        lib.archive_item(it["id"])
    ms.save_section(sec["id"], 2, body="不再引用。")
    lib.archive_item(it["id"])
    assert ms.check(m["id"])["issues"] == []


def test_a_report_becomes_a_draft_section_with_anchors_and_citations(research):
    ms, lib, agent, human = research["ms"], research["lib"], research["agent"], research["human"]
    it = lib.add_item(PAPER, {"kind": "manual"})["item"]
    claim = agent.upsert_node("Claim", "屏幕推迟入睡", LLM, "x", props={"origin": "literature"})["node"]
    concept = agent.upsert_node("Concept", "屏幕使用", LLM, "x")["node"]
    human.ground_direct(claim["id"], it["id"], "supports", "调查")
    report = {"version": 3, "title": "周报", "body": {"sections": [
        {"heading": "核心判断", "sentences": [
            {"id": "s1", "text": "睡前用屏幕会推迟入睡。", "nodes": [claim["id"]], "links": [], "from": []},
            {"id": "s2", "text": "屏幕使用[很普遍]", "nodes": [concept["id"]], "links": [], "from": []}]},
        {"heading": "其他", "sentences": [{"id": "s3", "text": "略。", "nodes": [], "links": [], "from": []}]}]}}
    m = ms.create("论文", sections=[{"heading": "引言"}])
    [g] = ms.suggest_from_report(m["id"], report, [0], m["sections"][0]["id"])
    assert g["kind"] == "insert_after" and g["author"] == "report" and g["heading"] == "核心判断"
    assert g["body"] == (f"[睡前用屏幕会推迟入睡]{{.claim node={claim['id']}}}[@{it['citekey']}]。"
                         "屏幕使用［很普遍］。")
    with pytest.raises(OntologyError, match="at least one"):
        ms.suggest_from_report(m["id"], report, [9])


def test_manuscripts_travel_with_the_project_file(conn, research):
    ms, lib, agent, human = research["ms"], research["lib"], research["agent"], research["human"]
    it = lib.add_item(PAPER, {"kind": "manual"})["item"]
    claim = human.upsert_node("Claim", "屏幕推迟入睡", {"kind": "user"}, "x", props={"origin": "literature"})["node"]
    human.submit_changeset("x")
    ms.create("带走的论文", csl_style="apa", meta={"authors": [{"name": "王五"}], "keywords": ["睡眠"]},
              sections=[{"heading": "引言", "body": f"[论断]{{.claim node={claim['id']}}}[@{it['citekey']}]。"}])
    doc = projects.export(conn, research["project"])
    assert doc["manuscripts"][0]["csl_style"] == "apa"
    copy = projects.import_doc(conn, doc, "副本")
    with projects.project_connect(copy["id"], TEST_URL) as pc:
        m2 = Manuscripts(pc)
        [paper] = m2.list()
        sec = m2.get(paper["id"])["sections"][0]
        new_claim = pc.execute("SELECT id FROM objects WHERE name = '屏幕推迟入睡'").fetchone()["id"]
        assert sec["claims"] == [new_claim] and new_claim != claim["id"]
        assert sec["citekeys"] == [it["citekey"]]
        assert paper["meta"]["keywords"] == ["睡眠"]
    assert projects.summary(conn, projects.get(conn, research["project"]))["stats"]["manuscripts"] == 1
