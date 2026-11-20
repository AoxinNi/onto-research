"""Manuscript export: DOCX, a LaTeX project, Markdown and BibTeX.

The CSL-JSON of the cited library items is the single source of the references. pandoc does the formatting:
DOCX through citeproc with the official CSL styles in ./csl (CC BY-SA 3.0, see csl/README.md), LaTeX with biblatex
so the user keeps an editable .bib. pandoc always runs with --sandbox, so a manuscript cannot make it read files or
fetch addresses (an image like ![](/etc/passwd) stays out of the document).

Without pandoc on the server, DOCX and LaTeX are unavailable (PandocMissing, HTTP 501) and Markdown and BibTeX are
built in Python, with the plain formatter of ontology.cite.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from .. import bibio, cite
from ..errors import OntologyError

HERE = Path(__file__).parent
CSL_DIR = HERE / "csl"
TEMPLATES = HERE / "templates"
REFERENCE_DOCX = TEMPLATES / "reference-zh.docx"
TIMEOUT = 90
FORMATS = ("docx", "tex", "md", "bib")
REF_TITLE = {"zh-CN": "参考文献", "en-US": "References"}
# Markdown that reads well as plain text: citations written out, no divs, spans or raw HTML
CLAIM_SPAN = re.compile(r"\[([^\[\]]*(?:\[[^\[\]]*\][^\[\]]*)*)\]\{[^{}\n]*\.claim[^{}\n]*\}")


def strip_claims(text: str) -> str:
    """Claim anchors keep their words and lose the markup."""
    return CLAIM_SPAN.sub(r"\1", text)


class PandocMissing(OntologyError):
    pass


def pandoc_path() -> str | None:
    configured = os.environ.get("WORKBENCH_PANDOC")
    if configured:
        return configured if Path(configured).is_file() else None
    return shutil.which("pandoc")


def pandoc_version() -> str | None:
    exe = pandoc_path()
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return out.splitlines()[0].split()[-1] if out else None


def run_pandoc(args: list[str], cwd: Path, stdin: bytes | None = None) -> bytes:
    exe = pandoc_path()
    if not exe:
        raise PandocMissing("服务器未安装 pandoc，只能导出 Markdown 和 BibTeX")
    try:
        done = subprocess.run([exe, "--sandbox", *args], cwd=cwd, input=stdin, capture_output=True, timeout=TIMEOUT,
                              env={"PATH": os.environ.get("PATH", ""), "HOME": str(cwd), "LANG": "C.UTF-8"})
    except subprocess.TimeoutExpired:
        raise OntologyError("pandoc 超时") from None
    if done.returncode != 0:
        msg = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise OntologyError("pandoc 失败：" + (msg[-1][:300] if msg else f"exit {done.returncode}"))
    return done.stdout


def style_file(style: str) -> Path:
    if style not in cite.STYLES:
        raise OntologyError(f"unknown citation style '{style}'; known: {list(cite.STYLES)}")
    return CSL_DIR / cite.STYLES[style]["csl"]


# ------------------------------------------------------------------ building the document


def csl_items(entries: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """CSL-JSON for pandoc: each item's id is its citekey."""
    return [{**csl, "id": key} for key, csl in entries]


def metadata(ms: dict[str, Any]) -> dict[str, Any]:
    meta = ms.get("meta") or {}
    out: dict[str, Any] = {"title": ms["title"], "lang": ms.get("lang") or "zh-CN",
                           "reference-section-title": REF_TITLE.get(ms.get("lang") or "zh-CN", "参考文献"),
                           "link-citations": False}
    if meta.get("subtitle"):
        out["subtitle"] = meta["subtitle"]
    if meta.get("authors"):
        out["author"] = [a["name"] + (f"（{a['affiliation']}）" if a.get("affiliation") else "") for a in meta["authors"]]
    if meta.get("date"):
        out["date"] = meta["date"]
    abstract = (meta.get("abstract") or "").strip()
    if meta.get("keywords"):
        out["keywords"] = meta["keywords"]
        label = "关键词" if out["lang"] == "zh-CN" else "Keywords"
        sep = "；" if out["lang"] == "zh-CN" else "; "
        abstract = (abstract + "\n\n" if abstract else "") + f"**{label}**：{sep.join(meta['keywords'])}"
    if abstract:
        out["abstract"] = abstract
    return out


