"""MinerU conversion against a fake MinerU, the reading digest's checks, and the 精读 endpoints end to end."""

import asyncio
import base64
import hashlib
import io
import json
import time
import zipfile
from types import SimpleNamespace as NS

import httpx
import pytest

from ontology import digest, mineru, projects
from ontology.errors import OntologyError
from tests.pdfgen import make_pdf
from tests.test_library_api import client  # noqa: F401  (the fixture)
from workbench import library_api

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

MARKDOWN = """# Screens and Sleep

Teens who used phones before bed slept 23 minutes less.

![](images/page_0_image_1.png)

Figure 1: Sleep minutes by phone use.

![](images/page_1_table_1.png)

![](images/page_1_equation_1.png)

![](images/page_1_image_1_1.png)
"""

STRUCTURED = [{"type": "table", "content": {"image_source": {"path": "images/page_1_table_1.png"},
                                            "table_caption": [{"type": "text", "content": "Table 1: Regression results"}]}}]


def result_zip(markdown=MARKDOWN):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("paper/markdown.md", markdown)
        z.writestr("paper/structured_content.json", json.dumps(STRUCTURED))
        z.writestr("paper/middle_json.json", "{}")
        for name in ("page_0_image_1.png", "page_1_table_1.png", "page_1_equation_1.png", "page_1_image_1_1.png"):
            z.writestr(f"paper/images/{name}", PNG)
        z.writestr("../evil.md", "nope")
    return buf.getvalue()


class FakeMinerU:
    """Enough of the V1 Tasks API: create, upload, poll (running once, then completed), download."""

    def __init__(self, key="sk-test", zipped=None, fail=None):
        self.key, self.zipped, self.fail = key, zipped or result_zip(), fail
        self.uploaded, self.polls, self.seen = None, 0, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append((request.method, request.url.host, request.url.path, request.headers.get("authorization")))
        host, path = request.url.host, request.url.path
        if host == "mineru.net" and request.headers.get("authorization") != f"Bearer {self.key}":
            return httpx.Response(401, json={"error": {"code": "invalid_api_key", "message": "bad key"}})
        if path == "/api/v1/usage":
            return httpx.Response(200, json={"pages_used": 3})
        if request.method == "POST" and path == "/api/v1/tasks":
            body = json.loads(request.content)
            assert body["files"][0]["file_name"].endswith(".pdf")
            return httpx.Response(202, json={"task_id": "t1", "status": "waiting-file", "files": [
                {"file_id": "f1", "status": "waiting-file",
                 "upload": {"url": "https://oss.example.com/put/f1", "headers": {"Content-Type": "application/pdf"}}}]})
        if request.method == "PUT" and host == "oss.example.com":
            assert "authorization" not in request.headers  # the key never goes to the storage host
            self.uploaded = request.content
            return httpx.Response(200)
        if request.method == "GET" and path == "/api/v1/tasks/t1":
            self.polls += 1
            if self.fail:
                return httpx.Response(200, json={"task_id": "t1", "status": "failed", "files": [
                    {"file_id": "f1", "status": "failed", "error": {"code": self.fail, "message": "x"}}]})
            if self.polls < 2:
                return httpx.Response(200, json={"task_id": "t1", "status": "running",
                                                 "files": [{"file_id": "f1", "status": "running"}]})
            return httpx.Response(200, json={"task_id": "t1", "status": "completed", "files": [
                {"file_id": "f1", "status": "completed", "result_url": "https://cdn.example.com/r/f1.zip"}]})
        if host == "cdn.example.com":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=self.zipped)
        return httpx.Response(404, json={"error": {"code": "not_found"}})


@pytest.fixture
def fake(monkeypatch):
    f = FakeMinerU()
    monkeypatch.setattr(mineru, "TEST_TRANSPORT", httpx.MockTransport(f))
    monkeypatch.setenv(mineru.ENV_KEY, "sk-test")
    return f


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_status_and_cache_location(monkeypatch, tmp_path):
    st = mineru.status()
    assert st["hosted"] and st["host"] == "mineru.net" and not st["ready"]
    assert mineru.cache_root() == tmp_path / "papers"
    monkeypatch.setenv(mineru.ENV_URL, "http://127.0.0.1:8000/")
    st = mineru.status()
    assert not st["hosted"] and st["ready"] and st["url"] == "http://127.0.0.1:8000"
    with pytest.raises(OntologyError):
        mineru.folder("../etc")


