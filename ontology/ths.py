"""同花顺金融数据 (fuyao.aicubes.cn): A-share and index prices, valuations, financial indicators, lookups.

Needs THS_API_KEY in .env (issued on the site's「API Key 管理」page). Every response comes in an envelope
{code, message, data}; code 0 is success, 4001 / HTTP 429 mean slow down. Timestamps are milliseconds in
Asia/Shanghai; daily bars are stamped at local midnight.

Connectors here feed series (bindings); the query helpers back the agent's a_share_* tools.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .errors import OntologyError
from .providers import PROVIDERS
from . import sources as _sources  # called through the module so tests can stub the network in one place
from .sources import Obs, Source, _utc_ms

SH = ZoneInfo("Asia/Shanghai")
VALUATION_METRICS = {"pe_ttm": "市盈率 TTM", "pe_mrq": "市盈率 MRQ", "pb_mrq": "市净率 MRQ", "ps_ttm": "市销率 TTM",
                     "pcf_ttm": "市现率 TTM"}
INDICATORS = {
    "total_assets_growth_ratio": "总资产增长率", "net_profit_yoy_growth_ratio": "净利润同比增长率",
    "operating_income_yoy_growth_ratio": "营业收入同比增长率", "operating_profit_yoy_growth_ratio": "营业利润同比增长率",
    "sale_gross_margin": "销售毛利率", "sale_net_interest_ratio": "销售净利率", "total_assets_net_ratio": "总资产收益率",
    "index_deduct_weighted_avg_roe": "扣非加权净资产收益率", "index_weighted_avg_roe": "净资产收益率",
    "current_ratio": "流动比率", "quick_ratio": "速动比率", "assets_debt_ratio": "资产负债率", "cash_ratio": "现金比率",
}
# The API names some growth indicators differently from the short ids used in series keys.
API_IDS = {"operating_income_yoy_growth_ratio": "calculate_operating_income_yoy_growth_ratio",
           "operating_profit_yoy_growth_ratio": "calculate_operating_profit_yoy_growth_ratio",
           "net_profit_yoy_growth_ratio": "calculate_parent_holder_net_profit_yoy_growth_ratio"}
SHORT_IDS = {v: k for k, v in API_IDS.items()}
QUARTER_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
# Reports come out up to four months after the period ends; until then the API answers 3002 or 5003.
PUBLISH_WINDOW = timedelta(days=125)


def base_url() -> str:
    return os.environ.get("THS_BASE_URL", "https://fuyao.aicubes.cn").rstrip("/")


def call(path: str, **params: Any) -> Any:
    """GET an endpoint and return its `data`, or raise OntologyError with the provider's own message."""
    key = PROVIDERS["ths"].key()
    if not key:
        raise OntologyError("同花顺数据源需要 THS_API_KEY：在官网「API Key 管理」签发后写进 .env，并重启工作台")
    for attempt in (1, 2):
        try:
            body = _sources._get(f"{base_url()}{path}", provider="ths", headers={"X-api-key": key},
                        **{k: v for k, v in params.items() if v is not None}).json()
        except OntologyError as e:
            if "HTTP 429" in str(e) and attempt == 1:
                time.sleep(2)
                continue
            raise
        code = body.get("code")
        if code == 0:
            return body.get("data") or {}
        if code == 4001 and attempt == 1:
            time.sleep(2)
            continue
        raise OntologyError(f"同花顺 {path}: code {code} {body.get('message', '')}".strip())
    raise AssertionError("unreachable")


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _local_day(dt: datetime) -> datetime:
    """Midnight in Shanghai of the day `dt` falls on, as an aware datetime."""
    d = dt.astimezone(SH)
    return datetime(d.year, d.month, d.day, tzinfo=SH)


def _num(v: Any) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ parsing (kept pure for tests)


def parse_bars(data: dict[str, Any]) -> list[Obs]:
    return [Obs(_utc_ms(b["date_ms"]), float(b["close_price"]),
                {"o": b.get("open_price"), "h": b.get("high_price"), "l": b.get("low_price"), "v": b.get("volume"),
                 "turnover": b.get("turnover")})
            for b in data.get("item", []) if b.get("close_price") is not None]


