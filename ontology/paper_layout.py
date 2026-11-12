"""The 原文 view of a MinerU conversion: the paper page by page as typed blocks in reading order (titles, paragraphs,
figures, charts, tables, equations, footnotes, references), for the page to lay out with text nodes, never markup.

The source is structured_content.json ({"pages": [{"page_idx", "blocks": [...]}]}). A conversion without it, or with
it in another shape, falls back to markdown.md as one run of blocks without page numbers. Inline text comes as runs,
[[kind, text], ...] with kind "" (plain), "b", "sup", "sub" or "math" (LaTeX source); tables come as rows of cells.
"""

from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from . import mineru

TITLES = {"doc_title", "paragraph_title", "title"}
SKIP = {"page_number", "header", "footer", "page_header", "page_footer", "aside_text", "discarded"}
FIGURES = {"image": "image", "figure": "image", "chart": "chart", "table": "table"}
EQUATIONS = {"equation", "interline_equation", "formula"}
NOTES = {"page_footnote", "footnote"}
REFS = {"ref_text", "reference"}
CODE = {"code", "algorithm"}

MAX_BLOCKS = 5000
MAX_TEXT = 20_000  # characters in one block
MAX_ROWS, MAX_COLS = 400, 60

INLINE = re.compile(r"\*\*(.+?)\*\*|<sup>(.*?)</sup>|<sub>(.*?)</sub>|(?<![\\$])\$(?!\$)([^\n$]+?)(?<!\\)\$", re.S | re.I)
TAG = re.compile(r"<[^<>]{0,1000}>")
ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|~<>])")
IMAGE_LINK = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?\s*\)")


def _plain(s: str) -> str:
    return html.unescape(ESCAPE.sub(r"\1", TAG.sub("", s)))


def runs(text: str) -> list[list[str]]:
    """'a **b** $x^2$ c<sup>1</sup>' -> [["", "a "], ["b", "b"], ["", " "], ["math", "x^2"], ["", " c"], ["sup", "1"]]."""
    out: list[list[str]] = []

    def add(kind: str, s: str) -> None:
        s = s.strip() if kind == "math" else _plain(s)
        if not s:
            return
        if out and out[-1][0] == kind == "":
            out[-1][1] += s
        else:
            out.append([kind, s])

    last = 0
    for m in INLINE.finditer(text):
        add("", text[last:m.start()])
        bold, sup, sub, math = m.groups()
        if bold is not None:
            add("b", bold)
        elif sup is not None:
            add("sup", sup)
        elif sub is not None:
            add("sub", sub)
        else:
            add("math", math)
        last = m.end()
    add("", text[last:])
    return out


class _HtmlTable(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict[str, Any]]] = []
        self.cell: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th"):
            if not self.rows:
                self.rows.append([])
            self.cell = {"text": "", **({"head": True} if tag == "th" else {})}
            for key, value in attrs:
                if key in ("colspan", "rowspan") and (value or "").strip().isdigit() and int(value) > 1:
                    self.cell[key] = min(int(value), MAX_COLS)
            self.rows[-1].append(self.cell)
        elif tag == "br" and self.cell is not None:
            self.cell["text"] += "\n"

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th"):
            self.cell = None

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell["text"] += data


def _tidy(s: str) -> str:
    return "\n".join(" ".join(line.split()) for line in s.split("\n")).strip()


def table(content: str) -> list[list[dict[str, Any]]] | None:
    """An HTML or Markdown (pipe) table as rows of {text, head?, colspan?, rowspan?}; None if it is neither."""
    content = (content or "").strip()
    if re.search(r"<table\b", content, re.I):
        parser = _HtmlTable()
        try:
            parser.feed(content)
            parser.close()
        except Exception:  # noqa: BLE001  (HTMLParser is lenient; anything else means it is not a table we can read)
            return None
        rows = [[{**c, "text": _plain(_tidy(c["text"]))} for c in r[:MAX_COLS]] for r in parser.rows[:MAX_ROWS] if r]
        return rows or None
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if len(lines) < 2 or not all(line.startswith("|") for line in lines):
        return None
    rows = []
    split = [[c.strip() for c in re.split(r"(?<!\\)\|", line.strip("|"))] for line in lines]
    separator = [all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c) and any(cells) for cells in split]
    for i, cells in enumerate(split[: MAX_ROWS + 1]):
        if separator[i]:
            continue
        head = i == 0 and len(split) > 1 and separator[1]
        rows.append([{"text": _plain(c), **({"head": True} if head else {})} for c in cells[:MAX_COLS]])
    return rows or None


