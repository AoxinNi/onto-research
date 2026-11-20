"""Citations in manuscript text, and plain reference formatting.

Section bodies are Pandoc Markdown. A citation is `[@key]`, `[@a; @b, 第 12 页]` or an in-text `@key`; a claim anchor
is a bracketed span with the claim class and a node, `[这句话]{.claim node=n_…}`. This module finds both, for the
outline, the citation check and the export, without running pandoc.

Exports format references with pandoc and the official CSL styles (ontology/export). The formatter here is the
fallback for when pandoc is missing, and for the editor's preview: readable, close to each style, but not a
replacement for citeproc.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Citation styles a manuscript can use: the CSL file pandoc formats with, the biblatex style of the LaTeX export,
# and whether in-text citations are numbers.
STYLES: dict[str, dict[str, Any]] = {
    "gb7714-numeric": {"label": "GB/T 7714—2015 顺序编码制", "csl": "china-national-standard-gb-t-7714-2015-numeric.csl",
                       "biblatex": "gb7714-2015", "numeric": True},
    "gb7714-author-date": {"label": "GB/T 7714—2015 著者-出版年制",
                           "csl": "china-national-standard-gb-t-7714-2015-author-date.csl",
                           "biblatex": "gb7714-2015ay", "numeric": False},
    "apa": {"label": "APA 第 7 版", "csl": "apa.csl", "biblatex": "apa", "numeric": False},
    "chicago-author-date": {"label": "Chicago 著者-出版年", "csl": "chicago-author-date.csl",
                            "biblatex": "chicago-authordate", "numeric": False},
    "ieee": {"label": "IEEE", "csl": "ieee.csl", "biblatex": "ieee", "numeric": True},
}

KEY = r"[\w一-鿿][\w一-鿿:.#$%&+?<>~/-]*"
CITE = re.compile(rf"(?<![\w.@])-?@({KEY})")
NODE_ID = re.compile(r"n_[0-9a-f]{12}")
CODE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`", re.S)
COMMENT = re.compile(r"<!--.*?-->", re.S)
ATTRS = re.compile(r"\]\{([^{}\n]*)\}")
SENTENCE_END = "。！？!?；\n"  # not ";": it separates citations in [@a; @b]


def _blank(m: re.Match[str]) -> str:
    return re.sub(r"[^\n]", " ", m.group(0))


def visible(body: str) -> str:
    """The body with code and HTML comments blanked out (same length, so offsets still match)."""
    return COMMENT.sub(_blank, CODE.sub(_blank, body or ""))


def citekeys(body: str) -> list[str]:
    """Citekeys in order of first appearance."""
    out = []
    for m in CITE.finditer(visible(body)):
        out.append(m.group(1).rstrip(".,;:"))
    return list(dict.fromkeys(k for k in out if k))


@dataclass
class Claim:
    node: str
    text: str
    start: int          # the span's '['
    end: int            # after its '}'
    sentence: str = ""  # the sentence (or line) around the span, citations included
    citekeys: list[str] = field(default_factory=list)


def _open_bracket(text: str, close: int) -> int | None:
    depth = 0
    for i in range(close - 1, -1, -1):
        c = text[i]
        if c == "]" and (i == 0 or text[i - 1] != "\\"):
            depth += 1
        elif c == "[" and (i == 0 or text[i - 1] != "\\"):
            if depth == 0:
                return i
            depth -= 1
    return None


def claims(body: str) -> list[Claim]:
    """Claim anchors, each with the sentence it sits in and the citekeys of that sentence."""
    text = visible(body)
    out = []
    for m in ATTRS.finditer(text):
        attrs = m.group(1).split()
        if ".claim" not in attrs:
            continue
        node = next((a[5:].strip("\"'") for a in attrs if a.startswith("node=")), "")
        if not node:
            continue  # a malformed id is kept: the citation check reports it as a missing node
        start = _open_bracket(text, m.start())
        if start is None:
            continue
        end = m.end()
        s = max(text.rfind(c, 0, start) for c in SENTENCE_END) + 1
        e = min((i for i in (text.find(c, end) for c in SENTENCE_END) if i >= 0), default=len(text))
        sentence = text[s:e + 1].strip()
        out.append(Claim(node, text[start + 1:m.start()], start, end, sentence, citekeys(sentence)))
    return out


# ------------------------------------------------------------------ quotes and numbers next to citations

BRACKET_CITE = re.compile(r"\[([^\[\]\n]*@[^\[\]\n]*)\]")
INLINE_QUOTE = re.compile(r"(?:“([^“”\n]{4,3000})”|\"([^\"\n]{4,3000})\"|「([^「」\n]{4,3000})」|『([^『』\n]{4,3000})』)"
                          r"[ \t]*[（(]?[ \t]*(?=\[[^\[\]\n]*@)")
PAGE = re.compile(r"(?:\bpp?\.|\bpages?\b|\bS\.|第|页)\s*(\d{1,5})", re.I)
NUMBER = re.compile(r"(?<![\w.,])[-−]?\d{1,3}(?:,\d{3})+(?:\.\d+)?%?|(?<![\w.,])[-−]?\d+(?:\.\d+)?\s*%?")
YEAR = re.compile(r"(?:1[6-9]|20)\d\d")


@dataclass
class Cited:
    """A run of text that a citation vouches for: a quoted passage, or a number in a citing sentence."""
    text: str
    start: int          # offsets in the section body
    end: int
    citekeys: list[str]
    page: int | None = None


def _locator(cite_text: str) -> int | None:
    m = PAGE.search(cite_text)
    return int(m.group(1)) if m else None


def _keys(cite_text: str) -> list[str]:
    return [k for k in dict.fromkeys(m.group(1).rstrip(".,;:") for m in CITE.finditer(cite_text)) if k]


def quotes(body: str) -> list[Cited]:
    """Quoted passages followed by a citation: “…”[@key, p. 3], and block quotes whose last line (or the line after)
    is a citation. The page is the citation's locator when it has one and cites a single source."""
    text = visible(body)
    out: list[Cited] = []
    for m in INLINE_QUOTE.finditer(text):
        c = BRACKET_CITE.match(text, m.end())
        if not c:
            continue
        q = next(g for g in m.groups() if g is not None)
        keys = _keys(c.group(1))
        g = next(i for i in range(1, 5) if m.group(i) is not None)
        out.append(Cited(q, m.start(g), m.end(g), keys, _locator(c.group(1)) if len(keys) == 1 else None))
    lines = text.split("\n")
    starts, pos = [], 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln) + 1
    i = 0
    while i < len(lines):
        if not lines[i].lstrip().startswith(">"):
            i += 1
            continue
        j = i
        while j < len(lines) and lines[j].lstrip().startswith(">"):
            j += 1
        block = [(starts[k] + lines[k].index(">") + 1, lines[k][lines[k].index(">") + 1:]) for k in range(i, j)]
        joined_end = block[-1][0] + len(block[-1][1])
        last = block[-1][1].rstrip()
        c = None
        tail = re.search(r"[（(]?\s*\[([^\[\]\n]*@[^\[\]\n]*)\][）)]?\s*$", last)
        if tail:
            c = tail.group(1)
            joined_end = block[-1][0] + tail.start()
        elif j < len(lines):
            nxt = re.fullmatch(r"\s*(?:[—–-]+\s*)?[（(]?\s*\[([^\[\]\n]*@[^\[\]\n]*)\][）)]?\s*", lines[j])
            c = nxt.group(1) if nxt else None
        if c:
            first = block[0][0] + (len(block[0][1]) - len(block[0][1].lstrip()))
            quote = " ".join(text[a:a + len(t)].strip() for a, t in block)
            if tail:
                quote = quote[:len(quote) - len(last) + tail.start()].strip() if len(block) == 1 else \
                    " ".join([*(t.strip() for _, t in block[:-1]), last[:tail.start()].strip()])
            keys = _keys(c)
            if len(quote) >= 4:
                out.append(Cited(quote.strip().strip("“”\"「」"), first, joined_end, keys,
                                 _locator(c) if len(keys) == 1 else None))
        i = j
    out.sort(key=lambda q: q.start)
    return out