def provisional_from_snapshot(data: dict[str, Any], thscode: str, bars: list[Obs]) -> Obs | None:
    """Today's still-forming bar from a snapshot, when it is newer than the last daily bar."""
    item = next((i for i in data.get("item", []) if i.get("thscode") == thscode), None)
    if not item or item.get("last_price") is None:
        return None
    stamp = _utc_ms(data["timestamp"]) if data.get("timestamp") else datetime.now(UTC)
    day = _local_day(stamp)
    if bars and _local_day(bars[-1].ts) >= day:
        return None
    return Obs(day.astimezone(UTC), float(item["last_price"]),
               {"o": item.get("open_price"), "h": item.get("high_price"), "l": item.get("low_price"),
                "v": item.get("volume"), "turnover": item.get("turnover"), "open": True,
                "change_pct": item.get("price_change_ratio_pct")})


def parse_indicator(data: dict[str, Any], index_id: str) -> float | None:
    wanted = {index_id, API_IDS.get(index_id, index_id)}
    for block in data.get("abilities", []):
        for ind in block.get("indicators", []):
            if ind.get("index_id") in wanted:
                return _num(ind.get("value"))
    return None


def not_published(e: OntologyError, end: datetime, now: datetime) -> bool:
    """Whether an error for a report period just means the report is not out yet."""
    return "code 3002" in str(e) or ("code 5003" in str(e) and now - end < PUBLISH_WINDOW)


def report_periods(since: datetime, now: datetime) -> list[tuple[str, datetime]]:
    """Report ids (yyyy-q) whose period ended between `since` and `now`, oldest first, with the period-end date."""
    out = []
    for year in range(since.astimezone(SH).year, now.astimezone(SH).year + 1):
        for q, (m, d) in QUARTER_END.items():
            end = datetime(year, m, d, tzinfo=SH)
            if since <= end <= now:
                out.append((f"{year}-{q}", end))
    return out


# ------------------------------------------------------------------ connectors


class ThsStockDaily(Source):
    """Daily bars of one A-share (forward-adjusted by default), plus today's bar from the live snapshot."""

    def label(self, p):
        adj = {"forward": "前复权", "backward": "后复权", "none": "不复权"}.get(p["adjust"], p["adjust"])
        return f"同花顺 {p['thscode']} 日K（{adj}）"

    def fetch(self, p, since):
        now = datetime.now(UTC)
        since = max(since, now - timedelta(days=3650))
        bars = parse_bars(call("/api/a-share/prices/historical", thscode=p["thscode"], interval="1d",
                               start=_ms(since), end=_ms(now), adjust=p["adjust"]))
        live = provisional_from_snapshot(call("/api/a-share/prices/snapshot", thscodes=p["thscode"]), p["thscode"], bars)
        return bars + ([live] if live else [])


class ThsIndexDaily(Source):
    """Daily bars of an index or 同花顺 sector (the API keeps the last 5 years), plus today's bar."""

    def label(self, p):
        return f"同花顺 {p['thscode']} 指数日K"

    def fetch(self, p, since):
        now = datetime.now(UTC)
        since = max(since, now - timedelta(days=5 * 365 - 5))
        bars = parse_bars(call("/api/a-share-index/prices/historical", thscode=p["thscode"], interval="1d",
                               start=_ms(since), end=_ms(now)))
        live = provisional_from_snapshot(call("/api/a-share-index/prices/snapshot", thscodes=p["thscode"]),
                                         p["thscode"], bars)
        return bars + ([live] if live else [])


class ThsValuation(Source):
    """One valuation multiple of an A-share. The API only has the latest value, so history builds up from
    the day the binding starts (one point per trading day)."""

    def label(self, p):
        return f"同花顺 {p['thscode']} {VALUATION_METRICS.get(p['metric'], p['metric'])}"

    def clean(self, params):
        p = super().clean(params)
        if p["metric"] not in VALUATION_METRICS:
            raise OntologyError(f"metric must be one of {list(VALUATION_METRICS)}")
        return p

    def fetch(self, p, since):
        data = call("/api/a-share/valuations/snapshot", thscodes=p["thscode"])
        item = next((i for i in data.get("item", []) if i.get("thscode") == p["thscode"]), None)
        value = _num(item.get(p["metric"])) if item else None
        if value is None:
            return []
        stamp = _utc_ms(data["timestamp"]) if data.get("timestamp") else datetime.now(UTC)
        return [Obs(_local_day(stamp).astimezone(UTC), value)]


class ThsFinIndicator(Source):
    """One financial indicator of an A-share per report period (quarterly), stamped at the period end."""

    def label(self, p):
        return f"同花顺 {p['thscode']} {INDICATORS.get(p['metric'], p['metric'])}"

    def frequency(self, p):
        return "1q"

    def clean(self, params):
        p = super().clean(params)
        if p["metric"] not in INDICATORS:
            raise OntologyError(f"metric must be one of {list(INDICATORS)}")
        return p

    def fetch(self, p, since):
        out = []
        now = datetime.now(UTC)
        for report, end in report_periods(since, now):
            try:
                data = call("/api/a-share/financials/indicators", thscode=p["thscode"], report=report)
            except OntologyError as e:
                if not_published(e, end, now):
                    continue
                raise
            value = parse_indicator(data, p["metric"])
            if value is not None:
                out.append(Obs(end.astimezone(UTC), value, {"report": report}))
        return out


