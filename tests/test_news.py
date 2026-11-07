from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

from ontology import news
from tests.conftest import node

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Spot bitcoin ETFs log $500M inflows</title><link>https://ex.com/a</link>
<description>&lt;p&gt;BlackRock's IBIT led as &lt;b&gt;BTC&lt;/b&gt; rallied.&lt;/p&gt;</description>
<pubDate>Mon, 19 Oct 2026 03:00:00 GMT</pubDate></item>
<item><title>Solana memecoin season returns</title><link>https://ex.com/b</link>
<pubDate>Mon, 19 Oct 2026 02:00:00 GMT</pubDate></item>
<item><title>No date</title><link>https://ex.com/c</link></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>美联储维持利率不变</title><link href="https://ex.cn/1"/><updated>2026-10-18T20:00:00Z</updated>
<summary>鲍威尔表示降息需要更多数据。</summary></entry></feed>"""


class FakeExtractor:
    """Plays the model: maps articles by keyword, flags the ETF article as an event."""

    def __init__(self, ids):
        self.ids, self.calls = ids, 0

    def extract(self, docs, nodes, events):
        self.calls += 1
        out = []
        for i, d in enumerate(docs):
            text = d["title"] + (d["summary"] or "")
            mentions, event = [], None
            if "ETF" in text:
                mentions += [{"node_id": self.ids["etf"], "sentiment": 0.8, "relevance": 0.9, "quote": "inflows"},
                             {"node_id": self.ids["btc"], "sentiment": 0.6, "relevance": 0.7}]
                event = {"title": "现货ETF单日大额净流入", "category": "etf", "severity": 3,
                         "node_ids": [self.ids["etf"], self.ids["btc"], self.ids["blackrock"], "n_unknown"]}
            if "美联储" in text:
                mentions.append({"node_id": self.ids["fed"], "sentiment": -0.3, "relevance": 1.0})
            mentions.append({"node_id": "n_unknown", "sentiment": 1, "relevance": 1})  # ignored
            out.append({"doc": i, "mentions": mentions, "event": event})
        return out


def seed(store, human):
    ids = {
        "btc": node(store, "Asset", "比特币", aliases=["BTC", "Bitcoin"], props={"symbol": "BTC", "asset_class": "spot"})["id"],
        "etf": node(store, "Narrative", "ETF与机构化", aliases=["ETF Institutionalization"])["id"],
        "fed": node(store, "Entity", "美联储", aliases=["Fed"], props={"kind": "central_bank"})["id"],
        "blackrock": node(store, "Entity", "贝莱德", aliases=["BlackRock"], props={"kind": "etf_issuer"})["id"],
    }
    human.accept_changeset(store.submit_changeset("seed")["id"])
    return ids


def recent(docs):
    # Keep the fixtures inside the extraction window whatever day the tests run.
    now = datetime.now(UTC)
    return [{**d, "published_at": now - timedelta(hours=i + 1)} for i, d in enumerate(docs)]


def test_parse_feed_handles_rss_and_atom():
    rss = news.parse_feed(RSS)
    assert [d["url"] for d in rss] == ["https://ex.com/a", "https://ex.com/b"]
    assert rss[0]["summary"] == "BlackRock's IBIT led as BTC rallied."
    assert rss[0]["published_at"] == datetime(2026, 10, 19, 3, tzinfo=UTC)
    atom = news.parse_feed(ATOM)
    assert atom[0]["title"] == "美联储维持利率不变" and atom[0]["published_at"].tzinfo


def test_screen_keeps_btc_news_and_drops_the_rest(store, human):
    seed(store, human)
    assert news.store_documents(store.conn, "x", "en", recent(news.parse_feed(RSS))) == 2
    assert news.store_documents(store.conn, "x", "en", recent(news.parse_feed(RSS))) == 0  # dedup by URL
    news.store_documents(store.conn, "y", "zh", recent(news.parse_feed(ATOM)))
    news.screen(store.conn)
    rel = {r["title"]: r["relevant"] for r in store.conn.execute("SELECT title, relevant FROM documents")}
    assert rel == {"Spot bitcoin ETFs log $500M inflows": True, "Solana memecoin season returns": False,
                   "美联储维持利率不变": True}


def test_pass_extracts_mentions_heat_and_promotes_events_for_review(store, human):
    ids = seed(store, human)
    news.store_documents(store.conn, "coindesk", "en", recent(news.parse_feed(RSS)))
    news.store_documents(store.conn, "panews", "zh", recent(news.parse_feed(ATOM)))
    ex = FakeExtractor(ids)
    news.run(store.conn, ex, fetch=False)
    assert ex.calls == 1
    m = {(r["node_id"], r["document_id"]) for r in store.conn.execute("SELECT * FROM mentions")}
    assert len(m) == 3  # unknown node ids are dropped
    # One report at severity 3 is not enough to promote.
    assert store.conn.execute("SELECT count(*) AS n FROM objects WHERE type = 'Event'").fetchone()["n"] == 0

    # A second outlet reports the same event: it is promoted, with links, into a pending changeset.
    second = recent([{"url": "https://other.com/etf", "title": "ETF inflows top $500M, bitcoin climbs", "summary": ""}])
    news.store_documents(store.conn, "theblock", "en", second)
    news.run(store.conn, ex, fetch=False)
    ev = store.conn.execute("SELECT * FROM objects WHERE type = 'Event'").fetchone()
    assert ev["name"] == "现货ETF单日大额净流入" and ev["props"]["category"] == "etf"
    assert ev["provenance"]["kind"] == "data_source" and "2 篇报道" in ev["provenance"]["note"]
    links = {(r["type"], r["to_id"]) for r in store.conn.execute("SELECT * FROM links WHERE from_id = %s", (ev["id"],))}
    assert links == {("affects", ids["etf"]), ("affects", ids["btc"]), ("involves", ids["blackrock"])}
    review = [c for c in human.list_changesets("pending") if c["actor"] == "news-agent"]
    assert len(review) == 1 and "现货ETF单日大额净流入" in review[0]["summary"]

    # Rejecting it removes the event and its links, and it is not promoted again.
    human.reject_changeset(review[0]["id"])
    news.run(store.conn, ex, fetch=False)
    assert store.conn.execute("SELECT count(*) AS n FROM objects WHERE type = 'Event'").fetchone()["n"] == 0

    # Two mentions of the narrative: it now carries heat and sentiment series.
    roles = {b["role"]: b for b in store.list_bindings(ids["etf"])}
    assert set(roles) == {"heat", "sentiment"}
    assert roles["heat"]["latest"]["value"] > 0 and roles["sentiment"]["latest"]["value"] == 0.8
    assert news.node_news(store.conn, ids["etf"])[0]["sentiment"] == 0.8
    assert {d["url"] for d in news.search_news(store.conn, node_id=ids["fed"])} == {"https://ex.cn/1"}


def test_heat_decays_and_sentiment_is_weighted():
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    ms = [{"published_at": t0, "sentiment": 1.0, "relevance": 1.0},
          {"published_at": t0 + timedelta(hours=1), "sentiment": -1.0, "relevance": 0.5}]
    heat, sent = news.heat_and_sentiment(ms, [t0 - timedelta(hours=1), t0, t0 + timedelta(hours=1), t0 + timedelta(days=3)])
    assert [h.ts for h in heat] == [t0, t0 + timedelta(hours=1), t0 + timedelta(days=3)]  # nothing before the first article
    assert heat[0].value == 1.0 and heat[2].value < heat[1].value
    assert sent[0].value == 1.0 and 0 < sent[1].value < 1


def test_extractor_forces_the_tool_and_reads_its_input():
    calls = []

    class Client:
        class messages:
            @staticmethod
            def create(**kw):
                calls.append(kw)
                return NS(stop_reason="tool_use", content=[NS(type="tool_use", input={"docs": [{"doc": 0, "mentions": []}]})])

    ex = news.Extractor(client=Client(), model="m")
    docs = [{"feed": "x", "published_at": datetime.now(UTC), "title": "t", "summary": None}]
    assert ex.extract(docs, [{"id": "n1", "type": "Asset", "name": "比特币", "aliases": ["BTC"]}], []) == [{"doc": 0, "mentions": []}]
    assert calls[0]["tool_choice"] == {"type": "tool", "name": "record_extraction"}
    assert "n1 | Asset | 比特币 | BTC" in calls[0]["messages"][0]["content"]
