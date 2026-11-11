from datetime import UTC, datetime, timedelta

import pytest

from ontology import collector, sources
from ontology.errors import OntologyError
from ontology.sources import Obs, Source
from tests.conftest import link, node

BTC = {"symbol": "BTC", "asset_class": "spot"}


class FakeSource(Source):
    """Hourly points ending at the last full hour; records what it was asked for."""

    def __init__(self):
        super().__init__("fake", "测试源", {"symbol": "代码"}, "USD", timedelta(minutes=5), timedelta(days=2))
        self.calls = []

    def label(self, p):
        return f"Fake {p['symbol']}"

    def frequency(self, p):
        return "1h"

    def fetch(self, p, since):
        self.calls.append(since)
        end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        hours = int((end - since).total_seconds() // 3600)
        return [Obs(end - timedelta(hours=h), 100.0 + (hours - h)) for h in range(hours, -1, -1)]


@pytest.fixture
def fake(monkeypatch):
    src = FakeSource()
    monkeypatch.setitem(sources.SOURCES, "fake", src)
    return src


def test_proposed_binding_is_reviewed_and_only_collected_once_accepted(store, human, fake):
    btc = node(store, "Asset", "比特币", props=BTC)
    store.submit_changeset("node")
    human.accept_changeset(store.list_changesets()[0]["id"])

    b = store.propose_binding(btc["id"], "fake", {"symbol": "BTC"}, "price", "价格")["binding"]
    assert b["pending"] and b["label"] == "Fake BTC"
    assert store.series.collectable() == []

    cs = store.submit_changeset("bind price")
    human.accept_changeset(cs["id"])
    assert [s["id"] for s in store.series.collectable()] == [b["series_id"]]

    changed = collector.run_once(store.conn)
    assert b["series_id"] in changed
    data = store.graph()["data"][btc["id"]][0]
    assert data["role"] == "price" and not data["pending"]
    assert data["latest"]["value"] in (147.0, 148.0)  # ~2 days of hourly points, counting up from 100
    assert data["change"]["window"] == "24h" and data["change"]["abs"] == 24.0
    assert 0 < len(data["spark"]) <= 60 and data["stale"] is False

    # Not due again yet; when it is, it re-reads only the last few points.
    assert collector.run_once(store.conn) == []
    store.conn.execute("UPDATE series SET last_fetch_at = now() - interval '1 hour'")
    collector.run_once(store.conn)
    assert datetime.now(UTC) - fake.calls[-1] < timedelta(hours=4)


def test_rejecting_a_binding_removes_it(store, human, fake):
    btc = node(store, "Asset", "比特币", props=BTC)
    b = store.propose_binding(btc["id"], "fake", {"symbol": "BTC"}, "price", "价格")["binding"]
    cs = store.submit_changeset("node + binding")
    human.reject_changeset(cs["id"])
    assert store.conn.execute("SELECT count(*) AS n FROM bindings").fetchone()["n"] == 0
    assert store.conn.execute("SELECT count(*) AS n FROM objects").fetchone()["n"] == 0
    with pytest.raises(OntologyError, match="no binding"):
        store.archive(b["id"], "x")


def test_binding_validation(store, fake):
    btc = node(store, "Asset", "比特币", props=BTC)
    with pytest.raises(OntologyError, match="unknown source"):
        store.propose_binding(btc["id"], "nope", {}, "price", "x")
    with pytest.raises(OntologyError, match="needs params"):
        store.propose_binding(btc["id"], "fake", {}, "price", "x")
    with pytest.raises(OntologyError, match="role"):
        store.propose_binding(btc["id"], "fake", {"symbol": "BTC"}, "Price!", "x")
    first = store.propose_binding(btc["id"], "fake", {"symbol": "BTC"}, "price", "x")
    again = store.propose_binding(btc["id"], "fake", {"symbol": "BTC"}, "price", "x")
    assert first["created"] and not again["changed"]
    swap = store.propose_binding(btc["id"], "fake", {"symbol": "XBT"}, "price", "换源")
    assert swap["changed"] and swap["binding"]["series_key"] == "fake:symbol=XBT"


def test_templates_bind_settled_nodes_and_respect_removal(store, human):
    dxy = node(store, "MacroIndicator", "美元指数", aliases=["DXY"])
    rate = node(store, "MacroIndicator", "联邦基金目标利率", aliases=["Federal Funds Target Rate"])
    node(store, "Metric", "资金费率", aliases=["Funding Rate"])  # no template for it
    assert store.apply_templates() == []  # still pending review
    human.accept_changeset(store.submit_changeset("nodes")["id"])

    made = {b["series_key"] for b in store.apply_templates()}
    assert made == {"fred:series=DFEDTARU", "fred:series=DTWEXBGS"}
    assert store.apply_templates() == []
    rate_binding = store.list_bindings(rate["id"])[0]
    assert rate_binding["unit"] == "%" and rate_binding["provenance"]["ref"] == "template:fed-funds-target"

    human.archive(rate_binding["binding_id"], "不需要")
    assert store.apply_templates() == []
    assert not store.list_bindings(rate["id"]) and store.list_bindings(dxy["id"])


def test_archiving_or_merging_a_node_carries_its_bindings(store, human, fake):
    a = node(store, "Asset", "比特币", props=BTC)
    b = node(store, "Asset", "Bitcoin 2", props=BTC, force_new=True)
    c = node(store, "Asset", "以太坊", props={"symbol": "ETH", "asset_class": "spot"})
    link(store, "tracks", b, a)
    store.propose_binding(b["id"], "fake", {"symbol": "BTC"}, "price", "x")
    store.propose_binding(b["id"], "fake", {"symbol": "BTCV"}, "volume", "x")
    store.propose_binding(a["id"], "fake", {"symbol": "XBT"}, "price", "x")
    store.merge_nodes(a["id"], b["id"], "duplicate")
    roles = {x["role"]: x["key"] for x in store.list_bindings(a["id"])}
    assert roles == {"price": "fake:symbol=XBT", "volume": "fake:symbol=BTCV"}

    store.propose_binding(c["id"], "fake", {"symbol": "ETH"}, "price", "x")
    archived = store.archive(c["id"], "remove")["archived"]
    assert any(x.startswith("b_") for x in archived) and not store.list_bindings(c["id"])


def test_reverting_a_node_takes_its_template_binding(store, human):
    node(store, "MacroIndicator", "美元指数", aliases=["DXY"])
    cs = store.submit_changeset("node")
    human.accept_changeset(cs["id"])
    assert len(store.apply_templates()) == 1
    human.revert_changeset(cs["id"])
    assert store.conn.execute("SELECT count(*) AS n FROM bindings").fetchone()["n"] == 0


def test_manual_csv_import(store, human, tmp_path):
    from ontology import cli

    etf = node(store, "Metric", "ETF 净流入")
    human.accept_changeset(store.submit_changeset("x")["id"])
    f = tmp_path / "flows.csv"
    f.write_text("Date,Total\n2026-10-14,\"1,250.5\"\n2026-10-15,-310\n2026-10-16,\n", encoding="utf-8")
    points = cli._read_csv(str(f))
    assert [p.value for p in points] == [1250.5, -310.0]
    b = human.bind_direct(etf["id"], "manual", {"name": "ETF 净流入"}, "value", "导入", unit="百万美元")["binding"]
    assert human.series.write(b["series_id"], points) == 2
    assert human.series.write(b["series_id"], points) == 0  # idempotent
    s = store.list_bindings(etf["id"])[0]
    assert s["latest"]["value"] == -310.0 and s["stale"] is False
    assert store.series.collectable() == []  # manual series are never polled


def test_a_source_removed_from_the_catalog_keeps_its_points_but_is_not_fetched(store, human, fake):
    from ontology import collector

    btc = node(store, "Asset", "比特币", props=BTC)
    store.propose_binding(btc["id"], "fake", {"symbol": "BTC"}, "price", "价格")
    human.accept_changeset(store.submit_changeset("node + binding")["id"])
    collector.run_once(store.conn)
    assert len(store.series.collectable()) == 1

    del sources.SOURCES["fake"]  # the connector is gone; the project still has the binding and its points
    assert store.series.collectable() == []
    assert collector.run_once(store.conn) is not None
    shown = store.list_bindings(btc["id"])[0]
    assert shown["latest"] is not None and shown["stale"] is False


def test_parsers_on_recorded_payloads():
    fred = sources.parse_fred_csv("observation_date,DFF\n2026-10-15,4.33\n2026-10-16,.\n")
    assert len(fred) == 1 and fred[0].value == 4.33


def test_fred_html_page_is_a_source_error_not_a_crash():
    import pytest

    with pytest.raises(OntologyError, match="不是 CSV"):
        sources.parse_fred_csv('<!DOCTYPE html>\n    <link href="https://fonts.googleapis.com/css?family=Open+Sans:300">')


def test_one_broken_source_does_not_stop_the_pass(store, human, monkeypatch):
    from ontology import collector

    s = store.series.ensure_series("manual", {"name": "x"})
    monkeypatch.setattr(sources.source("manual"), "fetch", lambda p, since: 1 / 0)
    assert collector.collect_series(store, store.series.get_series(s["id"]), datetime.now(UTC)) == 0
    assert "ZeroDivisionError" in store.series.get_series(s["id"])["last_error"]