def test_convert_caches_markdown_images_and_figures(fake, tmp_path):
    pdf = b"%PDF-1.4 fake"
    stages = []
    info = mineru.convert(sha(pdf), "zhang 2021", pdf, stages.append)
    assert stages == ["uploading", "parsing", "downloading"]
    assert fake.uploaded == pdf and info["images"] == 4 and info["file_name"] == "zhang 2021.pdf"
    base = tmp_path / "papers" / sha(pdf) / "mineru"
    assert (base / "markdown.md").read_text().startswith("# Screens")
    assert not (base / "middle_json.json").exists() and not (tmp_path / "evil.md").exists()
    assert mineru.manifest(sha(pdf))["task_id"] == "t1"
    assert not [p for p in (tmp_path / "papers").iterdir() if p.name.startswith(".mineru-")]  # no temp left

    figs = mineru.figures(sha(pdf))
    assert [(f["id"], f["name"], f["kind"], f["page"]) for f in figs] == [
        ("F1", "page_0_image_1.png", "image", 1), ("T1", "page_1_table_1.png", "table", 2)]
    assert figs[0]["caption"] == "Figure 1: Sleep minutes by phone use."
    assert figs[1]["caption"] == "Table 1: Regression results"

    assert mineru.asset(sha(pdf), "images/page_0_image_1.png").read_bytes() == PNG
    for bad in ("manifest.json", "images/../manifest.json", "../x.png", "images/a/b.png", "structured_content.json"):
        with pytest.raises(OntologyError):
            mineru.asset(sha(pdf), bad)
    assert mineru.check() == "mineru.net 可用，密钥有效"


def test_convert_errors(monkeypatch):
    pdf = b"%PDF-1.4 other"
    with pytest.raises(OntologyError, match="API 密钥"):
        mineru.convert(sha(pdf), "a.pdf", pdf)
    monkeypatch.setenv(mineru.ENV_KEY, "wrong")
    monkeypatch.setattr(mineru, "TEST_TRANSPORT", httpx.MockTransport(FakeMinerU()))
    with pytest.raises(OntologyError, match="拒绝了密钥"):
        mineru.convert(sha(pdf), "a.pdf", pdf)
    monkeypatch.setenv(mineru.ENV_KEY, "sk-test")
    monkeypatch.setattr(mineru, "TEST_TRANSPORT", httpx.MockTransport(FakeMinerU(fail="file_encrypted")))
    with pytest.raises(OntologyError, match="密码保护"):
        mineru.convert(sha(pdf), "a.pdf", pdf)
    monkeypatch.setattr(mineru, "TEST_TRANSPORT", httpx.MockTransport(FakeMinerU(zipped=b"not a zip")))
    with pytest.raises(OntologyError, match="无法打开"):
        mineru.convert(sha(pdf), "a.pdf", pdf)
    assert mineru.manifest(sha(pdf)) is None
    legacy = httpx.Response(200, json={"msgCode": "A0202", "msg": "user authenticate failed"})
    with pytest.raises(OntologyError, match="拒绝了密钥"):
        mineru._json(legacy)


# ------------------------------------------------------------------ the digest

FIGS = [{"id": "F1", "name": "page_0_image_1.png", "kind": "image", "page": 1, "caption": "Figure 1"},
        {"id": "T1", "name": "page_1_table_1.png", "kind": "table", "page": 2, "caption": "Table 1"}]

REPLY = {
    "graph": {"nodes": [{"id": "q", "label": "手机使用影响睡眠吗", "kind": "question", "detail": "研究问题"},
                        {"id": "d", "label": "青少年追踪数据", "kind": "data"},
                        {"id": "f", "label": "睡眠少 23 分钟", "kind": "finding"},
                        {"id": "f", "label": "duplicate id", "kind": "finding"},
                        {"id": "x", "label": "odd kind", "kind": "banana"}],
              "edges": [{"from": "q", "to": "d", "label": "检验"}, {"from": "d", "to": "f"}, {"from": "d", "to": "f"},
                        {"from": "f", "to": "f"}, {"from": "q", "to": "missing"}]},
    "logic": [{"title": "研究问题", "text": "睡前用手机  是否\n减少睡眠？"}, "a bare string", 42],
    "framework": [], "conclusions": [{"title": "睡眠减少", "text": "**23 分钟**"}],
    "experiments": [{"title": "数据", "text": "追踪调查"}], "methods": [{"title": "DiD", "text": "双重差分"}],
    "figures": [{"id": "f1", "title": "睡眠时长", "text": "手机组更短"}, {"id": "F9", "title": "不存在", "text": "x"}],
}


def test_parse_checks_and_trims_the_reply():
    out = digest.parse("好的，这是结果：\n```json\n" + json.dumps(REPLY, ensure_ascii=False) + "\n```", FIGS)
    g = out["graph"]
    assert [n["id"] for n in g["nodes"]] == ["n1", "n2", "n3", "n4"]
    assert g["nodes"][3]["kind"] == "concept"
    assert g["edges"] == [{"from": "n1", "to": "n2", "label": "检验"}, {"from": "n2", "to": "n3", "label": ""}]
    assert out["logic"] == [{"title": "研究问题", "text": "睡前用手机 是否 减少睡眠？"}, {"title": "", "text": "a bare string"}]
    assert out["figures"] == [{"title": "睡眠时长", "text": "手机组更短", "id": "F1"}]
    assert set(out) == {"graph", *digest.SECTIONS}
    with pytest.raises(OntologyError, match="JSON"):
        digest.parse("I cannot do that.", FIGS)
    with pytest.raises(OntologyError, match="没有给出"):
        digest.parse('{"graph": {}, "logic": []}', FIGS)


