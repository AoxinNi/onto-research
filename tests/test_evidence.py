import math
import random
from datetime import UTC, datetime, timedelta

from ontology import evidence
from ontology.sources import Obs
from tests.conftest import link, node

DAY = timedelta(days=1)


def series_pair(n=120, lag=2, noise=0.3, seed=7):
    """b follows a's daily returns `lag` days later, plus noise."""
    rnd = random.Random(seed)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    ra = [rnd.gauss(0, 0.02) for _ in range(n)]
    a, b, pa, pb = [], [], 100.0, 50.0
    for i in range(n):
        pa *= math.exp(ra[i])
        pb *= math.exp((ra[i - lag] if i >= lag else 0) + rnd.gauss(0, 0.02 * noise))
        a.append((t0 + i * DAY, pa))
        b.append((t0 + i * DAY, pb))
    return a, b


A = {"id": "s_a", "label": "A", "frequency": "1d", "log": True}
B = {"id": "s_b", "label": "B", "frequency": "1d", "log": True}


def test_compare_finds_the_lead_and_its_direction():
    a, b = series_pair()
    res = evidence.compare(A, B, a, b, "甲", "乙")
    assert res["verdict"] == "supports" and res["stat"]["best_lag"] == 2 and res["stat"]["best_r"] > 0.8
    assert "甲 领先 2 天" in res["summary"] and "显著" in res["summary"]
    rev = evidence.compare(B, A, b, a, "乙", "甲")
    assert rev["verdict"] == "reverse" and rev["stat"]["best_lag"] == -2


def test_compare_on_noise_and_on_too_little_data():
    rnd = random.Random(1)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    a = [(t0 + i * DAY, 100 + rnd.random()) for i in range(120)]
    b = [(t0 + i * DAY, 100 + rnd.random()) for i in range(120)]
    assert evidence.compare(A, B, a, b, "甲", "乙")["verdict"] == "weak"
    short = evidence.compare(A, B, a[:10], b[:10], "甲", "乙")
    assert short["verdict"] == "insufficient" and "数据不足" in short["summary"]


def test_monthly_against_hourly_uses_the_coarser_bucket():
    assert evidence.bucket_for("1h", "1mo") == "month" and evidence.bucket_for("1h", "8h") == "day"


def test_score_combines_and_penalises():
    s = evidence.score([{"verdict": "supports", "strength": 0.5}, {"verdict": "supports", "strength": 0.5}])
    assert s == 0.75
    assert evidence.score([{"verdict": "supports", "strength": 0.6}, {"verdict": "reverse", "strength": 0.4}]) == 0.4


def test_refresh_attaches_series_and_news_evidence_to_links(store, human):
    rate = node(store, "MacroIndicator", "利率")
    btc = node(store, "Asset", "比特币", props={"symbol": "BTC", "asset_class": "spot"})
    l = link(store, "drives", rate, btc)
    human.accept_changeset(store.submit_changeset("seed")["id"])
    a, b = series_pair()
    for n, pts, name in ((rate, a, "利率"), (btc, b, "比特币")):
        bd = human.bind_direct(n["id"], "manual", {"name": name}, "value", "test")["binding"]
        human.series.write(bd["series_id"], [Obs(t, v) for t, v in pts])
    # Two articles mention both ends.
    for i in range(2):
        store.conn.execute("INSERT INTO documents (id, feed, url, title, published_at, relevant) VALUES (%s, %s, %s, 't', now(), true)",
                           (f"d{i}", f"f{i}", f"https://x/{i}"))
        for nid, s in ((rate["id"], -0.4), (btc["id"], -0.2)):
            store.conn.execute("INSERT INTO mentions (document_id, node_id, sentiment, relevance) VALUES (%s, %s, %s, 0.9)",
                               (f"d{i}", nid, s))
    now = datetime(2026, 4, 30, tzinfo=UTC)
    assert evidence.refresh(store.conn, now) == 1
    assert evidence.refresh(store.conn, now) == 0  # not again within the hour
    ev = evidence.link_evidence(store.conn, l["id"])
    kinds = {e["kind"]: e for e in ev["evidence"]}
    assert kinds["series"]["verdict"] == "supports" and kinds["series"]["stat"]["best_lag"] == 2
    assert kinds["news"]["stat"]["count"] == 2 and kinds["news"]["stat"]["agreement"] == 1.0
    assert 0 < ev["strength"] <= 1
    assert store.graph()["evidence"][l["id"]]["strength"] == ev["strength"]

    noted = evidence.add_note(store.conn, l["id"], "FOMC 纪要显示降息预期升温", "supports", 0.4, ["https://x/fomc"], "test-agent")
    assert any(e["kind"] == "note" for e in noted["evidence"]) and noted["strength"] >= ev["strength"]

    # Undoing the link takes its evidence with it.
    human.archive(l["id"], "x")
    store.conn.execute("DELETE FROM links WHERE id = %s", (l["id"],))
    assert store.conn.execute("SELECT count(*) AS n FROM link_evidence").fetchone()["n"] == 0
