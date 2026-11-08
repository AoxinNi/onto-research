"""Read reference-manager exports (BibTeX, RIS, CSL-JSON) into CSL-JSON items, and write BibTeX back.

Plain Python, so importing does not need pandoc. The parsers are forgiving: an entry that cannot be read is
reported and skipped, not fatal for the rest of the file.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from .errors import OntologyError

MAX_IMPORT = 5 * 1024 * 1024
MAX_ENTRIES = 2000

BIB_TYPES = {"article": "article-journal", "book": "book", "booklet": "book", "inbook": "chapter",
             "incollection": "chapter", "inproceedings": "paper-conference", "conference": "paper-conference",
             "proceedings": "book", "phdthesis": "thesis", "mastersthesis": "thesis", "thesis": "thesis",
             "techreport": "report", "report": "report", "manual": "report", "unpublished": "manuscript",
             "online": "webpage", "electronic": "webpage", "www": "webpage", "misc": "document",
             "dataset": "dataset", "software": "software"}
CSL_BIB = {"article-journal": "article", "article": "misc", "book": "book", "chapter": "incollection",
           "paper-conference": "inproceedings", "thesis": "phdthesis", "report": "techreport",
           "webpage": "online", "dataset": "misc", "software": "misc", "manuscript": "unpublished"}
RIS_TYPES = {"JOUR": "article-journal", "JFULL": "article-journal", "MGZN": "article-magazine",
             "NEWS": "article-newspaper", "BOOK": "book", "EDBOOK": "book", "CHAP": "chapter", "CONF": "paper-conference",
             "CPAPER": "paper-conference", "THES": "thesis", "RPRT": "report", "ELEC": "webpage", "WEB": "webpage",
             "BLOG": "post-weblog", "DATA": "dataset", "COMP": "software", "UNPB": "manuscript", "GEN": "document"}
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"), start=1)}
ACCENTS = {"'": "\u0301", "`": "\u0300", "^": "\u0302", '"': "\u0308", "~": "\u0303", "=": "\u0304", ".": "\u0307",
           "u": "\u0306", "v": "\u030c", "H": "\u030b", "c": "\u0327", "k": "\u0328", "r": "\u030a"}
SYMBOLS = {"ss": "ß", "o": "ø", "O": "Ø", "ae": "æ", "AE": "Æ", "oe": "œ", "OE": "Œ", "aa": "å", "AA": "Å",
           "l": "ł", "L": "Ł", "i": "ı", "j": "ȷ", "&": "&", "%": "%", "$": "$", "#": "#", "_": "_", "textendash": "–",
           "textemdash": "—", "textquoteright": "’", "textquoteleft": "‘"}


# ------------------------------------------------------------------ detection


def detect(text: str, filename: str = "") -> str:
    name = filename.lower()
    if name.endswith((".bib", ".bibtex")):
        return "bibtex"
    if name.endswith((".ris", ".nbib")):
        return "ris"
    if name.endswith(".json"):
        return "csl-json"
    head = text.lstrip()[:2000]
    if head.startswith(("[", "{")):
        return "csl-json"
    if re.search(r"^TY  - ", head, re.M):
        return "ris"
    if re.search(r"@\w+\s*[{(]", head):
        return "bibtex"
    raise OntologyError("not a BibTeX, RIS or CSL-JSON file")


def parse(text: str, filename: str = "", fmt: str | None = None) -> dict[str, Any]:
    """{format, entries: [{csl, citekey?, tags}], skipped: [reason]}."""
    if len(text.encode("utf-8")) > MAX_IMPORT:
        raise OntologyError("the import file is larger than 5 MB")
    fmt = fmt or detect(text, filename)
    reader = {"bibtex": parse_bibtex, "ris": parse_ris, "csl-json": parse_csl_json}.get(fmt)
    if not reader:
        raise OntologyError("format must be bibtex, ris or csl-json")
    entries, skipped = reader(text)
    if len(entries) > MAX_ENTRIES:
        raise OntologyError(f"at most {MAX_ENTRIES} entries per import")
    return {"format": fmt, "entries": entries, "skipped": skipped}


# ------------------------------------------------------------------ CSL-JSON


def parse_csl_json(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        data = json.loads(text)
    except ValueError as e:
        raise OntologyError(f"the file is not valid JSON: {e}") from None
    if isinstance(data, dict):
        data = data.get("items", [data]) if "items" in data else [data]
    if not isinstance(data, list):
        raise OntologyError("CSL-JSON must be a list of items")
    entries, skipped = [], []
    for i, item in enumerate(data):
        if not isinstance(item, dict) or not item.get("title"):
            skipped.append(f"item {i + 1}: no title")
            continue
        key = item.get("citation-key") or item.get("id")
        entries.append({"csl": item, "citekey": str(key) if key and _keyish(str(key)) else None,
                        "tags": _split_tags(item.get("keyword"))})
    return entries, skipped


# ------------------------------------------------------------------ BibTeX


def _keyish(key: str) -> bool:
    return bool(re.fullmatch(r"[\w一-鿿][\w一-鿿:.#$%&+?<>~/-]{0,79}", key))


def _read_value(s: str, i: int, strings: dict[str, str]) -> tuple[str, int]:
    """One field value starting at s[i] (after '='): braces, quotes, bare words/numbers, joined with '#'."""
    parts = []
    n = len(s)
    while True:
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            break
        if s[i] == "{":
            depth, j = 1, i + 1
            while j < n and depth:
                if s[j] == "\\":
                    j += 2
                    continue
                depth += {"{": 1, "}": -1}.get(s[j], 0)
                j += 1
            parts.append(s[i + 1:j - 1])
            i = j
        elif s[i] == '"':
            depth, j = 0, i + 1
            while j < n and not (s[j] == '"' and depth == 0):
                if s[j] == "\\":
                    j += 2
                    continue
                depth += {"{": 1, "}": -1}.get(s[j], 0)
                j += 1
            parts.append(s[i + 1:j])
            i = j + 1
        else:
            m = re.match(r"[^\s,#}\)]+", s[i:])
            if not m:
                break
            word = m.group(0)
            parts.append(strings.get(word.lower(), word))
            i += len(word)
        while i < n and s[i].isspace():
            i += 1
        if i < n and s[i] == "#":
            i += 1
            continue
        break
    return "".join(parts), i


def _entries(text: str) -> list[tuple[str, str, int]]:
    """(type, body, offset) for every @type{...} or @type(...) block, matching braces."""
    out = []
    for m in re.finditer(r"@\s*(\w+)\s*([{(])", text):
        open_ch = m.group(2)
        close_ch = "}" if open_ch == "{" else ")"
        depth, j = 1, m.end()
        while j < len(text) and depth:
            c = text[j]
            if c == "\\":
                j += 2
                continue
            depth += 1 if c == open_ch else -1 if c == close_ch else 0
            j += 1
        out.append((m.group(1).lower(), text[m.end():j - 1], m.start()))
    # drop blocks nested inside earlier ones (an @ inside a field value)
    kept, end = [], -1
    for t, body, start in out:
        if start > end:
            kept.append((t, body, start))
            end = start + len(body)
    return kept


def _fields(body: str, strings: dict[str, str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    i = 0
    while True:
        m = re.compile(r"\s*,?\s*([\w:.+-]+)\s*=").match(body, i)
        if not m:
            break
        value, i = _read_value(body, m.end(), strings)
        fields[m.group(1).lower()] = value
    return fields


def delatex(s: str) -> str:
    """Plain text from a BibTeX value: accents, escaped symbols, simple commands and braces."""
    def accent(m: re.Match) -> str:
        cmd, ch = m.group(1), m.group(2) or m.group(3) or ""
        if ch in ("i", "\\i"):
            ch = "i"
        return unicodedata.normalize("NFC", ch + ACCENTS[cmd]) if ch else ""

    s = re.sub(r"\\([`'^\"~=.uvHckr])\s*(?:\{\\?(\w)\}|\\?(\w))", accent, s)
    s = re.sub(r"\\([`'^\"~=.])\{\}", "", s)
    s = re.sub(r"\\(ss|ae|AE|oe|OE|aa|AA|o|O|l|L|i|j|textendash|textemdash|textquoteright|textquoteleft)\b\s?",
               lambda m: SYMBOLS[m.group(1)], s)
    s = re.sub(r"\\([&%$#_])", lambda m: m.group(1), s)
    s = re.sub(r"\\(?:emph|textit|textbf|textsc|textrm|texttt|mathrm|url|mbox|text)\s*\{([^{}]*)\}", r"\1", s)
    s = s.replace("---", "—").replace("--", "–").replace("~", "\u00a0").replace("``", "“").replace("''", "”")
    s = re.sub(r"\\[a-zA-Z]+\s*", "", s)
    return " ".join(s.replace("{", "").replace("}", "").split())


def _top_split(s: str, sep: str) -> list[str]:
    """Split on a separator word (' and ') or char (',') outside braces."""
    out, depth, cur, i = [], 0, [], 0
    while i < len(s):
        c = s[i]
        depth += {"{": 1, "}": -1}.get(c, 0)
        if depth == 0 and s.startswith(sep, i):
            out.append("".join(cur))
            cur = []
            i += len(sep)
            continue
        cur.append(c)
        i += 1
    out.append("".join(cur))
    return [x.strip() for x in out if x.strip()]


def bib_names(value: str) -> list[dict[str, str]]:
    people = []
    for raw in _top_split(" ".join(value.split()), " and "):
        if raw.lower() == "others":
            continue
        if raw.startswith("{") and raw.endswith("}") and raw.count("{") == 1:
            people.append({"literal": delatex(raw)})
            continue
        parts = _top_split(raw, ",")
        if len(parts) >= 2:  # Family, Given  or  Family, Jr, Given
            person = {"family": delatex(parts[0]), "given": delatex(parts[-1])}
            if len(parts) == 3:
                person["suffix"] = delatex(parts[1])
        else:
            words = _top_split(raw, " ")
            if len(words) == 1:
                person = {"family": delatex(words[0])}
            else:  # von-part: lower-case words before the family name belong to it
                k = len(words) - 1
                while k > 1 and words[k - 1][:1].islower():
                    k -= 1
                person = {"given": delatex(" ".join(words[:k])), "family": delatex(" ".join(words[k:]))}
        people.append({k: v for k, v in person.items() if v})
    return people


def _date(year: str | None, month: str | None = None, day: str | None = None) -> dict[str, Any] | None:
    if not year or not (m := re.search(r"\d{4}", year)):
        return None
    parts = [int(m.group(0))]
    mon = (month or "").strip().lower()[:3]
    if mon.isdigit() or mon in MONTHS:
        parts.append(int(mon) if mon.isdigit() else MONTHS[mon])
        if day and day.strip().isdigit():
            parts.append(int(day.strip()))
    return {"date-parts": [parts]}


def _split_tags(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [t.strip() for t in re.split(r"[;,]", str(value or "")) if t.strip()]


def bib_to_csl(etype: str, f: dict[str, str]) -> dict[str, Any]:
    t = BIB_TYPES.get(etype, "document")
    if etype == "misc" and ("arxiv" in (f.get("eprinttype", "") + f.get("archiveprefix", "")).lower()):
        t = "article"
    csl: dict[str, Any] = {"type": t, "title": delatex(f.get("title", ""))}
    for key in ("author", "editor"):
        if f.get(key):
            csl[key] = bib_names(f[key])
    container = f.get("journal") or f.get("journaltitle") or f.get("booktitle")
    if container:
        csl["container-title"] = delatex(container)
    if issued := (_date(f.get("year"), f.get("month"), f.get("day")) or _iso_date(f.get("date"))):
        csl["issued"] = issued
    simple = {"volume": "volume", "number": "issue", "issue": "issue", "edition": "edition", "isbn": "ISBN",
              "issn": "ISSN", "url": "URL", "doi": "DOI", "abstract": "abstract", "note": "note",
              "series": "collection-title", "address": "publisher-place", "location": "publisher-place"}
    for k, target in simple.items():
        if f.get(k) and target not in csl:
            csl[target] = delatex(f[k]) if k not in ("url", "doi") else f[k].strip()
    if f.get("pages"):
        csl["page"] = delatex(f["pages"]).replace("–", "-")
    publisher = f.get("publisher") or f.get("school") or f.get("institution") or f.get("organization")
    if publisher:
        csl["publisher"] = delatex(publisher)
    if f.get("eprint") and "arxiv" in (f.get("archiveprefix", "") + f.get("eprinttype", "")).lower():
        csl["number"] = f["eprint"].strip()
        csl.setdefault("publisher", "arXiv")
        csl.setdefault("URL", f"https://arxiv.org/abs/{f['eprint'].strip()}")
    return csl


def _iso_date(value: str | None) -> dict[str, Any] | None:
    if not value or not (m := re.match(r"(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", value.strip())):
        return None
    return {"date-parts": [[int(x) for x in m.groups() if x]]}


def parse_bibtex(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    strings = {m: m.title() for m in MONTHS}
    entries, skipped = [], []
    for etype, body, _ in _entries(text):
        if etype in ("comment", "preamble"):
            continue
        if etype == "string":
            for k, v in _fields(body, strings).items():
                strings[k] = v
            continue
        key, _, rest = body.partition(",")
        key = key.strip()
        fields = _fields(rest, strings)
        if not fields.get("title"):
            skipped.append(f"@{etype}{{{key}}}: no title")
            continue
        try:
            csl = bib_to_csl(etype, fields)
        except Exception as e:  # noqa: BLE001 - one bad entry must not stop the import
            skipped.append(f"@{etype}{{{key}}}: {e}")
            continue
        entries.append({"csl": csl, "citekey": key if _keyish(key) else None,
                        "tags": _split_tags(delatex(fields.get("keywords", "")))})
    return entries, skipped


# ------------------------------------------------------------------ RIS


def parse_ris(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    entries, skipped = [], []
    record: dict[str, list[str]] | None = None
    last = None
    for line in text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").split("\n"):
        m = re.match(r"^([A-Z][A-Z0-9])  -(?: (.*))?$", line)
        if not m:
            if record is not None and last and line.strip():  # wrapped continuation line
                record[last][-1] += " " + line.strip()
            continue
        tag, value = m.group(1), (m.group(2) or "").strip()
        if tag == "TY":
            record, last = {"TY": [value]}, "TY"
            continue
        if record is None:
            continue
        if tag == "ER":
            csl = ris_to_csl(record)
            if csl.get("title"):
                entries.append({"csl": csl, "citekey": None, "tags": record.get("KW", [])})
            else:
                skipped.append("an RIS record without a title")
            record, last = None, None
            continue
        record.setdefault(tag, []).append(value)
        last = tag
    if record is not None:
        skipped.append("the last RIS record has no ER line")
    return entries, skipped


def _ris_name(value: str) -> dict[str, str]:
    parts = [p.strip() for p in value.split(",")]
    if len(parts) >= 2 and parts[0]:
        out = {"family": parts[0], "given": parts[1]}
        if len(parts) > 2 and parts[2]:
            out["suffix"] = parts[2]
        return {k: v for k, v in out.items() if v}
    return {"literal": value.strip()}


def ris_to_csl(r: dict[str, list[str]]) -> dict[str, Any]:
    def one(*tags: str) -> str:
        for t in tags:
            if r.get(t) and r[t][0]:
                return r[t][0]
        return ""

    csl: dict[str, Any] = {"type": RIS_TYPES.get(one("TY"), "document"), "title": one("TI", "T1", "CT", "BT")}
    authors = r.get("AU", []) + r.get("A1", [])
    if authors:
        csl["author"] = [_ris_name(a) for a in authors if a]
    editors = r.get("ED", []) + r.get("A2", []) if csl["type"] in ("chapter", "paper-conference") else r.get("ED", [])
    if editors:
        csl["editor"] = [_ris_name(a) for a in editors if a]
    container = one("JF", "JO", "T2", "JA", "J2") if csl["type"] != "book" else one("T3")
    if container:
        csl["container-title"] = container
    raw = one("PY", "Y1", "DA")
    if raw and (m := re.match(r"(\d{4})(?:[/-](\d{1,2}))?(?:[/-](\d{1,2}))?", raw)):
        csl["issued"] = {"date-parts": [[int(x) for x in m.groups() if x]]}
    for tag, key in (("VL", "volume"), ("IS", "issue"), ("PB", "publisher"), ("CY", "publisher-place"),
                     ("AB", "abstract"), ("N2", "abstract"), ("UR", "URL"), ("DO", "DOI"), ("ET", "edition")):
        if one(tag) and key not in csl:
            csl[key] = one(tag)
    sn = one("SN")
    if sn:
        csl["ISBN" if csl["type"] in ("book", "chapter") else "ISSN"] = sn
    sp, ep = one("SP"), one("EP")
    if sp:
        csl["page"] = f"{sp}-{ep}" if ep else sp
    return csl


# ------------------------------------------------------------------ BibTeX export


def _bib_escape(s: str) -> str:
    return re.sub(r"([&%$#_])", r"\\\1", str(s)).replace("{", "\\{").replace("}", "\\}")


def _bib_person(p: dict[str, Any]) -> str:
    if p.get("literal"):
        return "{" + _bib_escape(p["literal"]) + "}"
    family = _bib_escape(" ".join(x for x in (p.get("non-dropping-particle"), p.get("family")) if x))
    given = _bib_escape(p.get("given") or "")
    if p.get("suffix"):
        return f"{family}, {_bib_escape(p['suffix'])}, {given}"
    return f"{family}, {given}" if given else family


def to_bibtex(items: list[dict[str, Any]]) -> str:
    """BibTeX for [{citekey, csl}] — for people who keep a .bib file elsewhere."""
    out = []
    for it in items:
        csl = it["csl"]
        etype = CSL_BIB.get(str(csl.get("type") or ""), "misc")
        fields: list[tuple[str, str]] = [("title", "{" + _bib_escape(csl.get("title", "")) + "}")]
        for key in ("author", "editor"):
            if csl.get(key):
                fields.append((key, " and ".join(_bib_person(p) for p in csl[key] if isinstance(p, dict))))
        container = csl.get("container-title")
        if container:
            fields.append(("journal" if etype == "article" else "booktitle", _bib_escape(container)))
        parts = ((csl.get("issued") or {}).get("date-parts") or [[None]])[0]
        if parts and parts[0]:
            fields.append(("year", str(parts[0])))
        for key, bib in (("volume", "volume"), ("issue", "number"), ("page", "pages"), ("publisher", "publisher"),
                         ("ISBN", "isbn"), ("edition", "edition")):
            if csl.get(key):
                value = str(csl[key])
                fields.append((bib, _bib_escape(value.replace("-", "--") if key == "page" else value)))
        for key, bib in (("DOI", "doi"), ("URL", "url")):
            if csl.get(key):
                fields.append((bib, str(csl[key]).replace("{", "").replace("}", "")))
        if csl.get("type") == "article" and csl.get("number") and str(csl.get("publisher", "")).lower() == "arxiv":
            fields += [("eprint", str(csl["number"])), ("archiveprefix", "arXiv")]
        body = ",\n".join(f"  {k} = {{{v}}}" if not v.startswith("{") else f"  {k} = {v}" for k, v in fields)
        out.append(f"@{etype}{{{it['citekey']},\n{body}\n}}")
    return "\n\n".join(out) + ("\n" if out else "")
