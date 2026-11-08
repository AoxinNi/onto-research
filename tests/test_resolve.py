import json

import httpx
import pytest

from ontology import bibio, resolve
from ontology.errors import OntologyError
from ontology.library import Library
from ontology.resolve import _fetch as real_fetch

from tests.pdfgen import make_pdf


def fake(routes):
    """A _fetch stand-in: url prefix -> (content type, body)."""
    calls = []

    def fetch(url, accept, max_bytes, any_host=False):
        calls.append(url)
        for prefix, (ctype, body) in routes.items():
            if url.startswith(prefix):
                return url, ctype, body if isinstance(body, bytes) else body.encode("utf-8")
        raise OntologyError("not found at the source; check the identifier")

    fetch.calls = calls
    return fetch


def test_classify_recognises_identifier_forms():
    assert resolve.classify("10.1037/a0012345") == ("doi", "10.1037/a0012345")
    assert resolve.classify("https://doi.org/10.1037/A0012345") == ("doi", "10.1037/a0012345")
    assert resolve.classify("doi: 10.1016/j.sleep.2020.01.002.") == ("doi", "10.1016/j.sleep.2020.01.002")
    assert resolve.classify("arXiv:2301.01234v2") == ("arxiv", "2301.01234")
    assert resolve.classify("https://arxiv.org/pdf/2301.01234.pdf") == ("arxiv", "2301.01234")
    assert resolve.classify("hep-th/9901001") == ("arxiv", "hep-th/9901001")
    assert resolve.classify("ISBN 978-0-262-03384-8") == ("isbn", "9780262033848")
    assert resolve.classify("0-306-40615-2") == ("isbn", "0306406152")
    assert resolve.classify("https://openalex.org/W2741809807") == ("openalex", "W2741809807")
    assert resolve.classify("https://example.org/post") == ("url", "https://example.org/post")
    with pytest.raises(OntologyError, match="check digit"):
        resolve.classify("978-0-262-03384-9")
    with pytest.raises(OntologyError):
        resolve.classify("sleep and screens")


def test_doi_resolves_csl_and_drops_bulky_fields(monkeypatch):
    csl = {"type": "article-journal", "title": ["Screens and Sleep"], "DOI": "10.1000/X",
           "author": [{"family": "Zhang", "given": "Wei"}], "issued": {"date-parts": [[2021, 3]]},
           "container-title": ["Sleep Medicine"], "abstract": "<jats:p>We <b>found</b> links.</jats:p>",
           "reference": [{"key": "r1"}] * 50, "license": [{}]}
    monkeypatch.setattr(resolve, "_fetch", fake({"https://doi.org/10.1000/x": ("application/json", json.dumps(csl))}))
    out = resolve.resolve("10.1000/x")
    assert out["origin"] == {"kind": "doi", "ref": "10.1000/x"}
    assert out["csl"]["title"] == "Screens and Sleep"
    assert out["csl"]["container-title"] == "Sleep Medicine"
    assert out["csl"]["abstract"] == "We found links."
    assert "reference" not in out["csl"] and "license" not in out["csl"]


ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
 <entry>
  <title>Attention   Is All
   You Need</title>
  <published>2017-06-12T17:57:34Z</published>
  <summary>  The dominant sequence models...  </summary>
  <author><name>Ashish Vaswani</name></author>
  <author><name>Noam Shazeer</name></author>
  <arxiv:doi>10.48550/arXiv.1706.03762</arxiv:doi>
 </entry>
