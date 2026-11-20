"""Converting a library PDF to Markdown with MinerU, cached on this computer by the PDF's SHA-256.

Everything MinerU produces (markdown.md, structured_content.json, images/) can be made again from the PDF that the
library keeps, so it lives in a local cache folder rather than in the database:

    <cache>/<sha256 of the PDF>/mineru/
        markdown.md  structured_content.json  images/…  manifest.json

The same PDF in two projects is converted once. The cache folder is ONTO_PAPER_CACHE, by default the user's
application-data folder.

Conversion uses MinerU's V1 Tasks API (create a task, PUT the PDF to the link it returns, poll, download the result
ZIP). By default that is the hosted service at mineru.net, which needs a key and receives the PDF; the address can
point at a self-hosted `mineru-kit api-server` instead, which needs none. The connection is set in 平台设置 (saved
in settings_file()); MINERU_API_KEY / MINERU_API_URL in the environment are the fallback.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urljoin, urlsplit

import httpx

from .errors import OntologyError

ENV_URL = "MINERU_API_URL"
ENV_KEY = "MINERU_API_KEY"
ENV_CACHE = "ONTO_PAPER_CACHE"
ENV_SETTINGS = "ONTO_MINERU_SETTINGS"  # where 平台设置 saves the connection; tests point it at a temporary file
DEFAULT_URL = "https://mineru.net/api"
FOLDER = "mineru"
POLL_SECONDS = 3.0
MAX_WAIT = 20 * 60          # seconds to wait for one conversion
MAX_UNZIPPED = 400 * 1024 * 1024
MAX_FIGURES = 60
TEST_TRANSPORT: httpx.BaseTransport | None = None  # tests serve a fake MinerU through this

SHA = re.compile(r"[0-9a-f]{64}")
IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.(?:png|jpe?g|gif|webp)", re.I)
TERMINAL = {"completed", "partial", "failed", "canceled"}
CAPTION = re.compile(r"^\s*(?:\*\*)?\s*(fig(?:ure)?s?\.?|table|tab\.|chart|图|表)\s*[A-Za-z]?\s*\d", re.I)
ERRORS = {
    "invalid_api_key": "MinerU 拒绝了密钥：在「平台设置」里检查 Secret Key",
    "quota_exceeded": "MinerU 的解析额度用完了，稍后再试",
    "rate_limit_exceeded": "MinerU 请求太频繁，稍后再试",
    "file_encrypted": "PDF 有密码保护，MinerU 无法解析",
    "file_corrupted": "MinerU 读不了这个 PDF（文件可能损坏）",
    "upload_expired": "上传链接过期了，请重试",
    "parse_timeout": "MinerU 解析超时，请重试",
}


# ------------------------------------------------------------------ settings and the cache


def settings_file() -> Path:
    """Where 平台设置 keeps the MinerU connection on this computer, next to the model settings."""
    if os.environ.get(ENV_SETTINGS):
        return Path(os.environ[ENV_SETTINGS]).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "onto-research" / "mineru.json"


def _saved() -> dict[str, Any]:
    try:
        with settings_file().open("rb") as f:
            data = json.loads(f.read(64 * 1024))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def api_url() -> str:
    """The saved service address, else MINERU_API_URL, else mineru.net."""
    saved = _saved().get("url")
    return (saved if isinstance(saved, str) and saved else os.environ.get(ENV_URL) or DEFAULT_URL).strip().rstrip("/")


def api_key() -> str | None:
    """The saved secret key, else MINERU_API_KEY. It is sent as `Authorization: Bearer <secret>`."""
    saved = _saved().get("secret")
    return (saved if isinstance(saved, str) and saved else os.environ.get(ENV_KEY, "")).strip() or None


def _hint(secret: str | None) -> str | None:
    return f"••••{secret[-4:]}" if secret and len(secret) >= 12 else ("已设置" if secret else None)


def status() -> dict[str, Any]:
    """What a page needs to know before offering a conversion."""
    url = api_url()
    host = urlsplit(url).hostname or ""
    hosted = host == "mineru.net" or host.endswith(".mineru.net")
    key = bool(api_key())
    return {"url": url, "host": host, "hosted": hosted, "key": key, "ready": key or not hosted}


def settings() -> dict[str, Any]:
    """The connection as 平台设置 shows it. The secret itself never leaves this module, only its last 4 characters."""
    saved = _saved()
    secret = saved.get("secret") if isinstance(saved.get("secret"), str) else ""
    env_key = os.environ.get(ENV_KEY, "").strip()
    return {
        **status(),
        "default_url": DEFAULT_URL,
        "saved_url": saved.get("url") or "",
        "key_id": saved.get("key_id") or "",
        "secret_configured": bool(secret),
        "secret_hint": _hint(secret),
        "key_source": "settings" if secret else "environment" if env_key else None,
        "env_hint": _hint(env_key) if env_key and not secret else None,
        "file": str(settings_file()),
    }


def _clean_url(value: Any) -> str:
    url = (value or "").strip().rstrip("/") if isinstance(value, str) else ""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        ok = parts.scheme in ("http", "https") and parts.hostname and not parts.username and not parts.password \
            and not parts.query and not parts.fragment and len(url) <= 512
        parts.port  # noqa: B018  (raises on a bad port)
    except ValueError:
        ok = False
    if not ok:
        raise OntologyError("服务地址应为 http(s)://主机[:端口][/路径]，不带账号、参数或 #")
    return "" if url == DEFAULT_URL else url


def save_settings(data: dict[str, Any]) -> dict[str, Any]:
    """Save the connection. An empty secret keeps the saved one, unless the address changed: a secret is never
    sent to a server other than the one it was saved for. clear_secret removes it."""
    current = _saved()
    url = _clean_url(data.get("url"))
    key_id = data.get("key_id", current.get("key_id", ""))
    secret = data.get("secret") or ""
    for name, value, limit in (("Key ID", key_id, 200), ("Secret Key", secret, 2048)):
        if not isinstance(value, str) or len(value.strip()) > limit or not re.fullmatch(r"[\x21-\x7e]*", value.strip()):
            raise OntologyError(f"{name} 格式不对：应为不含空格的一串字符")
    secret = secret.strip()
    if not secret and not data.get("clear_secret") and url == (current.get("url") or ""):
        secret = current.get("secret") or ""
    out = {k: v for k, v in (("url", url), ("key_id", key_id.strip()), ("secret", secret)) if v}
    path = settings_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:  # mode 0600
        try:
            json.dump(out, handle, ensure_ascii=False)
            handle.close()
            os.replace(handle.name, path)
        finally:
            Path(handle.name).unlink(missing_ok=True)
    return settings()


def cache_root() -> Path:
    if os.environ.get(ENV_CACHE):
        return Path(os.environ[ENV_CACHE]).expanduser()
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "onto-research" / "papers"


def folder(sha: str) -> Path:
    if not SHA.fullmatch(sha or ""):
        raise OntologyError("bad file hash")
    return cache_root() / sha / FOLDER


def manifest(sha: str) -> dict[str, Any] | None:
    """The conversion of this PDF, if it is in the cache."""
    base = folder(sha)
    try:
        info = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) and (base / "markdown.md").is_file() else None


def markdown(sha: str) -> str:
    try:
        return (folder(sha) / "markdown.md").read_text(encoding="utf-8")
    except OSError:
        raise OntologyError("这个 PDF 还没有用 MinerU 转换过") from None


def asset(sha: str, name: str) -> Path:
    """A file of the conversion that pages may fetch: markdown.md or images/<name>. Nothing else, no other folder."""
    base = folder(sha)
    if name == "markdown.md":
        path = base / name
    else:
        parts = name.split("/")
        if len(parts) != 2 or parts[0] != "images" or not IMAGE.fullmatch(parts[1]):
            raise OntologyError("no such file")
        path = base / "images" / parts[1]
    if not path.is_file():
        raise OntologyError("no such file")
    return path


def remove(sha: str) -> None:
    shutil.rmtree(folder(sha), ignore_errors=True)


def check() -> str:
    """One cheap authenticated request, for the sources page's test button."""
    st = status()
    if not st["ready"]:
        raise OntologyError("需要 MinerU 密钥：在「平台设置」里填写 Secret Key")
    extra = {"transport": TEST_TRANSPORT} if TEST_TRANSPORT else {}
    auth = {"Authorization": f"Bearer {api_key()}"} if api_key() else {}
    try:
        with httpx.Client(timeout=20, **extra) as cli:
            usage = _json(cli.get(f"{st['url']}/v1/usage", headers=auth))
    except httpx.HTTPError as e:
        raise OntologyError(f"连不上 MinerU（{st['host']}）：{type(e).__name__}") from None
    level = str(usage.get("access_level") or "")
    if st["key"] and level == "anonymous":
        raise OntologyError(f"{st['host']} 把这次请求当作匿名访问：密钥没有生效，检查 Secret Key")
    current = usage.get("current") if isinstance(usage.get("current"), dict) else {}
    pages = current.get("pages_processed")
    parts = [f"{st['host']} 可用", "密钥有效" if st["key"] else "", f"账号级别 {level}" if level else "",
             f"本期已解析 {pages} 页" if isinstance(pages, int) else ""]
    return "，".join(p for p in parts if p)


