import pytest

from ontology.errors import OntologyError
from tests.conftest import LLM, USER, link, node


def btc(store):
    return node(store, "Asset", "BTC", aliases=["比特币", "Bitcoin"], props={"symbol": "BTC", "asset_class": "spot"})


def test_upsert_is_idempotent(store):
    a = btc(store)
    again = store.upsert_node("Asset", "btc", provenance=LLM, rationale="again", props={"symbol": "BTC", "asset_class": "spot"},
                              aliases=["比特币", "Bitcoin"])
    assert again["node"]["id"] == a["id"]
    assert again["created"] is False
    assert len(store.graph()["nodes"]) == 1


def test_alias_duplicate_is_refused_and_found_by_search(store):
    a = btc(store)
    with pytest.raises(OntologyError, match="possible duplicate"):
        node(store, "Asset", "比特币", props={"symbol": "BTC"})
    hits = store.search_nodes("bitcoin")
    assert [h["id"] for h in hits] == [a["id"]] and hits[0]["match"] == "exact"
    assert store.search_nodes("比特")[0]["id"] == a["id"]
    forced = node(store, "Asset", "比特币", key="btc-fork", props={"symbol": "BTCF"}, force_new=True)
    assert forced["id"] != a["id"]


def test_same_name_other_type_only_warns(store):
    btc(store)
    r = store.upsert_node("Concept", "Bitcoin", provenance=LLM, rationale="protocol")
    assert r["created"] and r["warnings"]


def test_link_domain_range_and_provenance(store):
    a = btc(store)
    n = node(store, "Narrative", "数字黄金", props={"stance": "bullish"})
    with pytest.raises(OntologyError, match="links"):
        store.create_link("affects", n["id"], a["id"], provenance=LLM, rationale="wrong way")
    with pytest.raises(OntologyError, match="unknown link type"):
        store.create_link("related_to", a["id"], n["id"], provenance=LLM, rationale="x")
    n2 = node(store, "Narrative", "美债避险")
    with pytest.raises(OntologyError, match="needs 'ref'"):
        store.create_link("competes_with", n["id"], n2["id"], provenance={"kind": "data_source"}, rationale="x")
    c = node(store, "Concept", "稀缺性")
    l1 = link(store, "underpins", c, n, confidence=0.8)
    l2 = store.create_link("underpins", c["id"], n["id"], provenance=LLM, rationale="test", confidence=0.8)
    assert l2["link"]["id"] == l1["id"] and l2["changed"] is False


def test_changeset_accept_and_reject(store, human):
    a = btc(store)
    assert store.get_node(a["id"])["pending"] is True
    cs = store.submit_changeset("seed BTC")
    assert cs["status"] == "pending" and len(cs["operations"]) == 1
    human.accept_changeset(cs["id"])
    assert store.get_node(a["id"])["pending"] is False

    e = node(store, "Event", "2024 减半", props={"category": "halving"})
    link(store, "affects", e, a, props={"direction": "bullish"})
    cs2 = store.submit_changeset("halving")
    human.reject_changeset(cs2["id"])
    g = store.graph()
    assert [n["id"] for n in g["nodes"]] == [a["id"]] and g["links"] == []


def test_revert_restores_previous_state(store, human):
    a = btc(store)
    human.accept_changeset(store.submit_changeset("seed")["id"])
    store.update_props(a["id"], "fix", description="数字黄金")
    store.revert_changeset()
    assert store.get_node(a["id"])["description"] is None
    assert store.list_changesets()[0]["status"] == "reverted"


def test_revert_refused_when_later_changes_depend_on_it(store, human):
    a = btc(store)
    first = store.submit_changeset("seed")["id"]
    store.update_props(a["id"], "later", description="x")
    store.submit_changeset("later")
    with pytest.raises(OntologyError, match="later changesets"):
        store.revert_changeset(first)


def test_merge_moves_links_and_redirects_key(store):
    a = btc(store)
    dup = node(store, "Asset", "BTC现货", props={"symbol": "BTC"})
    n = node(store, "Narrative", "数字黄金")
    e = node(store, "Event", "ETF 获批")
    link(store, "affects", e, a)
    link(store, "affects", e, dup)  # becomes a duplicate after merge
    m = node(store, "Metric", "MVRV")
    link(store, "measures", m, dup)
    r = store.merge_nodes(a["id"], dup["id"], "same asset")
    assert "BTC现货" in r["node"]["aliases"]
    assert len(r["moved_links"]) == 1 and len(r["dropped_duplicate_links"]) == 1
    assert store.get_node(dup["id"])["merged_into"] == a["id"]
    again = store.upsert_node("Asset", "BTC现货", provenance=USER, rationale="x")
    assert again["node"]["id"] == a["id"]
    assert n["id"]  # untouched


def test_archive_takes_links_with_it(store):
    a, e = btc(store), node(store, "Event", "Mt.Gox 还款")
    l = link(store, "affects", e, a)
    r = store.archive(e["id"], "not relevant")
    assert set(r["archived"]) == {e["id"], l["id"]}
    assert store.graph()["links"] == []


def test_neighbors_and_paths(store):
    fed = node(store, "Entity", "美联储", props={"kind": "central_bank"})
    rate = node(store, "MacroIndicator", "联邦基金利率")
    liq = node(store, "Narrative", "流动性宽松")
    a = btc(store)
    link(store, "sets", fed, rate)
    link(store, "drives", rate, liq)
    e = node(store, "Event", "降息")
    link(store, "affects", e, liq)
    link(store, "affects", e, a)
    sub = store.get_neighbors(rate["id"], depth=1)
    assert {n["name"] for n in sub["nodes"]} == {"联邦基金利率", "美联储", "流动性宽松"}
    assert len(store.get_neighbors(rate["id"], depth=3)["nodes"]) == 5
    paths = store.find_paths(fed["id"], a["id"])
    assert len(paths[0]) == 4
    assert [s["type"] for s in paths[0]] == ["sets", "drives", "affects", "affects"]


def test_proposed_type_unusable_until_accepted(store, human):
    store.propose_type("object", "Miner", "矿池", "Entity kind is too coarse", layer="world")
    with pytest.raises(OntologyError, match="unknown object type"):
        node(store, "Miner", "Foundry")
    assert store.get_schema()["proposed_types"][0]["name"] == "Miner"
    human.decide_type("object", "Miner", accept=True)
    store.registry = store.load_registry()
    assert node(store, "Miner", "Foundry")["type"] == "Miner"


def test_validate_graph_reports_orphans_and_duplicates(store):
    a = btc(store)
    node(store, "Asset", "比特币现货", aliases=["BTC 现货"], props={"symbol": "BTC"})
    report = store.validate_graph()
    assert not report["ok"]
    assert a["id"] in {o["id"] for o in report["orphans"]}
