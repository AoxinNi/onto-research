from datetime import UTC, datetime, timedelta

from ontology import history
from ontology.sources import Obs
from tests.conftest import link, node


def accept(store, human, summary="x"):
    return human.accept_changeset(store.submit_changeset(summary)["id"])


def test_commit_only_when_accepted_graph_changes(store, human):
    assert history.commit_if_changed(store, "system", "开始记录")["parent"] is None  # empty graph is the root
    assert history.commit_if_changed(store, "system", "again") is None

    btc = node(store, "Asset", "比特币", props={"symbol": "BTC"})
    assert history.commit_if_changed(store, "system", "pending only") is None  # pending work is not history

    accept(store, human)
    c = history.commit_if_changed(store, "human", "接受")
    assert c["stats"]["nodes_added"] == 1 and c["message"].startswith("接受（+1 节点")

    rate = node(store, "MacroIndicator", "利率")
    link(store, "drives", rate, btc)
    accept(store, human)
    c2 = history.commit_if_changed(store, "human", "接受")
    assert c2["parent"] == c["id"] and c2["stats"]["links_added"] == 1

    log = history.log(store.conn)
    assert [x["id"] for x in log][-2:] == [c["id"], c2["id"]]


def test_graph_at_returns_past_structure_and_data_as_of_commit(store, human):
    btc = node(store, "Asset", "比特币", props={"symbol": "BTC"})
    accept(store, human)
    sid = human.bind_direct(btc["id"], "manual", {"name": "BTC 价格"}, "price", "导入", unit="USD")["binding"]["series_id"]
    now = datetime.now(UTC)
    store.series.write(sid, [Obs(now - timedelta(days=2), 100.0), Obs(now - timedelta(days=1), 110.0)])
    first = history.commit_if_changed(store, "human", "一")

    store.series.write(sid, [Obs(now + timedelta(hours=1), 200.0)])  # later value: not visible at `first`
    node(store, "MacroIndicator", "利率")
    accept(store, human)
    history.commit_if_changed(store, "human", "二")

    past = history.graph_at(store, first["id"])
    assert past["commit"]["id"] == first["id"]
    assert [n["name"] for n in past["graph"]["nodes"]] == ["比特币"]
    assert past["graph"]["data"][btc["id"]][0]["latest"]["value"] == 110.0