def test_prompt_lists_figures_and_cuts_long_papers():
    p = digest.prompt("x" * (digest.MAX_INPUT + 10), FIGS, "Screens")
    assert "- T1 (table, p.2, images/page_1_table_1.png): Table 1" in p and "cut off" in p and "Screens" in p


class FakeModel:
    def __init__(self, reply=REPLY, stop="end_turn"):
        self.reply, self.stop, self.calls, self.closed = reply, stop, [], False
        self.messages = self

    async def create(self, **kw):
        self.calls.append(kw)
        text = self.reply if isinstance(self.reply, str) else json.dumps(self.reply, ensure_ascii=False)
        return NS(content=[NS(type="text", text=text)], stop_reason=self.stop)

    async def close(self):
        self.closed = True


def test_generate_reports_a_cut_off_reply():
    with pytest.raises(OntologyError, match="截断"):
        asyncio.run(digest.generate(FakeModel('{"graph": {"nodes": [', stop="max_tokens"), "m", "# paper", FIGS))
    with pytest.raises(OntologyError, match="没有从"):
        asyncio.run(digest.generate(FakeModel(), "m", "  ", FIGS))


# ------------------------------------------------------------------ the 精读 endpoints

def wait_for(client, item_id, want=("ok", "failed"), seconds=20):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        r = client.get(f"/api/library/{item_id}/reading").json()
        if r["status"] in want:
            return r
        time.sleep(0.05)
    raise AssertionError(f"reading still {r['status']}")


def test_reading_flow(client, fake, monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(library_api, "model_client", lambda: (model, "claude-test"))
    item = client.post("/api/library", json={"csl": {"type": "article-journal", "title": "Screens and Sleep"}}).json()["item"]
    assert client.get(f"/api/library/{item['id']}/reading").json()["status"] == "none"
    assert client.post(f"/api/library/{item['id']}/reading", json={}).status_code == 400  # no PDF yet

    pdf = make_pdf(["Screens and sleep.", "Page two."])
    f = client.post(f"/api/library/{item['id']}/files", content=pdf,
                    headers={"X-Filename": "zhang.pdf", "Content-Type": "application/pdf"}).json()["file"]
    r = client.post(f"/api/library/{item['id']}/reading", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] in ("converting", "summarizing", "ok")
    done = wait_for(client, item["id"])
    assert done["status"] == "ok", done["error"]
    assert done["file_id"] == f["id"] and "sha256" not in done["files"][0]
    assert done["converted"]["images"] == 4 and done["model"] == "claude-test"
    assert [n["label"] for n in done["digest"]["graph"]["nodes"]][0] == "手机使用影响睡眠吗"
    assert done["digest"]["figures"][0]["id"] == "F1" and [x["id"] for x in done["figures"]] == ["F1", "T1"]
    assert fake.uploaded == pdf and model.closed and "Screens and Sleep" in model.calls[0]["messages"][0]["content"]

    img = client.get(f"/api/library/files/{f['id']}/mineru/images/page_0_image_1.png")
    assert img.content == PNG and img.headers["content-type"] == "image/png"
    b64 = client.get(f"/api/library/files/{f['id']}/mineru/images/page_0_image_1.png", params={"encoding": "base64"})
    assert base64.b64decode(b64.json()["data"]) == PNG
    assert client.get(f"/api/library/files/{f['id']}/mineru/images/..%2Fmanifest.json").status_code == 404
    md = client.get(f"/api/library/files/{f['id']}/mineru/markdown")
    assert md.text.startswith("# Screens") and "zhang.md" in md.headers["content-disposition"]

    # A second reading uses the cached conversion: MinerU is not asked again.
    tasks = sum(1 for s in fake.seen if s[0] == "POST")
    model.reply = "not json"
    client.post(f"/api/library/{item['id']}/reading", json={})
    failed = wait_for(client, item["id"], want=("failed",))
    assert "JSON" in failed["error"] and failed["digest"]["graph"]["nodes"]  # the old reading is kept
    assert sum(1 for s in fake.seen if s[0] == "POST") == tasks

    # A job that was running when the workbench stopped shows as failed, not as stuck.
    library_api._run(projects.DEFAULT, lambda lib, _: digest.mark(lib.conn, f["id"], item["id"], sha(pdf), "summarizing"))
    assert "中断" in client.get(f"/api/library/{item['id']}/reading").json()["error"]

    monkeypatch.delenv(mineru.ENV_KEY)
    r = client.post(f"/api/library/{item['id']}/reading", json={"reconvert": True})
    assert r.status_code == 400 and "MINERU_API_KEY" in r.json()["detail"]
