from datetime import UTC, datetime, timedelta

import pytest

from ontology import sources, ths
from ontology.errors import OntologyError
from tests.conftest import node

SH_DAY = lambda y, m, d: datetime(y, m, d, tzinfo=ths.SH)  # noqa: E731
MS = lambda dt: int(dt.timestamp() * 1000)  # noqa: E731


class R:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


@pytest.fixture
def api(monkeypatch):
    """Stub the 同花顺 HTTP layer: path -> envelope (or a list of envelopes served in turn)."""
    monkeypatch.setenv("THS_API_KEY", "test-key")
    routes, calls = {}, []

    def fake_get(url, **kw):
        path = url.replace(ths.base_url(), "")
        calls.append((path, kw))
        reply = routes[path]
        return R(reply.pop(0) if isinstance(reply, list) else reply)

    monkeypatch.setattr(sources, "_get", fake_get)
    return routes, calls


def ok(data):
    return {"code": 0, "message": "success", "data": data}


def test_parsers():
    bars = ths.parse_bars({"item": [{"date_ms": MS(SH_DAY(2026, 10, 19)), "open_price": 10, "high_price": 11,
                                     "low_price": 9, "close_price": 10.5, "volume": 100, "turnover": 1050}]})
    assert bars[0].value == 10.5 and bars[0].payload["turnover"] == 1050
    later = {"timestamp": MS(SH_DAY(2026, 10, 20) + timedelta(hours=10)), "item": [{"thscode": "600519.SH", "last_price": 11.2}]}
    live = ths.provisional_from_snapshot(later, "600519.SH", bars)
    assert live.value == 11.2 and live.payload["open"] and live.ts == SH_DAY(2026, 10, 20).astimezone(UTC)
    same = {"timestamp": MS(SH_DAY(2026, 10, 19) + timedelta(hours=15)), "item": [{"thscode": "600519.SH", "last_price": 10.5}]}
    assert ths.provisional_from_snapshot(same, "600519.SH", bars) is None  # the daily bar already covers today
    data = {"abilities": [{"ability": "profitability", "indicators": [{"index_id": "index_weighted_avg_roe", "value": "12.5000"},
                                                                      {"index_id": "cash_ratio", "value": None}]}]}
    assert ths.parse_indicator(data, "index_weighted_avg_roe") == 12.5 and ths.parse_indicator(data, "cash_ratio") is None
    periods = ths.report_periods(SH_DAY(2025, 5, 1), SH_DAY(2026, 4, 1))
    assert [r for r, _ in periods] == ["2025-2", "2025-3", "2025-4", "2026-1"]


def test_call_needs_a_key_and_reports_provider_errors(api, monkeypatch):
    routes, calls = api
    routes["/api/meta/tickers/search"] = [{"code": 4001, "message": "rate"}, ok({"item": [{"thscode": "600519.SH", "name": "贵州茅台"}]})]
    assert ths.search("茅台")[0]["thscode"] == "600519.SH"  # 4001 is retried once
    assert calls[0][1]["headers"] == {"X-api-key": "test-key"} and calls[0][1]["provider"] == "ths"
    routes["/api/a-share-index/constituents/ths-stock-list"] = {"code": 3001, "message": "标的不存在"}
    with pytest.raises(OntologyError, match="code 3001 标的不存在"):
        ths.index_members("999999.TI")
    monkeypatch.delenv("THS_API_KEY")
    with pytest.raises(OntologyError, match="THS_API_KEY"):
        ths.search("茅台")


def test_stock_daily_adds_todays_live_bar(api):
    routes, calls = api
    today = datetime.now(UTC)
    routes["/api/a-share/prices/historical"] = ok({"item": [{"date_ms": MS(ths._local_day(today) - timedelta(days=1)), "close_price": 100.0}]})
    routes["/api/a-share/prices/snapshot"] = ok({"timestamp": MS(today), "item": [{"thscode": "600519.SH", "last_price": 101.0}]})
    pts = sources.source("ths_stock_daily").fetch({"thscode": "600519.SH", "adjust": "forward"}, today - timedelta(days=5))
    assert [p.value for p in pts] == [100.0, 101.0] and pts[-1].payload["open"]
    assert calls[0][1]["adjust"] == "forward" and calls[0][1]["interval"] == "1d"


def test_fin_indicator_skips_unpublished_reports(api):
    routes, _ = api
    env = lambda v: ok({"abilities": [{"ability": "profitability", "indicators": [{"index_id": "index_weighted_avg_roe", "value": v}]}]})  # noqa: E731
    routes["/api/a-share/financials/indicators"] = [env("10.0"), env("11.0"), {"code": 3002, "message": "数据未就绪"}]
    now = datetime.now(UTC)
    since = now - timedelta(days=250)
    expected = len(ths.report_periods(since, now))
    if expected != 3:
        pytest.skip("depends on how many quarter ends fall in the window")
    pts = sources.source("ths_fin_indicator").fetch({"thscode": "600519.SH", "metric": "index_weighted_avg_roe"}, since)
    assert [p.value for p in pts] == [10.0, 11.0] and pts[0].payload["report"]


def test_a_share_templates_wait_for_the_key(store, human, monkeypatch):
    monkeypatch.delenv("THS_API_KEY", raising=False)
    node(store, "Asset", "贵州茅台", props={"symbol": "600519.SH", "asset_class": "equity"})
    node(store, "Asset", "沪深300", props={"symbol": "000300.SH", "asset_class": "index"})
    human.accept_changeset(store.submit_changeset("x")["id"])
    assert store.apply_templates() == []
    monkeypatch.setenv("THS_API_KEY", "k")
    made = {(b["role"], b["series_key"]) for b in store.apply_templates()}
    assert made == {
        ("price", "ths_stock_daily:adjust=forward,thscode=600519.SH"),
        ("valuation", "ths_valuation:metric=pe_ttm,thscode=600519.SH"),
        ("roe", "ths_fin_indicator:metric=index_weighted_avg_roe,thscode=600519.SH"),
        ("revenue_growth", "ths_fin_indicator:metric=operating_income_yoy_growth_ratio,thscode=600519.SH"),
        ("price", "ths_index_daily:thscode=000300.SH"),
    }


def test_unpublished_report_errors_and_api_indicator_names():
    now = datetime(2026, 10, 21, tzinfo=UTC)
    recent, old = datetime(2026, 9, 30, tzinfo=UTC), datetime(2025, 12, 31, tzinfo=UTC)
    gone = OntologyError("同花顺 /api/a-share/financials/indicators: code 5003 Data source unavailable")
    assert ths.not_published(gone, recent, now) and not ths.not_published(gone, old, now)
    assert ths.not_published(OntologyError("code 3002 数据未就绪"), old, now)
    data = {"abilities": [{"ability": "growth", "indicators": [
        {"index_id": "calculate_operating_income_yoy_growth_ratio", "value": "16.48"}]}]}
    assert ths.parse_indicator(data, "operating_income_yoy_growth_ratio") == 16.48
