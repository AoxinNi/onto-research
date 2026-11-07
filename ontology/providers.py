"""Data providers: who the data comes from, what each needs to work, and how gently to call it.

A provider is an organisation or service (Binance, FRED, 同花顺…); its sources (ontology.sources) are the
concrete series it can deliver. Adding a provider means adding an entry here and a connector module;
the 数据源 page, health checks and rate limiting pick it up from this registry.

Credentials never live in the database or in code: a provider names the environment variable that holds
its key. The key goes into .env (by hand, or from the 数据源 page through `save_key`); the page only ever
shows whether it is set and its last four characters.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import OntologyError

ENV_FILE = Path(os.environ.get("ENV_FILE") or Path(__file__).resolve().parent.parent / ".env")

CATEGORIES = {"crypto": "加密市场", "onchain": "链上数据", "macro": "宏观经济", "a-share": "A 股", "news": "新闻资讯", "social": "社交媒体",
              "internal": "内部计算"}


@dataclass
class Provider:
    id: str
    name: str
    category: str
    homepage: str
    docs: str = ""
    auth_env: str | None = None   # environment variable holding the API key; None: no key needed
    min_interval: float = 0.0     # seconds between requests, enforced per process
    note: str = ""

    def configured(self) -> bool:
        return not self.auth_env or bool(os.environ.get(self.auth_env, "").strip())

    def key(self) -> str | None:
        return os.environ.get(self.auth_env, "").strip() or None if self.auth_env else None

    def describe(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "category": self.category, "category_name": CATEGORIES.get(self.category),
                "homepage": self.homepage, "docs": self.docs, "auth_env": self.auth_env, "configured": self.configured(),
                "min_interval": self.min_interval, "note": self.note, "key_hint": self.key_hint()}

    def key_hint(self) -> str | None:
        k = self.key()
        return f"••••{k[-4:]}" if k and len(k) >= 8 else ("已设置" if k else None)


PROVIDERS: dict[str, Provider] = {p.id: p for p in [
    Provider("binance", "Binance", "crypto", "https://www.binance.com", "https://developers.binance.com/docs",
             min_interval=0.1, note="现货 K 线、永续资金费率、未平仓量；公开接口，无需密钥"),
    Provider("fred", "FRED（圣路易斯联储）", "macro", "https://fred.stlouisfed.org", "https://fred.stlouisfed.org/docs/api/",
             min_interval=0.5, note="利率、美元指数、M2、CPI 等宏观序列；用公开 CSV，无需密钥"),
    Provider("mempool", "mempool.space", "crypto", "https://mempool.space", "https://mempool.space/docs/api",
             min_interval=0.5, note="比特币全网算力、BTC 地址余额"),
    Provider("defillama", "DefiLlama", "crypto", "https://defillama.com", "https://defillama.com/docs/api",
             min_interval=0.5, note="稳定币流通量"),
    Provider("ths", "同花顺金融数据", "a-share", "https://fuyao.aicubes.cn", "https://fuyao.aicubes.cn/docs/api-reference/overview/",
             auth_env="THS_API_KEY", min_interval=0.25,
             note="A 股与指数日 K、估值、财务指标、标的检索、指数成分股；在官网「API Key 管理」签发密钥"),
    Provider("ethereum", "以太坊公共节点", "onchain", "https://ethereum-rpc.publicnode.com", "https://ethereum.org/developers/docs/apis/json-rpc/",
             min_interval=0.15, note="USDT / USDC 冻结事件；默认 publicnode，只保留约 30 小时日志。设 ETH_RPC_URL 可换成自己的节点（如 Alchemy）"),
    Provider("trongrid", "TronGrid", "onchain", "https://www.trongrid.io", "https://developers.tron.network/reference/events",
             min_interval=0.35, note="Tron 上的 USDT 冻结事件；免密钥可用，设 TRON_API_KEY 提高限额"),
    Provider("rss", "新闻 RSS", "news", "", note="CoinDesk、Cointelegraph、Decrypt、The Block、Bitcoin Magazine、PANews"),
    Provider("grok", "X（经 Grok）", "social", "https://x.ai", note="search_x 工具经 magpie 调用 Grok 的 x_search；需要 magpie 里配置 Grok"),
    Provider("manual", "手动导入", "internal", "", note="python -m ontology.cli import-csv"),
    Provider("internal", "内部计算", "internal", "", note="新闻热度、新闻情绪等派生序列"),
]}

_locks: dict[str, threading.Lock] = {}
_last: dict[str, float] = {}


def throttle(provider_id: str | None) -> None:
    """Wait until this provider's minimum interval since the previous request has passed (thread-safe)."""
    p = PROVIDERS.get(provider_id or "")
    if not p or p.min_interval <= 0:
        return
    lock = _locks.setdefault(p.id, threading.Lock())
    with lock:
        wait = _last.get(p.id, 0) + p.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[p.id] = time.monotonic()


def save_key(provider_id: str, value: str, env_file: Path | None = None) -> Provider:
    """Set (or, with an empty value, remove) a provider's key in .env and in this process, effective at once."""
    p = PROVIDERS.get(provider_id)
    if not p or not p.auth_env:
        raise OntologyError(f"{provider_id} 不需要密钥")
    value = value.strip()
    if len(value) > 512 or not re.fullmatch(r"[\x21-\x7e]*", value):
        raise OntologyError("密钥格式不对：应为不含空格的一串字符")
    path = env_file or ENV_FILE
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pattern = re.compile(rf"^\s*(export\s+)?{re.escape(p.auth_env)}\s*=")
    kept = [ln for ln in lines if not pattern.match(ln)]
    if value:
        kept.append(f"{p.auth_env}={value}")
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    path.chmod(0o600)
    if value:
        os.environ[p.auth_env] = value
    else:
        os.environ.pop(p.auth_env, None)
    return p


def load_env_file(path: Path | None = None) -> None:
    """Read KEY=VALUE lines from .env into the environment, without overriding what is already set.
    The MCP server calls this, so a client's config needs no secrets: they stay in the project's .env."""
    path = path or ENV_FILE
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if not m or line.lstrip().startswith("#"):
            continue
        key, value = m.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def provider(provider_id: str) -> Provider:
    return PROVIDERS[provider_id]