# ------------------------------------------------------------------ figures and tables


def _captions(node: Any, out: dict[str, str]) -> None:
    """Image name -> caption, from structured_content.json: any block that names an image and has a caption."""
    if isinstance(node, list):
        for v in node:
            _captions(v, out)
        return
    if not isinstance(node, dict):
        return
    # The image is named in the block itself or one level down (e.g. {"image_source": {"path": "images/…"}}).
    values = [v for v in node.values() if isinstance(v, str)]
    values += [x for v in node.values() if isinstance(v, dict) for x in v.values() if isinstance(x, str)]
    names = [Path(v).name for v in values if IMAGE.fullmatch(Path(v).name or "")]
    if names:
        texts: list[str] = []

        def gather(v: Any) -> None:
            if isinstance(v, str):
                texts.append(v)
            elif isinstance(v, list):
                for x in v:
                    gather(x)
            elif isinstance(v, dict):
                for k in ("content", "text"):
                    if k in v:
                        gather(v[k])

        for k, v in node.items():
            if "caption" in k.lower():
                gather(v)
        caption = " ".join(" ".join(texts).split())
        for n in names:
            if caption:
                out.setdefault(n, caption)
    for v in node.values():
        if isinstance(v, (dict, list)):
            _captions(v, out)


def figures(sha: str) -> list[dict[str, Any]]:
    """Figures, tables and charts in reading order: [{id, name, kind, page, caption}]. `name` is the image under
    images/; page counts from 1. Captions come from structured_content.json, else from the line next to the image."""
    md = markdown(sha)
    captions: dict[str, str] = {}
    try:
        _captions(json.loads((folder(sha) / "structured_content.json").read_text(encoding="utf-8")), captions)
    except (OSError, ValueError):
        pass
    lines = md.splitlines()
    out, seen, used = [], set(), set()  # used: lines already taken as some figure's caption
    counts = {"figure": 0, "table": 0}
    for i, line in enumerate(lines):
        for m in re.finditer(r"!\[[^\]]*\]\(\s*<?(images/[^)\s>]+)>?\s*\)|<img[^>]+src=[\"'](images/[^\"']+)[\"']", line):
            name = Path(m.group(1) or m.group(2)).name
            if name in seen or not IMAGE.fullmatch(name):
                continue
            seen.add(name)
            kind = "table" if "_table" in name else "chart" if "_chart" in name else "image"
            if "_equation" in name or re.search(r"_(?:image|table|chart)_\d+_\d+\.", name):
                continue  # formula images and pictures inside another block are not figures of their own
            page = int(p.group(1)) + 1 if (p := re.match(r"page_(\d+)_", name)) else None
            caption = captions.get(name, "")
            if not caption:
                j = next((j for j in (i + 1, i - 1, i + 2, i - 2)
                          if 0 <= j < len(lines) and j not in used and CAPTION.match(lines[j])), None)
                if j is not None:
                    used.add(j)
                    caption = " ".join(lines[j].replace("**", "").split())
            group = "table" if kind == "table" else "figure"
            counts[group] += 1
            out.append({"id": f"{'T' if group == 'table' else 'F'}{counts[group]}", "name": name, "kind": kind,
                        "page": page, "caption": caption[:400]})
            if len(out) >= MAX_FIGURES:
                return out
    return out