def number_value(token: str) -> float | None:
    t = token.replace(",", "").replace("−", "-").replace("%", "").strip()
    try:
        return float(t)
    except ValueError:
        return None


def numbers(body: str) -> list[Cited]:
    """Numbers in sentences that cite a source, outside the citation itself and outside quotes (those are checked
    whole). Left out, so the check stays quiet: years, numbers below 10 without a decimal or a %, and numbers in
    headings, lists' markers, footnote marks, links and claim attributes."""
    text = visible(body)
    masked = list(text)

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if masked[k] != "\n":
                masked[k] = " "

    for m in BRACKET_CITE.finditer(text):
        blank(m.start(), m.end())
    for m in ATTRS.finditer(text):
        blank(m.start(), m.end())
    for m in re.finditer(r"\]\([^)\n]*\)|<[^>\n]+>|\[\^[^\]\n]*\]|https?://\S+", text):
        blank(m.start(), m.end())
    for q in quotes(body):
        blank(q.start, q.end)
    for m in re.finditer(r"(?m)^(?:\s*#{1,6}\s.*|\s*(?:\d+[.)]|[-*+])\s)", text):
        blank(m.start(), m.end() if not m.group(0).lstrip().startswith("#") else m.end())
    plain_text = "".join(masked)
    out: list[Cited] = []
    bounds = [m.end() for m in re.finditer(r"[。！？!?；\n]|\.(?=\s)", plain_text)] + [len(text)]
    s = 0
    for e in bounds:
        keys = citekeys(text[s:e])
        if keys:
            for m in NUMBER.finditer(plain_text, s, e):
                tok = m.group(0).strip()
                v = number_value(tok)
                if v is None:
                    continue
                whole = re.sub(r"[,%\s−-]", "", tok)
                if "%" not in tok and "." not in tok and "," not in tok:
                    if abs(v) < 10 or YEAR.fullmatch(whole):
                        continue
                out.append(Cited(tok, m.start(), m.start() + len(tok), keys))
        s = e
    return out


