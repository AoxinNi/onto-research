from types import SimpleNamespace as NS

import pytest

from ontology import reports
from ontology.errors import OntologyError
from tests.conftest import link, node


def seed(store, human):
    btc = node(store, "Asset", "比特币", props={"symbol": "BTC", "asset_class": "spot"}, description="核心资产")
    rate = node(store, "MacroIndicator", "利率")
    l = link(store, "drives", rate, btc, rationale="降息利好风险资产")
    human.accept_changeset(store.submit_changeset("seed")["id"])
    return btc, rate, l


class FakeModel:
    def __init__(self, payload):
        self.payload, self.calls = payload, []

        class M:
            @staticmethod
            def create(**kw):
                self.calls.append(kw)
                return NS(stop_reason="tool_use", content=[NS(type="tool_use", input=self.payload(kw))])

        self.messages = M()


def test_validate_drops_unknown_ids_and_forward_references():
    raw = {"sections": [{"heading": "结论", "sentences": [
        {"id": "s1", "text": "利率下行。", "nodes": ["n1", "n_bogus"], "links": ["l1", "l_bogus"]},
        {"id": "s2", "text": "因此利好。", "from": ["s1", "s3"]},       # s3 comes later: dropped
        {"id": "s3", "text": "继续观察。", "from": ["s2"]},
        {"id": "s1", "text": "重复的编号被丢弃。"},
        {"id": "s4", "text": "   "},
    ]}]}
    body = reports.validate(raw, {"n1"}, {"l1"})
    ss = body["sections"][0]["sentences"]
    assert [s["id"] for s in ss] == ["s1", "s2", "s3"]
    assert ss[0]["nodes"] == ["n1"] and ss[0]["links"] == ["l1"] and ss[1]["from"] == ["s1"] and ss[2]["from"] == ["s2"]
    with pytest.raises(OntologyError):
        reports.validate({"sections": []}, set(), set())


def test_write_cites_the_digest_and_versions(store, human):
    btc, rate, l = seed(store, human)

    def answer(kw):
        digest = kw["messages"][0]["content"]
        assert btc["id"] in digest and l["id"] in digest and "降息利好风险资产" in digest
        return {"title": "利率与比特币", "summary": "利率下行利好。", "sections": [{"heading": "结论", "sentences": [
            {"id": "s1", "text": "利率驱动比特币。", "nodes": [rate["id"], btc["id"]], "links": [l["id"]]},
            {"id": "s2", "text": "因此偏多。", "from": ["s1"]}]}]}

    fake = FakeModel(answer)
    r = reports.write(store, fake)
    assert r["status"] == "ready" and r["version"] == 1 and r["title"] == "利率与比特币"
    assert fake.calls[0]["tool_choice"] == {"type": "tool", "name": "write_report"}
    assert r["body"]["sections"][0]["sentences"][1]["from"] == ["s1"]
    assert reports.latest(store.conn)["id"] == r["id"]
    assert r["graph_hash"] == reports.fingerprint(store)
    assert reports.write(store, fake)["version"] == 2


def test_fingerprint_follows_structure_not_noise(store, human):
    btc, rate, _ = seed(store, human)
    before = reports.fingerprint(store)
    assert reports.fingerprint(store) == before
    node(store, "Narrative", "数字黄金")
    assert reports.fingerprint(store) == before  # pending changes are not in the report yet
    human.accept_changeset(store.submit_changeset("more")["id"])
    assert reports.fingerprint(store) != before


def test_failures_are_recorded_and_raised(store, human):
    seed(store, human)

    class Broken:
        class messages:
            @staticmethod
            def create(**kw):
                raise RuntimeError("upstream down")

    with pytest.raises(RuntimeError):
        reports.write(store, Broken())
    last = reports.last_attempt(store.conn)
    assert last["status"] == "failed" and "upstream down" in last["error"] and reports.latest(store.conn) is None
