"""On-chain sources: stablecoin freezes (Ethereum, Tron) and Bitcoin wallet balances (mempool.space).

Freezes are the clearest early signal of an exchange hack or a sanctions action: Tether and Circle blacklist the
receiving addresses within minutes, long before anyone announces anything. Each freeze is read from the token
contract's own events through a public node, so nothing here needs a key.

- Ethereum: USDT AddedBlackList, USDC Blacklisted, via eth_getLogs on a public RPC (ETH_RPC_URL to use your
  own, e.g. Alchemy). Free public nodes only serve logs of roughly the last 30 hours, which is fine for polling.
- Tron: USDT AddedBlackList, via TronGrid's event API (TRON_API_KEY optional, raises the rate limit).
- The frozen amount is the address's current token balance: a blacklisted address cannot move its funds.

Freezes are bucketed per hour (value = USD frozen in that hour, payload = the events). A single freeze above
FREEZE_EVENT_USD becomes a pending Event node for the human to review, like the news desk's events.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg

from .errors import OntologyError
from .providers import throttle
from .sources import TIMEOUT, USER_AGENT, Obs, Source, _get, _utc_ms

log = logging.getLogger("ontology.onchain")

# Free public nodes, tried in turn (the one that answered last goes first). ETH_RPC_URL replaces them all.
ETH_RPC_PUBLIC = ["https://ethereum-rpc.publicnode.com", "https://rpc.mevblocker.io"]
ETH_BLOCK_SECONDS = 12
ETH_LOG_SPAN = 2000            # blocks per eth_getLogs call; public nodes refuse wide ranges
ETH_LOG_DEPTH = 9000           # how far back a public node still serves logs (~30 h)
TRONGRID = "https://api.trongrid.io"
FREEZE_EVENT_USD = float(os.environ.get("FREEZE_EVENT_USD", "100000000"))

# token -> chain -> (contract, event topic or name, decimals)
TOKENS: dict[str, dict[str, tuple[str, str, int]]] = {
    "USDT": {
        "ethereum": ("0xdAC17F958D2ee523a2206206994597C13D831ec7",
                     "0x42e160154868087d6bfdc0ca23d96a1c1cfa32f1b72ba9ba27b69b98a0d819dc", 6),  # AddedBlackList(address)
        "tron": ("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "AddedBlackList", 6),
    },
    "USDC": {
        "ethereum": ("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                     "0xffa4e6181777692565cf28528fc88fd1516ea86b56da075235fa575af6a4b855", 6),  # Blacklisted(address)
    },
}
TRON_USDT_HEX = "41a614f803b6fd780986a42c78ec9c7f77e6ded13c"  # TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t
BALANCE_OF = "0x70a08231"  # balanceOf(address)
RETRY_PAUSES = (0.6, 1.8)


_eth_ok: str | None = None


def eth_rpc_urls() -> list[str]:
    own = os.environ.get("ETH_RPC_URL", "").strip()
    return [own] if own else sorted(ETH_RPC_PUBLIC, key=lambda u: u != _eth_ok)


def _post(url: str, body: dict[str, Any], provider: str, headers: dict[str, str] | None = None,
          retry: bool = True) -> Any:
    for attempt, pause in enumerate((RETRY_PAUSES if retry else ()) + (None,)):
        throttle(provider)
        try:
            r = httpx.post(url, json=body, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT, **(headers or {})})
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            raise OntologyError(f"{url}: HTTP {e.response.status_code}") from None
        except (httpx.HTTPError, ValueError) as e:
            if pause is None:
                raise OntologyError(f"{url}: {e or type(e).__name__}（已重试 {attempt} 次）") from None
            time.sleep(pause)
    raise AssertionError("unreachable")


def eth(method: str, *params: Any) -> Any:
    """One JSON-RPC call; on a connection failure or a refusal, the next public node is tried."""
    global _eth_ok
    urls = eth_rpc_urls()
    last: OntologyError | None = None
    for url in urls:
        try:
            body = _post(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)}, "ethereum",
                         retry=url == urls[-1])
        except OntologyError as e:
            last = e
            continue
        if "error" in body:
            last = OntologyError(f"以太坊节点 {method}: {body['error'].get('message', body['error'])}")
            continue
        _eth_ok = url
        return body["result"]
    raise last or OntologyError("no Ethereum node configured")


def _tron_headers() -> dict[str, str]:
    key = os.environ.get("TRON_API_KEY", "").strip()
    return {"TRON-PRO-API-KEY": key} if key else {}


# ------------------------------------------------------------------ freezes


def eth_freezes(token: str, since: datetime, now: datetime) -> list[dict[str, Any]]:
    """Blacklistings of `token` on Ethereum since `since` (as far back as the node serves logs)."""
    contract, topic, decimals = TOKENS[token]["ethereum"]
    head = int(eth("eth_blockNumber"), 16)
    head_ts = _block_time(head)
    back = int((head_ts - since).total_seconds() // ETH_BLOCK_SECONDS) + 1
    start = max(head - back, head - ETH_LOG_DEPTH, 0)
    out = []
    for lo in range(start, head + 1, ETH_LOG_SPAN):
        hi = min(lo + ETH_LOG_SPAN - 1, head)
        for lg in eth("eth_getLogs", {"address": contract, "topics": [topic], "fromBlock": hex(lo), "toBlock": hex(hi)}):
            # The address is the indexed topic for USDC and the data word for USDT.
            word = lg["topics"][1] if len(lg["topics"]) > 1 else lg["data"][:66]
            addr = "0x" + word[-40:]
            bn = int(lg["blockNumber"], 16)
            out.append({"chain": "ethereum", "token": token, "address": addr, "tx": lg["transactionHash"],
                        "block": bn, "ts": _block_time(bn), "amount": _eth_balance(contract, addr) / 10 ** decimals})
    return [e for e in out if e["ts"] >= since]


def _block_time(bn: int) -> datetime:
    return datetime.fromtimestamp(int(eth("eth_getBlockByNumber", hex(bn), False)["timestamp"], 16), tz=UTC)


def _eth_balance(contract: str, addr: str) -> int:
    data = BALANCE_OF + addr[2:].lower().rjust(64, "0")
    return int(eth("eth_call", {"to": contract, "data": data}, "latest") or "0x0", 16)


def tron_freezes(token: str, since: datetime, now: datetime) -> list[dict[str, Any]]:
    contract, event, decimals = TOKENS[token]["tron"]
    out, fingerprint = [], None
    for _ in range(20):  # 200 events a page; a few pages cover any window we poll
        params = {"event_name": event, "limit": 200, "order_by": "block_timestamp,asc",
                  "min_block_timestamp": int(since.timestamp() * 1000), **({"fingerprint": fingerprint} if fingerprint else {})}
        body = _get(f"{TRONGRID}/v1/contracts/{contract}/events", provider="trongrid", headers=_tron_headers(), **params).json()
        for e in body.get("data", []):
            addr = e["result"].get("_user") or e["result"].get("0")
            out.append({"chain": "tron", "token": token, "address": addr, "tx": e["transaction_id"],
                        "block": e["block_number"], "ts": _utc_ms(e["block_timestamp"]),
                        "amount": _tron_balance(addr) / 10 ** decimals})
        fingerprint = (body.get("meta") or {}).get("fingerprint")
        if not fingerprint:
            break
    return out


def _tron_balance(addr: str) -> int:
    body = _post(f"{TRONGRID}/wallet/triggerconstantcontract", {
        "owner_address": "41" + "0" * 40, "contract_address": TRON_USDT_HEX, "function_selector": "balanceOf(address)",
        "parameter": addr.lower().removeprefix("0x").rjust(64, "0"), "visible": False}, "trongrid", _tron_headers())
    result = body.get("constant_result") or ["0"]
    return int(result[0] or "0", 16)


def hourly(events: list[dict[str, Any]], since: datetime, now: datetime) -> list[Obs]:
    """One point per hour from `since` to now: USD frozen in that hour, with the events. The current hour is open."""
    start = since.replace(minute=0, second=0, microsecond=0)
    buckets: dict[datetime, list[dict[str, Any]]] = {}
    t = start
    while t <= now:
        buckets[t] = []
        t += timedelta(hours=1)
    for e in events:
        h = e["ts"].replace(minute=0, second=0, microsecond=0)
        if h in buckets:
            buckets[h].append(e)
    out = []
    for h, evs in buckets.items():
        evs.sort(key=lambda e: -e["amount"])
        payload: dict[str, Any] = {"count": len(evs), "events": [
            {**{k: v for k, v in e.items() if k != "ts"}, "ts": e["ts"].isoformat(), "amount": round(e["amount"], 2)}
            for e in evs[:20]]}
        if h + timedelta(hours=1) > now:
            payload["open"] = True
        out.append(Obs(h, round(sum(e["amount"] for e in evs), 2), payload))
    return out


class StablecoinFreezes(Source):
    """USD of stablecoins frozen per hour. chain/token "all" sums every supported pair."""

    def clean(self, params):
        p = super().clean(params)
        p["token"], p["chain"] = p["token"].upper().replace("ALL", "all"), p["chain"].lower()
        pairs = self.pairs(p)
        if not pairs:
            raise OntologyError("supported: token USDT (ethereum, tron), USDC (ethereum), or all")
        return p

    @staticmethod
    def pairs(p) -> list[tuple[str, str]]:
        return [(tok, ch) for tok, chains in TOKENS.items() for ch in chains
                if p["token"] in ("all", tok) and p["chain"] in ("all", ch)]

    def label(self, p):
        tok = "稳定币" if p["token"] == "all" else p["token"]
        ch = {"all": "", "ethereum": "（以太坊）", "tron": "（Tron）"}[p["chain"]]
        return f"{tok}冻结额{ch}"

    def frequency(self, p):
        return "1h"

    def fetch(self, p, since):
        now = datetime.now(UTC)
        events = []
        for tok, ch in self.pairs(p):
            events += (eth_freezes if ch == "ethereum" else tron_freezes)(tok, since, now)
        return hourly(events, since, now)


# ------------------------------------------------------------------ bitcoin wallets


class BtcWalletBalance(Source):
    """Confirmed balance (BTC) of one or more Bitcoin addresses, summed. A point per poll; no history."""

    def clean(self, params):
        p = {k: str(v).strip() for k, v in (params or {}).items() if v is not None and str(v).strip()}
        if set(p) - set(self.params):
            raise OntologyError(f"source {self.name} takes {list(self.params)}")
        if not p.get("address") or not addresses(p):
            raise OntologyError("btc_wallet_balance needs address")
        return p

    def label(self, p):
        n = len(addresses(p))
        return p.get("name") or (f"BTC 地址 {addresses(p)[0][:10]}… 余额" if n == 1 else f"{n} 个 BTC 地址余额")

    def frequency(self, p):
        return "5m"

    def fetch(self, p, since):
        total = 0
        for a in addresses(p):
            s = _get(f"https://mempool.space/api/address/{a}", provider="mempool").json()["chain_stats"]
            total += s["funded_txo_sum"] - s["spent_txo_sum"]
        now = datetime.now(UTC)
        return [Obs(now.replace(second=0, microsecond=0), total / 1e8, {"addresses": len(addresses(p))})]


def addresses(p) -> list[str]:
    return [a.strip() for a in p["address"].replace("\n", ",").split(",") if a.strip()]


SOURCES = [
    StablecoinFreezes("stablecoin_freezes", "稳定币冻结额（每小时）",
                      {"token": "USDT / USDC / all", "chain": "ethereum / tron / all"}, "USD",
                      timedelta(minutes=2), timedelta(hours=30), {"token": "all", "chain": "all"}, provider="ethereum"),
    BtcWalletBalance("btc_wallet_balance", "BTC 地址余额（mempool.space）",
                     {"address": "BTC 地址，多个用逗号分隔，余额相加", "name": "显示名称（可选）"}, "BTC",
                     timedelta(minutes=5), timedelta(0), provider="mempool"),
]


# ------------------------------------------------------------------ promote


def promote_freezes(conn: psycopg.Connection, now: datetime) -> list[str]:
    """Freezes above FREEZE_EVENT_USD in the last day become Event nodes, in one changeset for review."""
    from .store import OntologyStore  # store imports sources, which import this module

    rows = conn.execute(
        """SELECT o.payload FROM observations o JOIN series s ON s.id = o.series_id
           WHERE s.source = 'stablecoin_freezes' AND o.ts > %s AND o.value >= %s""",
        (now - timedelta(days=1), FREEZE_EVENT_USD),
    ).fetchall()
    seen = {r["tx"] for r in conn.execute("SELECT tx FROM onchain_alerts")}
    big = {}
    for r in rows:
        for e in (r["payload"] or {}).get("events", []):
            if e["amount"] >= FREEZE_EVENT_USD and e["tx"] not in seen:
                big[e["tx"]] = e
    if not big:
        return []
    store = OntologyStore(conn, actor="onchain-agent")
    targets = conn.execute(
        """SELECT id, type, name, props FROM objects WHERE status = 'active' AND type IN ('Asset', 'Narrative')""").fetchall()
    made = []
    for e in sorted(big.values(), key=lambda e: e["ts"]):
        chain = {"ethereum": "以太坊", "tron": "Tron"}[e["chain"]]
        issuer = "Tether" if e["token"] == "USDT" else "Circle"
        title = f"{issuer} 在{chain}冻结 {e['amount'] / 1e8:.2f} 亿美元 {e['token']}"
        ref = (f"https://etherscan.io/tx/{e['tx']}" if e["chain"] == "ethereum" else f"https://tronscan.org/#/transaction/{e['tx']}")
        prov = {"kind": "data_source", "ref": ref, "note": f"地址 {e['address']}"}
        try:
            node = store.upsert_node("Event", title, prov, f"链上冻结事件，金额超过 {FREEZE_EVENT_USD / 1e8:g} 亿美元，"
                                     "常见于交易所被盗或制裁执法", key=f"freeze:{e['tx']}",
                                     props={"category": "hack", "status": "happened", "occurred_at": e["ts"],
                                            "severity": 4 if e["amount"] >= 5 * FREEZE_EVENT_USD else 3})["node"]
        except OntologyError as err:
            log.info("not promoting freeze %s: %s", e["tx"], err)
            continue
        for t in targets:
            names = f"{t['name']} {(t['props'] or {}).get('symbol', '')}".upper()
            if e["token"] in names or (t["type"] == "Narrative" and "稳定币" in t["name"]):
                try:
                    store.create_link("affects", node["id"], t["id"], prov, f"{e['token']} 大额冻结", confidence=0.6)
                except OntologyError as err:
                    log.info("skipping link to %s: %s", t["name"], err)
        conn.execute("INSERT INTO onchain_alerts (tx, node_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (e["tx"], node["id"]))
        made.append(title)
    if made:
        store.submit_changeset(f"链上冻结 {len(made)} 起：" + "、".join(made[:3]) + ("…" if len(made) > 3 else ""))
    return made