def markdown(ms: dict[str, Any]) -> str:
    """The manuscript as one Pandoc Markdown document (metadata goes separately, see metadata())."""
    parts = []
    for s in ms["sections"]:
        heading = s["heading"].replace("\n", " ").strip()
        parts.append(f"{'#' * s['level']} {heading}\n\n{s['body'].strip()}\n")
    return "\n".join(parts)


def yaml_block(meta: dict[str, Any]) -> str:
    """A YAML metadata block (JSON is YAML) for the start of a .md file."""
    return "---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in meta.items()) + "\n---\n\n"


def _workdir(ms: dict[str, Any], entries: list[tuple[str, dict[str, Any]]]) -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory(prefix="onto-export-")
    d = Path(tmp.name)
    (d / "paper.md").write_text(markdown(ms), encoding="utf-8")
    (d / "meta.yaml").write_text(json.dumps(metadata(ms), ensure_ascii=False), encoding="utf-8")
    (d / "references.json").write_text(json.dumps(csl_items(entries), ensure_ascii=False), encoding="utf-8")
    return tmp


def _citeproc_args(ms: dict[str, Any], d: Path) -> list[str]:
    csl = d / "style.csl"
    shutil.copyfile(style_file(ms["csl_style"]), csl)
    return ["--citeproc", "--csl", "style.csl", "--bibliography", "references.json", "--metadata-file", "meta.yaml"]


def to_docx(ms: dict[str, Any], entries: list[tuple[str, dict[str, Any]]]) -> bytes:
    with _workdir(ms, entries) as tmp:
        d = Path(tmp)
        shutil.copyfile(REFERENCE_DOCX, d / "reference.docx")
        run_pandoc(["paper.md", "-f", "markdown", "-t", "docx", *_citeproc_args(ms, d), "--reference-doc",
                    "reference.docx", "--number-sections", "-o", "out.docx"], d)
        return (d / "out.docx").read_bytes()


def biblatex(entries: list[tuple[str, dict[str, Any]]]) -> str:
    """references.bib in biblatex form, by pandoc when there is one."""
    if not entries:
        return ""
    if not pandoc_path():
        return bibio.to_bibtex([{"citekey": k, "csl": c} for k, c in entries])
    with tempfile.TemporaryDirectory(prefix="onto-export-") as tmp:
        d = Path(tmp)
        (d / "references.json").write_text(json.dumps(csl_items(entries), ensure_ascii=False), encoding="utf-8")
        bib = run_pandoc(["references.json", "-f", "csljson", "-t", "biblatex"], d).decode("utf-8")
    # langid holds a BCP 47 tag (zh-CN) that biblatex does not know; the GB/T styles detect Chinese entries themselves
    return re.sub(r"^\s*langid = \{[^}]*\},?\n", "", bib, flags=re.M).replace(",\n}", "\n}")


LATEX_README = """这是从 ONTO-Research 导出的 LaTeX 工程。

编译（需要 XeLaTeX、ctex、biblatex、biber{extra}）：

    latexmk -xelatex main.tex

文件：
- main.tex          正文，由 pandoc 生成，可以直接修改
- references.bib    本文引用的文献（biblatex 格式），引用样式：{style}

论文中用 \\autocite{{键}} 或 \\textcite{{键}} 引用，键与文献库里的引用键一致。
"""


def to_latex(ms: dict[str, Any], entries: list[tuple[str, dict[str, Any]]]) -> bytes:
    style = cite.STYLES[ms["csl_style"]]
    zh = (ms.get("lang") or "zh-CN") == "zh-CN"
    thesis = ms.get("kind") == "thesis" or (ms.get("meta") or {}).get("template") == "report"
    docclass = ("ctexrep" if thesis else "ctexart") if zh else ("report" if thesis else "article")
    with _workdir(ms, entries) as tmp:
        d = Path(tmp)
        meta = metadata(ms)
        meta.pop("lang", None)  # ctex sets up Chinese; babel/polyglossia for zh only gets in the way
        meta.pop("reference-section-title", None)
        (d / "meta.yaml").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        (d / "references.bib").write_text(biblatex(entries) or "% 本文没有引用文献\n", encoding="utf-8")
        args = ["paper.md", "-f", "markdown", "-t", "latex", "-s", "--biblatex", "--bibliography", "references.bib",
                "--metadata-file", "meta.yaml", "--number-sections",
                "-V", f"documentclass={docclass}", "-V", f"biblio-style={style['biblatex']}",
                "-V", "biblatexoptions=backend=biber", "-V", f"biblio-title={REF_TITLE['zh-CN' if zh else 'en-US']}",
                "-V", "geometry=margin=2.5cm", "-o", "main.tex"]
        if thesis:
            args += ["--top-level-division=chapter"]
        run_pandoc(args, d)
        extra = {"gb7714-2015": "、biblatex-gb7714-2015", "gb7714-2015ay": "、biblatex-gb7714-2015",
                 "apa": "、biblatex-apa", "chicago-authordate": "、biblatex-chicago", "ieee": "、biblatex-ieee"}
        readme = LATEX_README.format(extra=extra.get(style["biblatex"], ""), style=style["label"])
        return _zip({"main.tex": (d / "main.tex").read_bytes(), "references.bib": (d / "references.bib").read_bytes(),
                     "README.txt": readme.encode("utf-8")})