def _texts(v: Any) -> str:
    """Captions and footnotes come as strings, {content} objects or lists of either."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return _texts(v.get("content") or v.get("text") or "")
    if isinstance(v, list):
        return "\n".join(t for t in (_texts(x) for x in v) if t.strip())
    return ""


def _image(b: dict[str, Any]) -> str | None:
    content = b.get("content")
    for holder in (b, content if isinstance(content, dict) else {}):
        for key in ("image_source", "img_path", "image_path", "path"):
            v = holder.get(key)
            if isinstance(v, dict):
                v = v.get("path")
            if isinstance(v, str) and mineru.IMAGE.fullmatch(Path(v).name):
                return Path(v).name
    return None


def _figure_kind(name: str | None, kind: str) -> str:
    if name and kind == "image":
        return "table" if "_table" in name else "chart" if "_chart" in name else "image"
    return kind


def _block(b: dict[str, Any], ids: dict[str, str]) -> dict[str, Any] | None:
    kind = str(b.get("type") or "").lower()
    content = b.get("content")
    c = content if isinstance(content, dict) else {}
    text = content if isinstance(content, str) else _texts(content) if isinstance(content, list) else _texts(c.get("text"))
    text = text[:MAX_TEXT]
    if kind in SKIP:
        return None
    if kind in FIGURES:
        name = _image(b)
        caption = b.get("captions") or b.get("caption") or c.get(f"{kind}_caption") or c.get("caption")
        notes = b.get("footnotes") or c.get(f"{kind}_footnote") or c.get("footnote")
        out: dict[str, Any] = {"t": "figure", "kind": _figure_kind(name, FIGURES[kind]), "name": name,
                               "id": ids.get(name or ""), "caption": runs(_texts(caption)[:3000]),
                               "notes": runs(_texts(notes)[:3000])}
        if kind == "table" and (rows := table(text or _texts(c.get("table_body")) or _texts(c.get("html")))):
            out["rows"] = rows
        return out if name or out.get("rows") or out["caption"] else None
    if kind in EQUATIONS:
        latex = text.strip().strip("$").strip()
        name = _image(b)
        return {"t": "equation", "latex": latex, "name": name} if latex or name else None
    if not text.strip():
        return None
    if kind in TITLES:
        try:
            level = int(b.get("level") or 2)
        except (TypeError, ValueError):
            level = 2
        return {"t": "title", "level": 1 if kind == "doc_title" else min(max(level, 2), 4), "runs": runs(text)}
    if kind in CODE:
        return {"t": "code", "text": text}
    if (rows := table(text)) is not None:
        return {"t": "figure", "kind": "table", "name": None, "id": None, "caption": [], "notes": [], "rows": rows}
    return {"t": "note" if kind in NOTES else "ref" if kind in REFS else "text", "runs": runs(text)}


def _from_markdown(md: str, ids: dict[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    pending: str | None = None  # a "Table 2: …" caption waiting for the table below it

    def flush() -> None:
        nonlocal pending
        if pending:
            out.append({"t": "text", "runs": runs(pending)})
        pending = None

    for para in re.split(r"\n[ \t]*\n", md):
        s = para.strip()[:MAX_TEXT]
        if not s:
            continue
        figure: dict[str, Any] | None = None
        if m := re.fullmatch(r"(#{1,6})\s+(.+)", s):
            flush()
            n = len(m.group(1))
            out.append({"t": "title", "level": 1 if n == 1 and not out else min(max(n, 2), 4), "runs": runs(m.group(2))})
            continue
        if (images := IMAGE_LINK.findall(s)) and not IMAGE_LINK.sub("", s).strip():
            for src in images:
                name = Path(src).name if mineru.IMAGE.fullmatch(Path(src).name) else None
                figure = {"t": "figure", "kind": _figure_kind(name, "image"), "name": name, "id": ids.get(name or ""),
                          "caption": [], "notes": []}
                out.append(figure)
        elif s.startswith("$$") and s.endswith("$$") and len(s) > 4:
            flush()
            out.append({"t": "equation", "latex": s[2:-2].strip(), "name": None})
            continue
        elif rows := table(s):
            figure = {"t": "figure", "kind": "table", "name": None, "id": None, "caption": [], "notes": [], "rows": rows}
            out.append(figure)
        elif mineru.CAPTION.match(s):
            prev = out[-1] if out else None
            if prev and prev["t"] == "figure" and not prev["caption"] and not re.match(r"\s*(?:\*\*)?\s*(table|tab\.|表)", s, re.I):
                prev["caption"] = runs(s)
            else:
                flush()
                pending = s
            continue
        else:
            flush()
            out.append({"t": "text", "runs": runs(s)})
            continue
        if pending and figure is not None:
            figure["caption"] = runs(pending)
            pending = None
    flush()
    return out


def layout(sha: str) -> dict[str, Any]:
    """{source: "structured" | "markdown", pages: [{page (from 1, or None), blocks}], truncated}. Raises
    OntologyError if this PDF has not been converted."""
    ids = {f["name"]: f["id"] for f in mineru.figures(sha)}  # the F1 / T1 numbering the 精读 tab uses
    try:
        data = json.loads((mineru.folder(sha) / "structured_content.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    pages_in = data.get("pages") if isinstance(data, dict) else None
    pages: list[dict[str, Any]] = []
    if isinstance(pages_in, list) and pages_in:
        source = "structured"
        for i, p in enumerate(pages_in):
            if not isinstance(p, dict):
                continue
            idx = p.get("page_idx", i)
            blocks = [x for x in (_block(b, ids) for b in p.get("blocks") or [] if isinstance(b, dict)) if x]
            if blocks:
                pages.append({"page": idx + 1 if isinstance(idx, int) else None, "blocks": blocks})
    else:
        source = "markdown"
        pages = [{"page": None, "blocks": _from_markdown(mineru.markdown(sha), ids)}]
    total, truncated, kept = 0, False, []
    for p in pages:
        room = MAX_BLOCKS - total
        if room <= 0:
            truncated = True
            break
        if len(p["blocks"]) > room:
            p = {**p, "blocks": p["blocks"][:room]}
            truncated = True
        total += len(p["blocks"])
        kept.append(p)
    return {"source": source, "pages": kept, "truncated": truncated}