def plain(body: str) -> str:
    """Readable text of a body: claim spans keep their words, markup characters go."""
    text = visible(body)
    text = re.sub(r"\]\{[^{}\n]*\}", "]", text)
    return re.sub(r"[#*_>`\[\]{}]", "", text)


def word_count(body: str) -> int:
    """Characters of CJK text plus words of everything else, as a Chinese editor counts."""
    text = plain(body)
    cjk = len(re.findall(r"[㐀-鿿豈-﫿]", text))
    words = len(re.findall(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*", text))
    return cjk + words


def rename_citekey(body: str, old: str, new: str) -> str:
    """Replace citations of `old` with `new`, leaving code and longer keys that start with `old` alone."""
    vis = visible(body)
    pat = re.compile(rf"(?<![\w.@])(-?@){re.escape(old)}(?![\w一-鿿:#$%&+?<>~/-]|\.[\w一-鿿])")
    out, last = [], 0
    for m in pat.finditer(body):
        if vis[m.start():m.end()] != m.group(0):
            continue  # inside code or a comment
        out.append(body[last:m.start()] + m.group(1) + new)
        last = m.end()
    return "".join(out) + body[last:]


def rewrite_nodes(body: str, ids: dict[str, str]) -> str:
    """Point claim anchors at other node ids (used when a project file is imported)."""
    return re.sub(r"node=(n_[0-9a-f]{12})", lambda m: f"node={ids.get(m.group(1), m.group(1))}", body or "")


# ------------------------------------------------------------------ fallback formatting

TYPE_CODE = {"article-journal": "J", "article-magazine": "J", "article-newspaper": "N", "book": "M", "chapter": "M",
             "paper-conference": "C", "thesis": "D", "report": "R", "webpage": "EB/OL", "post-weblog": "EB/OL",
             "dataset": "DS", "software": "CP", "patent": "P", "standard": "S"}
CJK = re.compile(r"[㐀-鿿]")


def _year(csl: dict[str, Any]) -> str:
    try:
        return str(csl["issued"]["date-parts"][0][0])
    except (KeyError, IndexError, TypeError):
        return str(csl.get("issued", {}).get("literal", "")) if isinstance(csl.get("issued"), dict) else ""


def _people(csl: dict[str, Any]) -> list[dict[str, str]]:
    return [p for p in (csl.get("author") or csl.get("editor") or []) if isinstance(p, dict)]


def _family(p: dict[str, str]) -> str:
    return str(p.get("literal") or p.get("family") or p.get("given") or "")


def _initials(given: str, sep: str = " ", dot: bool = False) -> str:
    parts = [g for g in re.split(r"[\s.]+", given or "") if g]
    out = []
    for g in parts:
        bits = g.split("-")
        out.append("-".join(b[:1].upper() + ("." if dot else "") for b in bits if b))
    return sep.join(out)


def _gb_name(p: dict[str, str]) -> str:
    if p.get("literal") or CJK.search(_family(p) + str(p.get("given") or "")):
        return str(p.get("literal") or f"{p.get('family', '')}{p.get('given', '')}")
    return f"{str(p.get('family') or '').upper()} {_initials(str(p.get('given') or ''))}".strip()


def _gb_authors(csl: dict[str, Any]) -> str:
    people = _people(csl)
    if not people:
        return ""
    names = [_gb_name(p) for p in people[:3]]
    zh = any(CJK.search(n) for n in names)
    if len(people) > 3:
        names.append("等" if zh else "et al")
    return ", ".join(names)


def _apa_name(p: dict[str, str]) -> str:
    if p.get("literal") or CJK.search(_family(p)):
        return _family(p) + str(p.get("given") or "" if CJK.search(_family(p)) else "")
    init = _initials(str(p.get("given") or ""), " ", dot=True)
    return f"{p.get('family', '')}, {init}".strip(", ")


def _join_and(names: list[str], amp: str = "&") -> str:
    if len(names) <= 1:
        return "".join(names)
    if len(names) == 2:
        return f"{names[0]}, {amp} {names[1]}" if amp == "&" else f"{names[0]} {amp} {names[1]}"
    return ", ".join(names[:-1]) + f", {amp} {names[-1]}"


def _pages(csl: dict[str, Any]) -> str:
    return str(csl.get("page") or "").replace("--", "-").replace("–", "-")


def _vol_issue(csl: dict[str, Any]) -> str:
    vol, issue = str(csl.get("volume") or ""), str(csl.get("issue") or "")
    return f"{vol}({issue})" if vol and issue else vol or (f"({issue})" if issue else "")


def format_entry(csl: dict[str, Any], style: str) -> str:
    """One reference list entry as plain text (no number or label)."""
    title = str(csl.get("title") or "").strip().rstrip(".")
    container = str(csl.get("container-title") or "").strip()
    year = _year(csl)
    publisher = str(csl.get("publisher") or "").strip()
    place = str(csl.get("publisher-place") or "").strip()
    doi = str(csl.get("DOI") or "").strip()
    url = str(csl.get("URL") or "").strip()
    pages = _pages(csl)
    ctype = str(csl.get("type") or "")
    if style.startswith("gb7714"):
        code = TYPE_CODE.get(ctype, "Z")
        authors = _gb_authors(csl)
        if ctype == "chapter" and container:
            body = f"{title}[M]//{container}. {', '.join(x for x in (place and f'{place}: {publisher}' or publisher, year) if x)}"
        elif code in ("M", "R", "D", "S"):
            pub = f"{place}: {publisher}" if place and publisher else publisher or place
            body = f"{title}[{code}]. " + ", ".join(x for x in (pub, year) if x)
        elif code == "EB/OL":
            body = f"{title}[EB/OL]. " + ", ".join(x for x in (container, year) if x)
        else:
            vi = _vol_issue(csl)
            src = ", ".join(x for x in (container, "" if style.endswith("author-date") else year, vi) if x)
            body = f"{title}[{code}]. {src}" + (f": {pages}" if pages else "")
        head = f"{authors}. {year}. " if style.endswith("author-date") and year else (f"{authors}. " if authors else "")
        tail = f". DOI: {doi}" if doi else (f". {url}" if url else "")
        return (head + body).rstrip(". ") + tail + "."
    if style == "ieee":
        people = _people(csl)
        names = [f"{_initials(str(p.get('given') or ''), ' ', True)} {p.get('family', '')}".strip() if not p.get("literal")
                 else str(p["literal"]) for p in people]
        authors = names[0] + " et al." if len(names) > 6 else (" and ".join(names) if len(names) <= 2
                                                               else ", ".join(names[:-1]) + ", and " + names[-1])
        parts = [f"{authors}," if authors else "", f"“{title},”"]
        if container:
            parts.append(f"{container},")
        if csl.get("volume"):
            parts.append(f"vol. {csl['volume']},")
        if csl.get("issue"):
            parts.append(f"no. {csl['issue']},")
        if pages:
            parts.append(f"pp. {pages.replace('-', '–')},")
        if publisher and not container:
            parts.append(f"{publisher},")
        parts.append(f"{year}." if year else "")
        out = " ".join(p for p in parts if p).rstrip(",")
        return out + (f" doi: {doi}." if doi else "")
    # APA and Chicago author-date
    people = _people(csl)
    if style == "apa":
        names = [_apa_name(p) for p in people]
        authors = _join_and(names[:20])
        head = f"{authors} ({year or 'n.d.'}). " if authors else f"{title}. ({year or 'n.d.'}). "
        body = (f"{title}. " if authors else "") + (f"{container}" if container else publisher)
        vi = _vol_issue(csl)
        if container and vi:
            body += f", {vi}"
        if pages and container:
            body += f", {pages.replace('-', '–')}"
        out = (head + body).rstrip(" .") + "."
        return out + (f" https://doi.org/{doi}" if doi else (f" {url}" if url else ""))
    names = []
    for i, p in enumerate(people):
        if p.get("literal") or CJK.search(_family(p)):
            names.append(_family(p) + (str(p.get("given") or "") if CJK.search(_family(p)) else ""))
        elif i == 0:
            names.append(f"{p.get('family', '')}, {p.get('given', '')}".strip(", "))
        else:
            names.append(f"{p.get('given', '')} {p.get('family', '')}".strip())
    authors = _join_and(names, "and").replace(", and", ", and")
    out = f"{authors}. {year}. “{title}.”" if authors else f"“{title}.” {year}."
    if container:
        out += f" {container}"
        if csl.get("volume"):
            out += f" {csl['volume']}"
        if csl.get("issue"):
            out += f" ({csl['issue']})"
        if pages:
            out += f": {pages.replace('-', '–')}"
        out += "."
    elif publisher:
        out += f" {place + ': ' if place else ''}{publisher}."
    return out + (f" https://doi.org/{doi}." if doi else "")


def short_label(csl: dict[str, Any], style: str) -> str:
    """Author and year as an author-date citation shows them, without brackets."""
    people = _people(csl)
    year = _year(csl) or "n.d."
    if not people:
        return f"{str(csl.get('title') or '')[:20]}, {year}"
    first = people[0]
    zh = bool(CJK.search(_family(first)))
    fam = _family(first) + (str(first.get("given") or "") if zh and not first.get("literal") else "")
    if style.startswith("gb7714") and not zh:
        fam = fam.upper()
    if len(people) == 2 and not style.startswith("gb7714"):
        sep = " & " if style == "apa" else " and "
        fam = f"{fam}{sep}{_family(people[1])}"
    elif len(people) > 2 or (style.startswith("gb7714") and len(people) > 1):
        fam += " 等" if zh else " et al."
    return f"{fam} {year}" if style == "chicago-author-date" else f"{fam}, {year}"


def bibliography(entries: list[tuple[str, dict[str, Any]]], style: str) -> dict[str, Any]:
    """Labels and entries for citekeys in order of first citation: {key: {label, text}} plus the order.

    Numeric styles number by first citation; author-date styles sort the list by author and year."""
    numeric = STYLES.get(style, STYLES["gb7714-numeric"])["numeric"]
    out: dict[str, dict[str, str]] = {}
    for i, (key, csl) in enumerate(entries, 1):
        out[key] = {"label": f"[{i}]" if numeric else short_label(csl, style), "text": format_entry(csl, style)}
    order = [k for k, _ in entries]
    if not numeric:
        order.sort(key=lambda k: (out[k]["text"].lower()))
    return {"numeric": numeric, "order": order, "entries": out}