def to_markdown(ms: dict[str, Any], entries: list[tuple[str, dict[str, Any]]], formatted: bool = False) -> tuple[bytes, str]:
    """(bytes, extension). Plain: a zip with paper.md (citations kept as [@key]) and its .bib and CSL-JSON.
    Formatted: one .md with citations and the reference list written out."""
    if not formatted:
        md = yaml_block({k: v for k, v in metadata(ms).items() if k != "link-citations"}) + markdown(ms)
        return _zip({"paper.md": md.encode("utf-8"),
                     "references.bib": bibio.to_bibtex([{"citekey": k, "csl": c} for k, c in entries]).encode("utf-8"),
                     "references.json": json.dumps(csl_items(entries), ensure_ascii=False, indent=1).encode("utf-8")}), "zip"
    return formatted_markdown(ms, entries).encode("utf-8"), "md"


def to_bib(entries: list[tuple[str, dict[str, Any]]]) -> bytes:
    return bibio.to_bibtex([{"citekey": k, "csl": c} for k, c in entries]).encode("utf-8")


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


# ------------------------------------------------------------------ Python fallback and preview

GROUP = re.compile(r"\[([^\[\]]*?@[^\[\]]*?)\](?!\{)")


def resolve_citations(text: str, bib: dict[str, Any], style: str) -> str:
    """Write citations out with plain labels: [@a; @b] -> [1, 2] or (张三, 2023; LI, 2021)."""
    entries = bib["entries"]
    numeric = bib["numeric"]

    def group(m: re.Match[str]) -> str:
        keys = [k for k in (x.group(1).rstrip(".,;:") for x in cite.CITE.finditer(m.group(1))) if k in entries]
        if not keys:
            return m.group(0)
        inner = [entries[k]["label"].strip("[]()（） ") for k in keys]
        if numeric:
            return "[" + ", ".join(inner) + "]"
        return "(" + "; ".join(inner) + ")"

    text = GROUP.sub(group, text)

    def bare(m: re.Match[str]) -> str:
        key = m.group(1).rstrip(".,;:")
        if key not in entries:
            return m.group(0)
        tail = m.group(1)[len(key):]
        label = entries[key]["label"].strip("[]()（） ")
        return (f"[{label}]" if numeric else label) + tail

    return cite.CITE.sub(bare, text)


def formatted_markdown(ms: dict[str, Any], entries: list[tuple[str, dict[str, Any]]]) -> str:
    """One readable .md: citations and the reference list written out (by citeproc when pandoc is there)."""
    bib = references(entries, ms["csl_style"], ms.get("lang") or "zh-CN")
    body = strip_claims(markdown(ms))
    body = resolve_citations(body, bib, ms["csl_style"])
    refs = "\n".join(f"{bib['entries'][k]['label'] + ' ' if bib['numeric'] else ''}{bib['entries'][k]['text']}\n"
                     for k in bib["order"])
    title = REF_TITLE.get(ms.get("lang") or "zh-CN", "参考文献")
    return f"# {ms['title']}\n\n{body}\n## {title}\n\n{refs}"


_preview_cache: dict[str, dict[str, Any]] = {}


