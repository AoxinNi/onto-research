from datetime import UTC, datetime, timedelta

from ontology import onchain, sources
from tests.conftest import node


def ev(ts, amount, tx, chain="ethereum", token="USDT"):
    return {"chain": chain, "token": token, "address": "0xabc", "tx": tx, "block": 1, "ts": ts, "amount": amount}


def test_hourly_buckets_sum_and_mark_the_open_hour():
    now = datetime(2026, 10, 29, 21, 30, tzinfo=UTC)
    since = now - timedelta(hours=2)
    pts = onchain.hourly([ev(now - timedelta(minutes=29), 1.2e9, "a"), ev(now - timedelta(minutes=10), 3e7, "b"),
                          ev(now - timedelta(hours=2), 5.0, "c")], since, now)
    assert [p.ts.hour for p in pts] == [19, 20, 21]
    assert [p.value for p in pts] == [5.0, 0.0, 1.23e9]
    assert pts[-1].payload["open"] and pts[-1].payload["events"][0]["tx"] == "a" and "open" not in pts[0].payload


def test_freeze_source_combines_chains(monkeypatch):
    now = datetime.now(UTC)
    monkeypatch.setattr(onchain, "eth_freezes", lambda tok, since, n: [ev(now, 100.0, f"e-{tok}", token=tok)])
    monkeypatch.setattr(onchain, "tron_freezes", lambda tok, since, n: [ev(now, 50.0, "t", chain="tron")])
    src = sources.source("stablecoin_freezes")
    p = src.clean({})
    assert p == {"token": "all", "chain": "all"} and src.label(p) == "稳定币冻结额"
    assert src.fetch(p, now - timedelta(minutes=5))[-1].value == 250.0
    assert src.label(src.clean({"token": "usdt", "chain": "tron"})) == "USDT冻结额（Tron）"


def test_eth_node_falls_back_to_the_next_public_node(monkeypatch):
    calls = []

    def post(url, body, provider, headers=None, retry=True):
        calls.append(url)
        if url == onchain.ETH_RPC_PUBLIC[0]:
            raise onchain.OntologyError("SSL EOF")
        return {"result": "0x10"}

    monkeypatch.setattr(onchain, "_post", post)
    monkeypatch.setattr(onchain, "_eth_ok", None)
    monkeypatch.delenv("ETH_RPC_URL", raising=False)
    assert onchain.eth("eth_blockNumber") == "0x10" and calls == onchain.ETH_RPC_PUBLIC
    calls.clear()
    onchain.eth("eth_blockNumber")
    assert calls == [onchain.ETH_RPC_PUBLIC[1]]  # the node that answered goes first


def test_big_freeze_becomes_a_pending_event_once(store, human, monkeypatch):
    usdt = node(store, "Asset", "泰达币", props={"symbol": "USDT", "asset_class": "stablecoin"})
    metric = node(store, "Metric", "稳定币冻结额")
    human.accept_changeset(store.submit_changeset("x")["id"])
    now = datetime.now(UTC)
    monkeypatch.setattr(onchain, "eth_freezes", lambda tok, since, n: [ev(now - timedelta(minutes=5), 1.2e9, "0xbig")]
                        if tok == "USDT" else [])
    monkeypatch.setattr(onchain, "tron_freezes", lambda tok, since, n: [ev(now, 10.0, "small", chain="tron")])
    made = store.apply_templates()
    assert [b["series_key"] for b in made if b["node_id"] == metric["id"]] == ["stablecoin_freezes:chain=all,token=all"]
    from ontology import collector
    s = store.series.get_series(made[0]["series_id"])
    collector.collect_series(store, s, now)

    titles = onchain.promote_freezes(store.conn, now)
    assert titles == ["Tether 在以太坊冻结 12.00 亿美元 USDT"]
    event = store.search_nodes("Tether 在以太坊冻结")[0]
    assert event["pending"] and event["props"]["category"] == "hack"
    assert any(l["to_id"] == usdt["id"] for l in store.get_node(event["id"])["links"])
    assert onchain.promote_freezes(store.conn, now) == []  # not proposed twice
