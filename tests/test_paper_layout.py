"""The 原文 layout of a MinerU conversion: per-page blocks from structured_content.json, the Markdown fallback,
inline runs and tables. The shapes follow a real mineru.net (V1 Tasks API) result."""

import json

import pytest

from ontology import mineru, paper_layout
from ontology.errors import OntologyError

SHA = "a" * 64

MARKDOWN = """# Walking Down the Memory Maze

![](images/page_1_image_0.jpg)

Figure 1: The two-stage procedure.

![](images/page_6_chart_2.jpg)

![](images/page_6_chart_3.jpg)

Figure 2: Performance by context length.
"""

STRUCTURED = {"pages": [
    {"page_idx": 0, "blocks": [
        {"type": "aside_text", "content": "arXiv:2310.05029v1"},
        {"type": "doc_title", "level": 1, "content": "WALKING DOWN THE MEMORY MAZE"},
        {"type": "paragraph_title", "level": 2, "content": "1 INTRODUCTION"},
        {"type": "text", "content": "A tree $\\mathcal{T}(x)$ of **summaries**<sup>1</sup> &amp; more \\* <span class=\"x\">here</span>."},
        {"type": "page_footnote", "content": "<sup>\\*</sup>Work done at Meta AI."},
        {"type": "page_number", "content": "1"},
    ]},
    {"page_idx": 1, "blocks": [
        {"type": "image", "content": "", "image_source": "images/page_1_image_0.jpg",
         "captions": [{"content": "Figure 1: The two-stage procedure."}], "footnotes": []},
    ]},
    {"page_idx": 5, "blocks": [
        {"type": "table", "content": "| | QuALITY | GovReport |\n| --- | --- | --- |\n| MemWalker | 67.4 | 59.4 |",
         "image_source": "images/page_5_table_1.jpg", "captions": [{"content": "Table 2: Results."}]},
        {"type": "table", "content": "<table><tr><td colspan=\"2\">Trajectory</td></tr><tr><td>summ 9</td><td>a<br>b</td></tr></table>",
         "captions": []},
        {"type": "chart", "content": "", "image_source": "images/page_6_chart_2.jpg", "captions": []},
        {"type": "chart", "content": "", "image_source": {"path": "images/page_6_chart_3.jpg"},
         "captions": [{"content": "Figure 2: Performance by context length."}]},
        {"type": "interline_equation", "content": "$$ r = f(x) $$"},
        {"type": "ref_text", "content": "Ainslie et al. Colt5. 2023."},
        {"type": "chart", "content": "", "image_source": "../../etc/passwd", "captions": []},
    ]},
    {"page_idx": 6, "blocks": [{"type": "page_number", "content": "7"}]},
]}


@pytest.fixture
def converted():
    base = mineru.folder(SHA)
    (base / "images").mkdir(parents=True)
    (base / "markdown.md").write_text(MARKDOWN, encoding="utf-8")
    (base / "structured_content.json").write_text(json.dumps(STRUCTURED), encoding="utf-8")
    return base


def test_structured_pages_keep_text_figures_and_tables_in_order(converted):
    out = paper_layout.layout(SHA)
    assert out["source"] == "structured" and not out["truncated"]
    assert [p["page"] for p in out["pages"]] == [1, 2, 6]  # the empty last page is dropped
    first = out["pages"][0]["blocks"]
    assert [b["t"] for b in first] == ["title", "title", "text", "note"]  # arXiv stamp and page number skipped
    assert first[0]["level"] == 1 and first[1]["level"] == 2
    assert first[2]["runs"] == [["", "A tree "], ["math", "\\mathcal{T}(x)"], ["", " of "], ["b", "summaries"],
                                ["sup", "1"], ["", " & more * here."]]
    assert first[3]["runs"] == [["sup", "*"], ["", "Work done at Meta AI."]]

    fig = out["pages"][1]["blocks"][0]
    assert fig == {"t": "figure", "kind": "image", "name": "page_1_image_0.jpg", "id": "F1",
                   "caption": [["", "Figure 1: The two-stage procedure."]], "notes": []}

    table, html, chart_a, chart_b, eq, ref = out["pages"][2]["blocks"]
    assert table["kind"] == "table" and table["name"] == "page_5_table_1.jpg"
    assert table["rows"] == [[{"text": "", "head": True}, {"text": "QuALITY", "head": True}, {"text": "GovReport", "head": True}],
                             [{"text": "MemWalker"}, {"text": "67.4"}, {"text": "59.4"}]]
    assert html["rows"] == [[{"text": "Trajectory", "colspan": 2}], [{"text": "summ 9"}, {"text": "a\nb"}]]
    assert html["name"] is None
    assert (chart_a["id"], chart_a["caption"]) == ("F2", []) and chart_b["name"] == "page_6_chart_3.jpg"
    assert chart_b["caption"] == [["", "Figure 2: Performance by context length."]]
    assert eq == {"t": "equation", "latex": "r = f(x)", "name": None}
    assert ref["t"] == "ref"  # and the chart pointing outside images/ is dropped


def test_markdown_fallback_when_there_are_no_pages(converted):
    (converted / "structured_content.json").write_text("[]", encoding="utf-8")
    out = paper_layout.layout(SHA)
    assert out["source"] == "markdown" and out["pages"][0]["page"] is None
    blocks = out["pages"][0]["blocks"]
    assert [b["t"] for b in blocks] == ["title", "figure", "figure", "figure"]
    assert blocks[1]["caption"] == [["", "Figure 1: The two-stage procedure."]]
    assert blocks[2]["caption"] == [] and blocks[3]["caption"][0][1].startswith("Figure 2")


def test_markdown_table_captions_attach_below_and_tables_parse():
    md = "Table 1: Scores.\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n$$x^2$$\n\nTable 9: dangling caption.\n\nText."
    blocks = paper_layout._from_markdown(md, {})
    assert blocks[0]["t"] == "figure" and blocks[0]["caption"] == [["", "Table 1: Scores."]]
    assert blocks[0]["rows"][1] == [{"text": "1"}, {"text": "2"}]
    assert blocks[1] == {"t": "equation", "latex": "x^2", "name": None}
    assert [b["t"] for b in blocks[2:]] == ["text", "text"]  # a caption with no table after it stays as text


def test_not_converted():
    with pytest.raises(OntologyError):
        paper_layout.layout("b" * 64)