</feed>"""


def test_arxiv_metadata_and_pdf(monkeypatch, conn):
    pdf = make_pdf(["Attention is all you need. The Transformer."])
    monkeypatch.setattr(resolve, "_fetch", fake({
        "https://export.arxiv.org/api/query?id_list=1706.03762": ("application/atom+xml", ATOM),
        "https://arxiv.org/pdf/1706.03762": ("application/pdf", pdf)}))
    out = resolve.resolve("arXiv:1706.03762")
    assert out["csl"]["title"] == "Attention Is All You Need"
    assert out["csl"]["author"][0] == {"family": "Vaswani", "given": "Ashish"}
    assert out["csl"]["issued"] == {"date-parts": [[2017, 6, 12]]}
    assert out["csl"]["DOI"] == "10.48550/arxiv.1706.03762"
    assert out["file"]["media_type"] == "application/pdf"

    lib = Library(conn, actor="test-agent")
    added = lib.add_resolved(out)
    assert added["created"] and added["item"]["kind"] == "preprint"
    assert added["item"]["citekey"] == "vaswani2017attention"
    assert added["file"]["pages"] == 1
    assert lib.search("Transformer")[0]["id"] == added["item"]["id"]
    again = lib.add_resolved(out)  # same DOI: the existing item, no second file
    assert not again["created"] and "file" not in again


def test_arxiv_without_paper_is_an_error(monkeypatch):
    empty = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Error</title></entry></feed>'
    monkeypatch.setattr(resolve, "_fetch", fake({"https://export.arxiv.org": ("application/atom+xml", empty)}))
    with pytest.raises(OntologyError, match="no paper"):
        resolve.resolve("2301.99999", with_file=False)


def test_isbn_and_openalex(monkeypatch):
    book = {"ISBN:9780262033848": {"title": "Introduction to Algorithms", "authors": [{"name": "Thomas H. Cormen"}],
                                   "publishers": [{"name": "MIT Press"}], "publish_date": "July 31, 2009"}}
    work = {"id": "https://openalex.org/W1", "display_name": "Sleep in teens", "type": "article",
            "publication_year": 2020, "authorships": [{"author": {"display_name": "李明"}}],
            "primary_location": {"source": {"display_name": "Child Dev"}}, "biblio": {"volume": "9", "first_page": "1",
                                                                                      "last_page": "9"},
            "abstract_inverted_index": {"Teens": [0], "sleep": [1], "less": [2]}, "cited_by_count": 4}
    monkeypatch.setattr(resolve, "_fetch", fake({
        "https://openlibrary.org/api/books": ("application/json", json.dumps(book)),
        "https://api.openalex.org/works/W1": ("application/json", json.dumps(work)),
        "https://api.openalex.org/works?search=": ("application/json", json.dumps({"results": [work]}))}))
    b = resolve.resolve("9780262033848")
    assert b["csl"]["type"] == "book" and b["csl"]["publisher"] == "MIT Press"
    assert b["csl"]["issued"] == {"date-parts": [[2009]]}
    assert b["csl"]["author"] == [{"family": "Cormen", "given": "Thomas H."}]

    w = resolve.resolve("openalex:W1")
    assert w["csl"]["author"] == [{"literal": "李明"}] and w["csl"]["page"] == "1-9"
    assert w["csl"]["abstract"] == "Teens sleep less"

    hits = resolve.search_scholarly("teen sleep", year_from=2015)
    assert hits[0]["id"] == "openalex:W1" and hits[0]["authors"] == ["李明"]
    assert "from_publication_date:2015-01-01" in resolve._fetch.calls[-1]


PAGE = """<!doctype html><html><head><title>Ignored | Site</title>
<meta name="citation_title" content="Blue light and melatonin">
<meta name="citation_author" content="Chang, Anne-Marie"><meta name="citation_author" content="Czeisler, Charles">
<meta name="citation_publication_date" content="2015/01/27"><meta name="citation_journal_title" content="PNAS">
<meta name="citation_volume" content="112"><meta name="citation_firstpage" content="1232">
</head><body><nav>Home | About</nav><article><h1>Blue light</h1><p>""" + ("Reading on a light-emitting device "
                                                                          "before bed delays sleep. " * 30) + \
    """</p><script>track()</script></article><footer>(c) site</footer></body></html>"""


def test_web_capture_reads_citation_meta_and_text(monkeypatch):
    monkeypatch.setenv("WORKBENCH_LIBRARY_FETCH_ANY", "1")
    monkeypatch.setattr(resolve, "_fetch", fake({"https://journal.example/article": ("text/html", PAGE)}))
    out = resolve.resolve("https://journal.example/article")
    csl = out["csl"]
    assert csl["title"] == "Blue light and melatonin" and csl["type"] == "article-journal"
    assert csl["author"][0] == {"family": "Chang", "given": "Anne-Marie"}
    assert csl["issued"] == {"date-parts": [[2015, 1, 27]]} and csl["container-title"] == "PNAS"
    text = out["file"]["bytes"].decode()
    assert out["file"]["media_type"] == "text/x-web-extract"
    assert "delays sleep" in text and "track()" not in text and "Home | About" not in text


def test_web_capture_is_off_in_the_cloud(monkeypatch):
    monkeypatch.delenv("WORKBENCH_LIBRARY_FETCH_ANY", raising=False)
    monkeypatch.setenv("WORKBENCH_CLOUDBASE_ENV_ID", "env-1")
    with pytest.raises(OntologyError, match="does not fetch arbitrary web pages"):
        resolve.resolve("https://example.org/page")


def test_fetch_checks_hosts_redirects_and_size(monkeypatch):
    def handler(request):
        if request.url.host == "doi.org":
            return httpx.Response(302, headers={"location": "https://evil.example/steal"})
        if request.url.host == "api.openalex.org":
            return httpx.Response(200, content=b"x" * 100, headers={"content-type": "application/json"})
        return httpx.Response(200, content=b"secret")

    monkeypatch.setattr(resolve, "_TRANSPORT", httpx.MockTransport(handler))
    with pytest.raises(OntologyError, match="not a metadata service"):
        real_fetch("https://doi.org/10.1000/x", "application/json", 1000)
    with pytest.raises(OntologyError, match="too large"):
        real_fetch("https://api.openalex.org/works/W1", "application/json", 10)
    assert real_fetch("https://api.openalex.org/works/W1", "*/*", 1000)[2] == b"x" * 100
    with pytest.raises(OntologyError, match="HTTPS only"):
        real_fetch("http://doi.org/10.1000/x", "*/*", 1000)
    with pytest.raises(OntologyError, match="not a public web address"):
        real_fetch("http://127.0.0.1:80/admin", "*/*", 1000, any_host=True)
    with pytest.raises(OntologyError, match="not a public web address"):
        real_fetch("http://localhost/admin", "*/*", 1000, any_host=True)
    with pytest.raises(OntologyError, match="can be fetched"):
        real_fetch("file:///etc/passwd", "*/*", 1000, any_host=True)


BIB = r"""
@string{sm = "Sleep Medicine"}
@comment{ignored @article{nope, title={x}} }
@article{zhang2021,
  author = {Zhang, Wei and M{\"u}ller, J{\"o}rg and van der Berg, Anna and {World Health Organization}},
  title = {Screens and {Sleep}: a {\em review}},
  journal = sm,
  year = 2021, month = mar,
  volume = {12}, number = 3, pages = {100--120},
  doi = {10.1000/ABC}, keywords = {sleep; screens}
}
@inproceedings{li2020,
  author = "Li, Ming",
  title = "Blue light at night",
  booktitle = {Proc. Sleep Conf.},
  year = {2020}
}
@misc{notitle, author = {Nobody}}
"""


def test_bibtex_parsing():
    out = bibio.parse(BIB, "refs.bib")
    assert out["format"] == "bibtex" and out["skipped"] == ["@misc{notitle}: no title"]
    a, b = (e["csl"] for e in out["entries"])
    assert a["title"] == "Screens and Sleep: a review"
    assert a["author"] == [{"family": "Zhang", "given": "Wei"}, {"family": "Müller", "given": "Jörg"},
                           {"given": "Anna", "family": "van der Berg"}, {"literal": "World Health Organization"}]
    assert a["container-title"] == "Sleep Medicine" and a["issued"] == {"date-parts": [[2021, 3]]}
    assert a["page"] == "100-120" and a["issue"] == "3" and a["DOI"] == "10.1000/ABC"
    assert out["entries"][0]["citekey"] == "zhang2021" and out["entries"][0]["tags"] == ["sleep", "screens"]
    assert b["type"] == "paper-conference" and b["container-title"] == "Proc. Sleep Conf."


RIS = """TY  - JOUR
AU  - Chang, Anne-Marie
AU  - Czeisler, Charles A.
TI  - Evening use of light-emitting eReaders
JO  - PNAS
PY  - 2015/01/27
VL  - 112
SP  - 1232
EP  - 1237
DO  - 10.1073/pnas.1418490112
KW  - melatonin
ER  -
TY  - BOOK
AU  - Walker, Matthew
TI  - Why We Sleep
PB  - Scribner
PY  - 2017
SN  - 9781501144318
ER  -
"""


def test_ris_and_csl_json_parsing():
    out = bibio.parse(RIS)
    assert out["format"] == "ris" and len(out["entries"]) == 2
    a, b = (e["csl"] for e in out["entries"])
    assert a["type"] == "article-journal" and a["page"] == "1232-1237" and a["issued"] == {"date-parts": [[2015, 1, 27]]}
    assert a["author"][1] == {"family": "Czeisler", "given": "Charles A."}
    assert out["entries"][0]["tags"] == ["melatonin"]
    assert b["type"] == "book" and b["ISBN"] == "9781501144318" and b["publisher"] == "Scribner"

    j = bibio.parse(json.dumps([{"id": "walker2017", "type": "book", "title": "Why We Sleep"}, {"type": "book"}]))
    assert j["format"] == "csl-json" and j["entries"][0]["citekey"] == "walker2017" and len(j["skipped"]) == 1
    with pytest.raises(OntologyError):
        bibio.parse("just some words")


def test_import_entries_dedupes_and_renames_keys(conn):
    lib = Library(conn, actor="human")
    lib.add_item({"title": "Already here", "DOI": "10.1000/abc", "type": "article-journal"}, {"kind": "manual"},
                 citekey="li2020")
    parsed = bibio.parse(BIB, "refs.bib")
    out = lib.import_entries(parsed["entries"], {"kind": "import", "format": "bibtex"}, tags=["imported"])
    assert [x["title"] for x in out["existing"]] == ["Already here"]  # same DOI as zhang2021
    assert [x["citekey"] for x in out["added"]] == ["li2020a"]
    item = lib.get_item(out["added"][0]["id"])
    assert item["tags"] == ["imported"] and item["kind"] == "conference"


def test_bibtex_export_round_trips():
    entries = bibio.parse(BIB, "refs.bib")["entries"]
    text = bibio.to_bibtex([{"citekey": e["citekey"], "csl": e["csl"]} for e in entries])
    again = bibio.parse(text, "x.bib")["entries"]
    assert [e["citekey"] for e in again] == ["zhang2021", "li2020"]
    assert again[0]["csl"]["author"] == entries[0]["csl"]["author"]
    assert again[0]["csl"]["page"] == "100-120" and again[0]["csl"]["title"] == entries[0]["csl"]["title"]
