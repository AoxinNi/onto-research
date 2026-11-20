"""Turn an identifier (DOI, arXiv id, ISBN, OpenAlex work, web address) into citable metadata, and search OpenAlex.

Metadata always comes from a resolver, never from the agent's memory. Requests go only to the hosts in
WORKBENCH_LIBRARY_FETCH_HOSTS (by default the DOI, Crossref, DataCite, arXiv, OpenAlex and Open Library services),
redirects are followed only to those hosts, and every response has a size cap. Capturing an arbitrary web page
needs WORKBENCH_LIBRARY_FETCH_ANY=1 (on by default only when the workbench runs locally); such a fetch is refused
for addresses that are not public. The address is checked before connecting, so a host whose DNS answer changes
between the check and the connection is not fully covered; keep FETCH_ANY off on servers with sensitive
neighbours.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin, urlsplit
from xml.etree import ElementTree

import httpx

from .errors import OntologyError
from .library import clean_doi

DEFAULT_HOSTS = ("doi.org", "dx.doi.org", "api.crossref.org", "data.crossref.org", "api.datacite.org",
                 "data.datacite.org", "export.arxiv.org", "arxiv.org", "api.openalex.org", "openlibrary.org")
TIMEOUT = 15.0
MAX_META = 2 * 1024 * 1024
MAX_PAGE = 10 * 1024 * 1024
MAX_PDF = 50 * 1024 * 1024
HOPS = 3
_TRANSPORT: httpx.BaseTransport | None = None  # tests put a mock transport here
UA = "onto-research/0.1 (research workbench; +https://github.com/)"
ARXIV_NEW = re.compile(r"^(\d{4}\.\d{4,5})(v\d+)?$")
ARXIV_OLD = re.compile(r"^([a-z-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?$")
OPENALEX = re.compile(r"(?:openalex:|openalex\.org/(?:works/)?)(W\d+)", re.I)
# CSL fields Crossref returns that are large and never cited
DROP = {"reference", "link", "license", "assertion", "funder", "relation", "update-to", "content-domain",
        "indexed", "deposited", "resource", "score", "references-count", "is-referenced-by-count", "member",
        "prefix", "source", "subject", "short-container-title", "original-title", "journal-issue", "institution"}


def allowed_hosts() -> set[str]:
    raw = os.environ.get("WORKBENCH_LIBRARY_FETCH_HOSTS")
    return {h.strip().lower() for h in raw.split(",") if h.strip()} if raw else set(DEFAULT_HOSTS)


def fetch_any_enabled() -> bool:
    default = "0" if os.environ.get("WORKBENCH_CLOUDBASE_ENV_ID") else "1"
    return os.environ.get("WORKBENCH_LIBRARY_FETCH_ANY", default) == "1"


# Proxies such as Clash or Surge in fake-IP / TUN mode answer every DNS lookup with an address from this range
# (RFC 2544, reserved for benchmarks) and connect to the real host themselves. Treated as public on a local
# workbench, where such a proxy is common; the cloud entry, which may not reach private networks, keeps it out.
FAKE_IP = ipaddress.ip_network("198.18.0.0/15")


def fake_ip_allowed() -> bool:
    default = "0" if os.environ.get("WORKBENCH_CLOUDBASE_ENV_ID") else "1"
    return os.environ.get("WORKBENCH_LIBRARY_FAKE_IP", default) == "1"


def _public(host: str, port: int) -> bool:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    fake_ok = fake_ip_allowed()
    def ok(addr: str) -> bool:
        ip = ipaddress.ip_address(addr)
        return ip.is_global or (fake_ok and ip.version == 4 and ip in FAKE_IP)
    return bool(infos) and all(ok(i[4][0]) for i in infos)


def _check(url: str, any_host: bool) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
        raise OntologyError(f"not a web address that can be fetched: {url[:200]}")
    host = parts.hostname.lower()
    if host in allowed_hosts():
        if parts.scheme != "https":
            raise OntologyError(f"{host} is fetched over HTTPS only")
        return
    if not any_host:
        raise OntologyError(f"{host} is not a metadata service this workbench may contact")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in (80, 443) or not _public(host, port):
        raise OntologyError(f"{host} is not a public web address")


def _fetch(url: str, accept: str, max_bytes: int, any_host: bool = False) -> tuple[str, str, bytes]:
    """GET with checked redirects and a size cap. Returns (final url, content type, body)."""
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False, transport=_TRANSPORT,
                      headers={"User-Agent": UA, "Accept": accept}) as client:
        for _ in range(HOPS + 1):
            _check(url, any_host)
            with client.stream("GET", url) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code == 404:
                    raise OntologyError("not found at the source; check the identifier")
                if r.status_code >= 400:
                    raise OntologyError(f"the source answered HTTP {r.status_code}")
                if int(r.headers.get("content-length") or 0) > max_bytes:
                    raise OntologyError("the response is too large")
                body = bytearray()
                for part in r.iter_bytes():
                    body += part
                    if len(body) > max_bytes:
                        raise OntologyError("the response is too large")
                return url, r.headers.get("content-type", "").split(";")[0].strip().lower(), bytes(body)
    raise OntologyError("too many redirects")


def _json(url: str, accept: str = "application/json") -> Any:
    _, _, body = _fetch(url, accept, MAX_META)
    try:
        return json.loads(body)
    except ValueError:
        raise OntologyError("the source did not return readable metadata") from None


# ------------------------------------------------------------------ identifiers


def _isbn_ok(digits: str) -> bool:
    if len(digits) == 10:
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(digits))
        return total % 11 == 0
    if len(digits) == 13 and digits.isdigit():
        return sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(digits)) % 10 == 0
    return False


def classify(identifier: str) -> tuple[str, str]:
    """('doi'|'arxiv'|'isbn'|'openalex'|'url', normalised value)."""
    s = " ".join((identifier or "").split())
    if not s:
        raise OntologyError("the identifier is empty")
    low = s.lower()
    if m := OPENALEX.search(s):
        return "openalex", m.group(1).upper()
    if low.startswith("arxiv:") or "arxiv.org/" in low:
        tail = re.split(r"arxiv:|arxiv\.org/(?:abs|pdf)/", s, flags=re.I)[-1].removesuffix(".pdf").strip("/ ")
        m = ARXIV_NEW.match(tail) or ARXIV_OLD.match(tail)
        if not m:
            raise OntologyError(f"not an arXiv id: {s}")
        return "arxiv", m.group(1)
    if m := (ARXIV_NEW.match(s) or ARXIV_OLD.match(s)):
        return "arxiv", m.group(1)
    if low.startswith("doi:") or "doi.org/" in low or re.match(r"^10\.\d{4,9}/", s):
        doi = clean_doi(s)
        if not doi:
            raise OntologyError(f"not a DOI: {s}")
        return "doi", doi
    digits = re.sub(r"[\s-]", "", re.sub(r"^isbn[:\s]*", "", s, flags=re.I)).upper()
    if re.fullmatch(r"\d{9}[\dX]|\d{13}", digits):
        if not _isbn_ok(digits):
            raise OntologyError(f"ISBN check digit does not match: {s}")
        return "isbn", digits
    if re.match(r"^https?://", s, re.I):
        return "url", s
    raise OntologyError("give a DOI, an arXiv id, an ISBN, an OpenAlex work id or a web address")


# ------------------------------------------------------------------ resolvers


def _strip_tags(text: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", text or "").split())


def _clean_csl(csl: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in csl.items() if k not in DROP}
    if isinstance(out.get("abstract"), str):
        out["abstract"] = _strip_tags(out["abstract"])[:20000]
    if isinstance(out.get("title"), list):
        out["title"] = out["title"][0] if out["title"] else ""
    if isinstance(out.get("container-title"), list):
        out["container-title"] = out["container-title"][0] if out["container-title"] else ""
    return out


def doi(value: str) -> dict[str, Any]:
    data = _json(f"https://doi.org/{quote(value, safe='/:;()._-')}", "application/vnd.citationstyles.csl+json")
    if not isinstance(data, dict) or not data.get("title"):
        raise OntologyError("the DOI service returned no title")
    csl = _clean_csl(data)
    csl["DOI"] = value
    return {"csl": csl, "origin": {"kind": "doi", "ref": value}}


def arxiv(value: str, with_pdf: bool = True) -> dict[str, Any]:
    _, _, body = _fetch(f"https://export.arxiv.org/api/query?id_list={quote(value)}", "application/atom+xml", MAX_META)
    ns = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}
    try:
        entry = ElementTree.fromstring(body).find("a:entry", ns)
    except ElementTree.ParseError:
        raise OntologyError("arXiv returned unreadable metadata") from None
    title = " ".join((entry.findtext("a:title", "", ns) if entry is not None else "").split())
    if entry is None or not title or title.lower() == "error":
        raise OntologyError(f"arXiv has no paper {value}")
    published = entry.findtext("a:published", "", ns)[:10]
    csl: dict[str, Any] = {
        "type": "article", "title": title, "publisher": "arXiv", "number": value,
        "URL": f"https://arxiv.org/abs/{value}",
        "author": [_split_name(" ".join(a.findtext("a:name", "", ns).split())) for a in entry.findall("a:author", ns)],
        "abstract": " ".join(entry.findtext("a:summary", "", ns).split()),
    }
    if published[:4].isdigit():
        csl["issued"] = {"date-parts": [[int(x) for x in published.split("-") if x.isdigit()]]}
    if d := clean_doi(entry.findtext("x:doi", "", ns)):
        csl["DOI"] = d
    out = {"csl": csl, "origin": {"kind": "arxiv", "ref": value}}
    if with_pdf:
        try:
            _, ctype, pdf = _fetch(f"https://arxiv.org/pdf/{value}", "application/pdf", MAX_PDF)
            if pdf[:5] == b"%PDF-":
                out["file"] = {"filename": f"{value.replace('/', '_')}.pdf", "bytes": pdf, "media_type": "application/pdf"}
        except OntologyError:
            pass  # metadata is still worth having; the PDF can be attached later
    return out


def isbn(value: str) -> dict[str, Any]:
    data = _json(f"https://openlibrary.org/api/books?bibkeys=ISBN:{value}&format=json&jscmd=data")
    book = (data or {}).get(f"ISBN:{value}")
    if not book or not book.get("title"):
        raise OntologyError(f"Open Library has no book with ISBN {value}")
    csl: dict[str, Any] = {"type": "book", "title": book["title"] + (f": {book['subtitle']}" if book.get("subtitle") else ""),
                           "ISBN": value, "author": [_split_name(a.get("name", "")) for a in book.get("authors", [])]}
    if m := re.search(r"\b(1[5-9]\d\d|20\d\d)\b", str(book.get("publish_date") or "")):
        csl["issued"] = {"date-parts": [[int(m.group(1))]]}
    if book.get("publishers"):
        csl["publisher"] = book["publishers"][0].get("name")
    if book.get("number_of_pages"):
        csl["number-of-pages"] = str(book["number_of_pages"])
    return {"csl": csl, "origin": {"kind": "isbn", "ref": value}}


OPENALEX_TYPES = {"article": "article-journal", "book": "book", "book-chapter": "chapter", "dissertation": "thesis",
                  "preprint": "article", "report": "report", "dataset": "dataset", "proceedings-article": "paper-conference"}


def _openalex_csl(w: dict[str, Any]) -> dict[str, Any]:
    csl: dict[str, Any] = {
        "type": OPENALEX_TYPES.get(w.get("type") or "", "article-journal"),
        "title": w.get("display_name") or w.get("title") or "",
        "author": [_split_name((a.get("author") or {}).get("display_name", "")) for a in w.get("authorships") or []],
    }
    if w.get("publication_year"):
        csl["issued"] = {"date-parts": [[int(w["publication_year"])]]}
    source = ((w.get("primary_location") or {}).get("source") or {})
    if source.get("display_name"):
        csl["container-title"] = source["display_name"]
    biblio = w.get("biblio") or {}
    for k_from, k_to in (("volume", "volume"), ("issue", "issue")):
        if biblio.get(k_from):
            csl[k_to] = str(biblio[k_from])
    if biblio.get("first_page"):
        csl["page"] = biblio["first_page"] + (f"-{biblio['last_page']}" if biblio.get("last_page") else "")
    if d := clean_doi(w.get("doi")):
        csl["DOI"] = d
    if abstract := _abstract(w.get("abstract_inverted_index")):
        csl["abstract"] = abstract
    return csl


def _abstract(inverted: dict[str, list[int]] | None) -> str:
    if not inverted:
        return ""
    words: dict[int, str] = {}
    for word, places in inverted.items():
        for p in places:
            words[p] = word
    return " ".join(words[i] for i in sorted(words))


def openalex(value: str) -> dict[str, Any]:
    w = _json(f"https://api.openalex.org/works/{value}")
    if not isinstance(w, dict) or not w.get("display_name"):
        raise OntologyError(f"OpenAlex has no work {value}")
    csl = _openalex_csl(w)
    if csl.get("DOI"):
        try:
            out = doi(csl["DOI"])
            out["origin"] = {"kind": "openalex", "ref": value, "doi": csl["DOI"]}
            if not out["csl"].get("abstract") and csl.get("abstract"):
                out["csl"]["abstract"] = csl["abstract"]
            return out
        except OntologyError:
            pass
    return {"csl": csl, "origin": {"kind": "openalex", "ref": value}}


def search_scholarly(query: str, year_from: int | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """Candidates from OpenAlex (which also indexes arXiv). Nothing is stored; add one with its id."""
    q = " ".join((query or "").split())
    if not q:
        raise OntologyError("query is empty")
    url = f"https://api.openalex.org/works?search={quote(q)}&per-page={max(1, min(limit, 25))}"
    if year_from:
        url += f"&filter=from_publication_date:{int(year_from)}-01-01"
    data = _json(url)
    out = []
    for w in (data or {}).get("results", []):
        csl = _openalex_csl(w)
        out.append({"id": "openalex:" + str(w.get("id", "")).rsplit("/", 1)[-1], "doi": csl.get("DOI"),
                    "title": csl["title"], "year": w.get("publication_year"),
                    "authors": [a.get("literal") or f"{a.get('given', '')} {a.get('family', '')}".strip()
                                for a in csl["author"][:5]],
                    "venue": csl.get("container-title"), "type": w.get("type"), "cited_by": w.get("cited_by_count"),
                    "abstract": (csl.get("abstract") or "")[:600]})
    return out


# ------------------------------------------------------------------ web pages


class _Page(HTMLParser):
    """Meta tags (citation_*, og:*, <title>) and the readable text of a page; article/main text wins."""

    SKIP = {"script", "style", "nav", "header", "footer", "aside", "form", "noscript", "svg", "button", "template"}
    BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre", "td", "th", "dd", "dt", "figcaption", "br", "div"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}
        self.title = ""
        self._in_title = False
        self._skip = 0
        self._main = 0
        self.all: list[str] = []
        self.main: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, []).append(a["content"].strip())
        elif tag == "title":
            self._in_title = True
        elif tag in self.SKIP:
            self._skip += 1
        elif tag in ("article", "main"):
            self._main += 1
        if tag in self.BLOCK:
            self._push("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in ("article", "main") and self._main:
            self._main -= 1
        if tag in self.BLOCK:
            self._push("\n")

    def _push(self, text: str) -> None:
        if self._skip:
            return
        self.all.append(text)
        if self._main:
            self.main.append(text)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        else:
            self._push(data)

    def text(self) -> str:
        body = "".join(self.main) if len("".join(self.main).strip()) > 500 else "".join(self.all)
        lines = [" ".join(line.split()) for line in body.split("\n")]
        return "\n".join(line for line in lines if line)


def _first(meta: dict[str, list[str]], *keys: str) -> str:
    for k in keys:
        if meta.get(k):
            return meta[k][0]
    return ""


def web(url: str) -> dict[str, Any]:
    if not fetch_any_enabled():
        raise OntologyError("this server does not fetch arbitrary web pages; give a DOI, arXiv id or ISBN, "
                            "or save the page as PDF and upload it")
    final, ctype, body = _fetch(url, "text/html,application/pdf;q=0.9", MAX_PDF, any_host=True)
    name = urlsplit(final).path.rsplit("/", 1)[-1] or urlsplit(final).hostname or "page"
    if body[:5] == b"%PDF-":
        return {"csl": {"type": "document", "title": name.removesuffix(".pdf") or final, "URL": final},
                "origin": {"kind": "url", "ref": final}, "needs_title": True,
                "file": {"filename": name if name.endswith(".pdf") else name + ".pdf", "bytes": body,
                         "media_type": "application/pdf"}}
    if "html" not in ctype and not body.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        raise OntologyError("the address is neither a web page nor a PDF")
    page = _Page()
    page.feed(body[:MAX_PAGE].decode("utf-8", errors="replace"))
    m = page.meta
    title = _first(m, "citation_title", "dc.title", "og:title") or " ".join(page.title.split())
    csl: dict[str, Any] = {"type": "webpage", "title": title or final, "URL": final}
    authors = m.get("citation_author") or m.get("dc.creator") or m.get("author") or []
    if authors:
        csl["author"] = [_split_name(a) for a in authors[:50]]
    date = _first(m, "citation_publication_date", "citation_date", "dc.date", "article:published_time")
    if y := re.search(r"\b(1[5-9]\d\d|20\d\d)\b", date):
        csl["issued"] = {"date-parts": [[int(p) for p in re.findall(r"\d+", date)[:3]]] if re.match(r"\d{4}", date)
                         else [[int(y.group(1))]]}
    if journal := _first(m, "citation_journal_title", "citation_conference_title"):
        csl["type"] = "article-journal" if m.get("citation_journal_title") else "paper-conference"
        csl["container-title"] = journal
    elif site := _first(m, "og:site_name"):
        csl["container-title"] = site
    for k_from, k_to in (("citation_volume", "volume"), ("citation_issue", "issue")):
        if m.get(k_from):
            csl[k_to] = m[k_from][0]
    if m.get("citation_firstpage"):
        csl["page"] = m["citation_firstpage"][0] + (f"-{m['citation_lastpage'][0]}" if m.get("citation_lastpage") else "")
    out: dict[str, Any] = {"csl": csl, "origin": {"kind": "url", "ref": final}}
    if d := clean_doi(_first(m, "citation_doi", "dc.identifier", "prism.doi")):
        try:
            resolved = doi(d)
            resolved["csl"].setdefault("URL", final)
            out = {"csl": resolved["csl"], "origin": {"kind": "url", "ref": final, "doi": d}}
        except OntologyError:
            csl["DOI"] = d
    text = page.text()
    if text:
        out["file"] = {"filename": (title or name)[:80] + ".txt", "bytes": text.encode("utf-8"),
                       "media_type": "text/x-web-extract"}
    return out


def _split_name(name: str) -> dict[str, str]:
    """CSL name from 'Family, Given', 'Given Family', or a CJK name kept whole."""
    name = " ".join((name or "").split())
    if not name:
        return {"literal": "?"}
    if re.search(r"[一-鿿぀-ヿ가-힯]", name):
        return {"literal": name.replace(" ", "")}
    if "," in name:
        family, given = (x.strip() for x in name.split(",", 1))
        return {"family": family, "given": given} if given else {"family": family}
    parts = name.split(" ")
    return {"family": parts[-1], "given": " ".join(parts[:-1])} if len(parts) > 1 else {"family": name}


def resolve(identifier: str, with_file: bool = True) -> dict[str, Any]:
    """{csl, origin, file?} for an identifier. `file` is a PDF (arXiv, a PDF link) or a web page's text."""
    kind, value = classify(identifier)
    if kind == "doi":
        return doi(value)
    if kind == "arxiv":
        return arxiv(value, with_pdf=with_file)
    if kind == "isbn":
        return isbn(value)
    if kind == "openalex":
        return openalex(value)
    return web(value)
