"""Model routes. Cloud credentials are account-scoped and encrypted at rest."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import os
import re
import secrets
import socket
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import anthropic
import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ontology.db import connect

DEFAULT_URL = "https://api.anthropic.com"
DEFAULT_MODEL = "claude-opus-5-5"
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,119}")
DNS_NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class SettingsError(Exception):
    """Safe to show in the settings UI."""


@dataclass(frozen=True)
class ModelRoute:
    mode: str
    base_url: str
    model: str
    auth_kind: str
    key: str = field(repr=False)

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"} if self.auth_kind == "bearer" else {"x-api-key": self.key}

    def client(self) -> anthropic.AsyncAnthropic:
        # Pin the requested wire header; the SDK may map api_key to Authorization
        # or import deployment-wide custom headers.
        credential = {"auth_token": self.key} if self.auth_kind == "bearer" else {"api_key": self.key}

        async def prepare(request) -> None:
            if self.mode != "environment":
                for header in os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines():
                    name, sep, _ = header.partition(":")
                    if sep:
                        request.headers.pop(name.strip(), None)
                request.headers["Host"] = urlsplit(self.base_url).netloc
                request.headers["anthropic-version"] = "2023-06-01"
                if request.method == "POST":
                    request.headers["Content-Type"] = "application/json"
                    request.headers["Content-Length"] = str(len(request.content))
            request.headers.pop("Authorization", None)
            request.headers.pop("x-api-key", None)
            request.headers.update(self.headers())

        return anthropic.AsyncAnthropic(
            base_url=self.base_url,
            http_client=anthropic.DefaultAsyncHttpxClient(
                follow_redirects=False, trust_env=False, timeout=120, event_hooks={"request": [prepare]},
            ),
            **credential,
        )


def allowed_hosts() -> set[str]:
    configured = os.environ.get("WORKBENCH_MODEL_ALLOWED_HOSTS", "api.anthropic.com")
    return {host.strip().lower() for host in configured.split(",") if host.strip()}


def normalize_url(value: object, cloud: bool) -> str:
    if not isinstance(value, str) or len(value) > 512 or re.search(r"[\s\\]", value.strip()):
        raise SettingsError("请输入有效的模型 Base URL。")
    try:
        parts = urlsplit(value.strip())
        host, port = parts.hostname, parts.port
        if (parts.scheme not in {"http", "https"} or not host or parts.username is not None
                or parts.query or parts.fragment):
            raise ValueError
    except ValueError:
        raise SettingsError("Base URL 需要 HTTP(S)，且不能包含用户名、查询参数或片段。") from None
    if cloud:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise SettingsError("云端 Base URL 需要使用已批准的域名。")
        if (parts.scheme != "https" or port not in (None, 443) or not DNS_NAME.fullmatch(host)
                or host in LOCAL_HOSTS or host not in allowed_hosts()):
            raise SettingsError("云端只允许通过 HTTPS 访问部署时批准的模型域名。")
    if parts.path.rstrip("/").endswith("/v1/messages"):
        raise SettingsError("请填写 Base URL，不包含 /v1/messages。")
    netloc = f"[{host}]" if ":" in host else host
    if port and (parts.scheme, port) not in {("http", 80), ("https", 443)}:
        netloc += f":{port}"
    path = parts.path.rstrip("/").removesuffix("/v1")
    return f"{parts.scheme}://{netloc}{path}"


def _read_json(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            data = json.loads(handle.read(128 * 1024))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _local_file() -> Path:
    default = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return Path(os.environ.get("WORKBENCH_LOCAL_MODEL_SETTINGS") or default / "onto-research" / "model.json")


def _write_local(data: dict) -> None:
    path = _local_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        try:
            json.dump(data, handle, ensure_ascii=False)
            handle.close()
            os.replace(handle.name, path)
        finally:
            Path(handle.name).unlink(missing_ok=True)


def _local_routes() -> dict[str, tuple[str, int, str, str]]:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    magpie = _read_json(config / "magpie" / "settings.json").get("port")
    magpie = magpie if type(magpie) is int and 1 <= magpie <= 65535 else 3425
    settings = _read_json(Path.home() / ".claude" / "settings.json").get("env")
    settings = settings if isinstance(settings, dict) else {}
    cc_port = 15721
    if "PROXY_MANAGED" in (settings.get("ANTHROPIC_AUTH_TOKEN"), settings.get("ANTHROPIC_API_KEY")):
        try:
            parts = urlsplit(settings.get("ANTHROPIC_BASE_URL", ""))
            if parts.scheme == "http" and parts.hostname in LOCAL_HOSTS and parts.port:
                cc_port = parts.port
        except (ValueError, TypeError, AttributeError):
            pass
    return {"magpie": ("Magpie", magpie, "api_key", "magpie-onto-research"),
            "ccswitch": ("CC Switch", cc_port, "bearer", "PROXY_MANAGED")}


def local_provider(mode: str, model: str, key: str = "") -> ModelRoute:
    routes = _local_routes()
    if mode not in routes:
        raise SettingsError("不支持此本机路由。")
    _, port, auth_kind, placeholder = routes[mode]
    return ModelRoute(mode, f"http://127.0.0.1:{port}", model, auth_kind, key or placeholder)


def _local_status() -> list[dict]:
    routes = []
    for mode, (label, port, _, _) in _local_routes().items():
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                reachable = True
        except OSError:
            reachable = False
        routes.append({"id": mode, "label": label, "base_url": f"http://127.0.0.1:{port}", "reachable": reachable})
    return routes


def _cipher() -> AESGCM:
    try:
        key = os.environ.get("WORKBENCH_MODEL_SETTINGS_KEY", "")
        raw = base64.b64decode(key, altchars=b"-_", validate=True)
        if len(raw) != 32:
            raise ValueError
    except (ValueError, binascii.Error):
        raise SettingsError("云端尚未配置模型密钥加密。") from None
    return AESGCM(raw)


def _aad(user_id: str, base_url: str, auth_kind: str) -> bytes:
    return f"{user_id}\0{base_url}\0{auth_kind}".encode()


def _stored(user_id: str) -> dict:
    if not user_id:
        return _read_json(_local_file())
    with connect() as conn:
        row = conn.execute(
            "SELECT base_url, model, auth_kind, encrypted_key FROM onto_research.web_model_settings WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    return row | {"mode": "custom"} if row else {}


def environment_route() -> ModelRoute:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    bearer = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    return ModelRoute("environment", os.environ.get("ANTHROPIC_BASE_URL") or DEFAULT_URL,
                      os.environ.get("WORKBENCH_MODEL") or DEFAULT_MODEL,
                      "bearer" if bearer and not key else "api_key", key or bearer)


def resolve(user_id: str) -> ModelRoute:
    saved = _stored(user_id)
    mode = saved.get("mode", "environment")
    if mode == "environment":
        return environment_route()
    if mode in {"magpie", "ccswitch"} and not user_id:
        return local_provider(mode, saved["model"], saved.get("key", ""))
    if mode != "custom":
        raise SettingsError("无法读取模型配置，请重新保存。")
    base = normalize_url(saved.get("base_url"), bool(user_id))
    auth_kind = saved["auth_kind"]
    key = saved.get("key", "")
    if user_id:
        encrypted = saved["encrypted_key"]
        try:
            key = _cipher().decrypt(encrypted[:12], encrypted[12:], _aad(user_id, base, auth_kind)).decode()
        except (InvalidTag, ValueError):
            raise SettingsError("无法解密模型 API Key，请重新保存。") from None
    return ModelRoute(mode, base, saved["model"], auth_kind, key)


def public_settings(user_id: str) -> dict:
    saved = _stored(user_id)
    mode = saved.get("mode", "environment")
    default = environment_route()
    return {
        "mode": mode, "model": saved.get("model", default.model),
        "base_url": saved.get("base_url", DEFAULT_URL),
        "auth_kind": saved.get("auth_kind", "api_key"),
        "key_configured": bool(saved.get("encrypted_key") or saved.get("key")),
        "default_configured": bool(default.key),
        "cloud": bool(user_id), "allowed_hosts": sorted(allowed_hosts()) if user_id else [],
        "local_routes": [] if user_id else _local_status(),
    }


def save(user_id: str, data: dict) -> dict:
    mode = data.get("mode")
    modes = ("environment", "custom") if user_id else ("environment", "custom", "magpie", "ccswitch")
    if mode not in modes:
        raise SettingsError("当前环境不支持此模型连接方式。")
    if mode == "environment":
        if user_id:
            with connect() as conn:
                conn.execute("DELETE FROM onto_research.web_model_settings WHERE user_id = %s", (user_id,))
        else:
            _write_local({"mode": "environment"})
        return public_settings(user_id)

    model = data.get("model")
    if not isinstance(model, str) or not MODEL_ID.fullmatch(model):
        raise SettingsError("请输入有效的模型 ID。")
    key = data.get("api_key", "")
    if not isinstance(key, str) or len(key) > 2048 or not re.fullmatch(r"[!-~]*", key):
        raise SettingsError("API Key 格式无效。")
    current = _stored(user_id)
    if mode != "custom":
        if not key and current.get("mode") == mode:
            key = current.get("key", "")
        _write_local({"mode": mode, "model": model, "key": key})
        return public_settings(user_id)

    base = normalize_url(data.get("base_url"), bool(user_id))
    auth_kind = data.get("auth_kind", "api_key")
    if auth_kind not in ("api_key", "bearer"):
        raise SettingsError("请选择有效的鉴权方式。")
    unchanged = (current.get("mode"), current.get("base_url"), current.get("auth_kind")) == (mode, base, auth_kind)
    if user_id:
        cipher = _cipher()
        encrypted = current.get("encrypted_key") if unchanged else None
        if key:
            nonce = secrets.token_bytes(12)
            encrypted = nonce + cipher.encrypt(nonce, key.encode(), _aad(user_id, base, auth_kind))
        if not encrypted:
            raise SettingsError("保存新连接时请输入 API Key。")
        with connect() as conn:
            conn.execute(
                """INSERT INTO onto_research.web_model_settings (user_id, base_url, model, auth_kind, encrypted_key)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (user_id) DO UPDATE SET base_url = excluded.base_url, model = excluded.model,
                       auth_kind = excluded.auth_kind, encrypted_key = excluded.encrypted_key""",
                (user_id, base, model, auth_kind, encrypted),
            )
    else:
        key = key or (current.get("key") if unchanged else "")
        if not key:
            raise SettingsError("保存新连接时请输入 API Key。")
        _write_local({"mode": mode, "base_url": base, "model": model, "auth_kind": auth_kind, "key": key})
    return public_settings(user_id)


def catalog(route: ModelRoute, *, include_url: bool) -> dict:
    via = {"magpie": "Magpie", "ccswitch": "CC Switch"}.get(route.mode)
    output = {"default": route.model, "base_url": route.base_url if include_url else None,
              "via": via, "models": [], "error": None}
    if route.key:
        try:
            with httpx.Client(timeout=6, follow_redirects=False, trust_env=False) as client:
                response = client.get(route.base_url.rstrip("/") + "/v1/models",
                                      headers=route.headers() | {"anthropic-version": "2023-06-01"},
                                      params={"limit": 100})
                response.raise_for_status()
                for entry in response.json()["data"][:100]:
                    mid = entry.get("id") if isinstance(entry, dict) else None
                    if isinstance(mid, str) and MODEL_ID.fullmatch(mid):
                        label = entry.get("display_name")
                        output["models"].append({"id": mid, "name": label[:120] if isinstance(label, str) else mid,
                                                 "provider": str(entry.get("owned_by") or "")[:80],
                                                 "context": entry.get("context_window")
                                                 if type(entry.get("context_window")) is int else None})
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            output["error"] = "模型目录不可用，可在平台设置中手动指定模型 ID。"
    else:
        output["error"] = "请在平台设置中配置模型连接。"
    if not any(entry["id"] == route.model for entry in output["models"]):
        output["models"].insert(0, {"id": route.model, "name": route.model, "provider": "", "context": None})
    return output