SOURCES = [
    ThsStockDaily("ths_stock_daily", "同花顺 A 股日 K", {"thscode": "完整代码，如 600519.SH", "adjust": "forward / backward / none"},
                  "CNY", timedelta(minutes=15), timedelta(days=5 * 365), {"adjust": "forward"},
                  lag=timedelta(days=5), provider="ths"),
    ThsIndexDaily("ths_index_daily", "同花顺指数 / 板块日 K", {"thscode": "指数代码，如 000300.SH、886042.TI"},
                  "点", timedelta(minutes=15), timedelta(days=4 * 365), lag=timedelta(days=5), provider="ths"),
    ThsValuation("ths_valuation", "同花顺估值", {"thscode": "A 股代码", "metric": " / ".join(VALUATION_METRICS)},
                 "倍", timedelta(hours=6), timedelta(0), {"metric": "pe_ttm"}, lag=timedelta(days=5), provider="ths"),
    ThsFinIndicator("ths_fin_indicator", "同花顺财务指标", {"thscode": "A 股代码", "metric": "指标 id，如 index_weighted_avg_roe"},
                    None, timedelta(days=1), timedelta(days=3 * 365), lag=timedelta(days=120), provider="ths"),
]


# ------------------------------------------------------------------ agent queries


def search(query: str, asset_type: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    data = call("/api/meta/tickers/search", q=query, asset_type=asset_type, limit=max(1, min(limit, 50)))
    return [{k: i.get(k) for k in ("thscode", "name", "asset_type", "exchange", "list_date")} for i in data.get("item", [])]


def quote(thscodes: list[str]) -> list[dict[str, Any]]:
    """Latest price for stocks and indices, plus valuation multiples for stocks. Codes the stock snapshot
    does not know (indices, 同花顺 sectors) are looked up in the index snapshot."""
    codes = list(dict.fromkeys(c.strip().upper() for c in thscodes if c.strip()))[:50]
    if not codes:
        raise OntologyError("thscodes is empty")
    out: dict[str, dict[str, Any]] = {c: {"thscode": c} for c in codes}
    stocks = [c for c in codes if not c.endswith(".TI")]
    if stocks:
        try:
            for i in call("/api/a-share/prices/snapshot", thscodes=",".join(stocks)).get("item", []):
                if i.get("thscode") in out:
                    out[i["thscode"]].update(i, kind="stock")
        except OntologyError:
            pass  # e.g. an index code in the list; those are retried below
        priced = [c for c in stocks if out[c].get("kind") == "stock"]
        if priced:
            for i in call("/api/a-share/valuations/snapshot", thscodes=",".join(priced)).get("item", []):
                if i.get("thscode") in out:
                    out[i["thscode"]].update({k: i.get(k) for k in ("name", *VALUATION_METRICS)})
    rest = [c for c in codes if out[c].get("kind") != "stock"]
    if rest:
        for i in call("/api/a-share-index/prices/snapshot", thscodes=",".join(rest)).get("item", []):
            if i.get("thscode") in out:
                out[i["thscode"]].update(i, kind="index")
    return list(out.values())


def financials(thscode: str, report: str | None = None) -> dict[str, Any]:
    """The five indicator groups for one report period (default: the latest published one)."""
    now = datetime.now(UTC)
    periods = list(reversed(report_periods(now - timedelta(days=400), now)))
    candidates = [(report, None)] if report else periods
    last_error = None
    for r, end in candidates:
        try:
            data = call("/api/a-share/financials/indicators", thscode=thscode, report=r)
        except OntologyError as e:
            last_error = e
            if end is not None and not_published(e, end, now):
                continue
            raise
        return {"thscode": thscode, "report": r, "indicators": {
            INDICATORS.get(SHORT_IDS.get(i["index_id"], i["index_id"]), i["index_id"]): _num(i.get("value"))
            for b in data.get("abilities", []) for i in b.get("indicators", [])}}
    raise last_error or OntologyError(f"no financial report found for {thscode}")


def index_members(thscode: str) -> list[dict[str, Any]]:
    return [{k: i.get(k) for k in ("thscode", "name")}
            for i in call("/api/a-share-index/constituents/ths-stock-list", thscode=thscode).get("item", [])]