def _stringify(node: Any) -> str:
    if isinstance(node, list):
        return "".join(_stringify(x) for x in node)
    if not isinstance(node, dict):
        return ""
    t, c = node.get("t"), node.get("c")
    if t == "Str":
        return c
    if t in ("Space", "SoftBreak"):
        return " "
    if t == "LineBreak":
        return "\n"
    if t in ("Emph", "Strong", "Strikeout", "Superscript", "Subscript", "SmallCaps", "Underline", "Plain", "Para"):
        return _stringify(c)
    if t == "Quoted":
        q = ("“", "”") if c[0]["t"] == "DoubleQuote" else ("‘", "’")
        return q[0] + _stringify(c[1]) + q[1]
    if t in ("Span", "Link"):
        return _stringify(c[1])
    if t == "Cite":
        return _stringify(c[1])
    if t == "Code":
        return c[1]
    if t == "Div":
        return _stringify(c[1])
    return ""


def references(entries: list[tuple[str, dict[str, Any]]], style: str, lang: str = "zh-CN") -> dict[str, Any]:
    """In-text labels and reference list entries, for the editor's preview: by pandoc and the CSL style when
    pandoc is there (cached), otherwise the plain formatter. {numeric, order, entries: {key: {label, text}}, engine}."""
    if not entries:
        return {"numeric": cite.STYLES[style]["numeric"], "order": [], "entries": {}, "engine": "none"}
    key = hashlib.sha256(json.dumps([style, lang, entries], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if key in _preview_cache:
        return _preview_cache[key]
    out = None
    if pandoc_path():
        try:
            out = _pandoc_references(entries, style, lang)
        except OntologyError:
            out = None
    if out is None:
        out = {**cite.bibliography(entries, style), "engine": "plain"}
    if len(_preview_cache) > 200:
        _preview_cache.clear()
    _preview_cache[key] = out
    return out


def _pandoc_references(entries: list[tuple[str, dict[str, Any]]], style: str, lang: str) -> dict[str, Any]:
    numeric = cite.STYLES[style]["numeric"]
    with tempfile.TemporaryDirectory(prefix="onto-preview-") as tmp:
        d = Path(tmp)
        (d / "doc.md").write_text("\n\n".join(f"[@{k}]" for k, _ in entries), encoding="utf-8")
        (d / "references.json").write_text(json.dumps(csl_items(entries), ensure_ascii=False), encoding="utf-8")
        shutil.copyfile(style_file(style), d / "style.csl")
        (d / "meta.yaml").write_text(json.dumps({"lang": lang, "link-citations": False}), encoding="utf-8")
        ast = json.loads(run_pandoc(["doc.md", "-f", "markdown", "-t", "json", "--citeproc", "--csl", "style.csl",
                                     "--bibliography", "references.json", "--metadata-file", "meta.yaml"], d))
    labels, texts, order = {}, {}, []
    paras = [b for b in ast["blocks"] if b["t"] == "Para"]
    for (k, _), p in zip(entries, paras):
        labels[k] = _stringify(p["c"]).strip()
    for b in ast["blocks"]:
        if b["t"] == "Div" and b["c"][0][0] == "refs":
            for item in b["c"][1]:
                if item["t"] != "Div":
                    continue
                ident = item["c"][0][0]
                k = ident[4:] if ident.startswith("ref-") else ident
                inner = item["c"][1]
                # numeric styles put the number in a csl-left-margin span; keep only the entry
                text = _stringify(inner).strip()
                if numeric:
                    text = re.sub(r"^\s*\[?\d+\]?[.\s]*", "", text)
                texts[k] = re.sub(r"\s+", " ", text)
                order.append(k)
    return {"numeric": numeric, "order": order or [k for k, _ in entries],
            "entries": {k: {"label": labels.get(k, ""), "text": texts.get(k, "")} for k, _ in entries},
            "engine": "pandoc"}


def export(ms: dict[str, Any], entries: list[tuple[str, dict[str, Any]]], fmt: str,
           formatted: bool = False) -> tuple[bytes, str, str]:
    """(bytes, filename, media type) for one export format."""
    if fmt not in FORMATS:
        raise OntologyError(f"format must be one of {list(FORMATS)}")
    stem = re.sub(r'[\\/:*?"<>|\s]+', "_", ms["title"]).strip("_")[:80] or "paper"
    if fmt == "docx":
        return to_docx(ms, entries), f"{stem}.docx", \
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if fmt == "tex":
        return to_latex(ms, entries), f"{stem}-latex.zip", "application/zip"
    if fmt == "bib":
        return to_bib(entries), f"{stem}.bib", "application/x-bibtex; charset=utf-8"
    data, ext = to_markdown(ms, entries, formatted)
    return data, (f"{stem}.md" if ext == "md" else f"{stem}-markdown.zip"), \
        ("text/markdown; charset=utf-8" if ext == "md" else "application/zip")