# ------------------------------------------------------------------ talking to MinerU


def _error(payload: Any, status: int | None = None) -> OntologyError:
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(payload, dict) and (payload.get("msgCode") == "A0202" or "authenticate failed" in str(payload.get("msg", "")).lower()):
        err = {"code": "invalid_api_key"}
    if status == 401 and not err:
        err = {"code": "invalid_api_key"}
    if isinstance(err, dict):
        code = str(err.get("code") or "")
        message = ERRORS.get(code) or f"MinerU 出错：{err.get('message') or code or '未知错误'}"
        return OntologyError(message[:400])
    return OntologyError(f"MinerU 返回 HTTP {status}" if status else "MinerU 返回了无法识别的内容")


def _json(r: httpx.Response) -> dict[str, Any]:
    try:
        payload = r.json()
    except ValueError:
        payload = None
    if r.status_code >= 400 or not isinstance(payload, dict) or payload.get("msgCode") == "A0202":
        raise _error(payload, r.status_code if r.status_code >= 400 else None)
    return payload


def _same_origin(a: str, b: str) -> bool:
    pa, pb = urlsplit(a), urlsplit(b)
    return (pa.scheme, pa.hostname, pa.port) == (pb.scheme, pb.hostname, pb.port)


def _unpack(data: bytes, target: Path) -> int:
    """The parts of a result ZIP we use, written under target. Returns how many images there were."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise OntologyError("MinerU 的结果包无法打开") from None
    total, images, have_md = 0, 0, False
    (target / "images").mkdir(parents=True, exist_ok=True)
    for info in archive.infolist():
        if info.is_dir():
            continue
        parts = [p for p in info.filename.replace("\\", "/").split("/") if p]
        if not parts or ".." in parts:
            continue
        name = parts[-1]
        if len(parts) >= 2 and parts[-2] == "images" and IMAGE.fullmatch(name):
            dest = target / "images" / name
            images += 1
        elif name in ("markdown.md", "structured_content.json") or (name.endswith(".md") and not have_md):
            dest = target / ("markdown.md" if name.endswith(".md") else name)
        else:
            continue
        total += info.file_size
        if total > MAX_UNZIPPED:
            raise OntologyError("MinerU 的结果包太大")
        with archive.open(info) as src, dest.open("wb") as out:
            shutil.copyfileobj(src, out)
        have_md = have_md or dest.name == "markdown.md"
    if not have_md:
        raise OntologyError("MinerU 的结果包里没有 Markdown")
    return images


def convert(sha: str, filename: str, data: bytes, on_stage: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Convert one PDF and put the result in the cache. Blocking: run it in a worker thread."""
    st = status()
    if not st["ready"]:
        raise OntologyError("需要 MinerU 密钥：在 mineru.net 申请后，到「平台设置」里填写 Key ID 和 Secret Key")
    base = st["url"]
    auth = {"Authorization": f"Bearer {api_key()}"} if api_key() else {}
    name = re.sub(r"[/\\:\x00-\x1f]", "_", filename).strip() or "paper.pdf"
    if name in (".", ".."):
        name = "paper.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    request = {"files": [{"file_name": name[-200:], "bytes": len(data), "sha256sum": sha}],
               "output_formats": ["default"]}
    extra = {"transport": TEST_TRANSPORT} if TEST_TRANSPORT else {}
    try:
        with httpx.Client(timeout=httpx.Timeout(120, connect=20), **extra) as cli:
            task = _json(cli.post(f"{base}/v1/tasks", json=request, headers=auth))
            task_id = str(task.get("task_id") or "")
            if not task_id or not isinstance(task.get("files"), list) or not task["files"]:
                raise OntologyError("MinerU 没有返回任务编号")
            uploaded, started = False, time.monotonic()
            while True:
                item = task["files"][0] if isinstance(task.get("files"), list) and task["files"] else {}
                link = item.get("upload") if isinstance(item, dict) else None
                if not uploaded and item.get("status") == "waiting-file" and isinstance(link, dict) and link.get("url"):
                    url = urljoin(base + "/", str(link["url"]))
                    headers = {str(k): str(v) for k, v in (link.get("headers") or {}).items()}
                    if _same_origin(base, url):
                        headers.update(auth)
                    if on_stage:
                        on_stage("uploading")
                    r = cli.put(url, content=data, headers=headers)
                    if r.status_code >= 400:
                        raise _error(None, r.status_code)
                    uploaded = True
                    if on_stage:
                        on_stage("parsing")
                if task.get("status") in TERMINAL:
                    break
                if time.monotonic() - started > MAX_WAIT:
                    raise OntologyError("MinerU 解析超时（20 分钟），请稍后重试")
                time.sleep(POLL_SECONDS)
                task = _json(cli.get(f"{base}/v1/tasks/{quote(task_id, safe='')}", headers=auth))
            item = task["files"][0]
            if item.get("status") != "completed" or not item.get("result_url"):
                raise _error({"error": item.get("error") or task.get("error") or {"message": task.get("status")}})
            if on_stage:
                on_stage("downloading")
            url = urljoin(base + "/", str(item["result_url"]))
            r = cli.get(url, headers=auth if _same_origin(base, url) else {}, follow_redirects=True)
            if r.status_code >= 400:
                raise _error(None, r.status_code)
            zipped = r.content
    except httpx.HTTPError as e:
        raise OntologyError(f"连不上 MinerU（{urlsplit(base).hostname}）：{type(e).__name__}") from None
    root = cache_root()
    root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".mineru-", dir=root))
    try:
        images = _unpack(zipped, work)
        info = {"converter": "mineru", "api": urlsplit(base).hostname, "task_id": task_id,
                "tier": (item.get("parse") or {}).get("tier") if isinstance(item.get("parse"), dict) else None,
                "file_name": name, "bytes": len(data), "images": images,
                "converted_at": datetime.now(UTC).isoformat(timespec="seconds")}
        (work / "manifest.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        dest = folder(sha)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        os.replace(work, dest)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return info
