"""Data source catalog: what can be collected, and how.

Each source turns a few params (a symbol, a FRED code …) into a series of (ts, value) points. Parsing is
kept apart from HTTP so it can be tested on recorded payloads. Only free, keyless endpoints for now.
"""

from __future__ import annotations

import csv
import io
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .errors import OntologyError
from .providers import throttle

TIMEOUT = httpx.Timeout(20.0, connect=10.0)
RETRY_PAUSES = (0.6, 1.8)  # seconds before the 2nd and 3rd attempt
FREQ_SECONDS = {"5m": 300, "1h": 3600, "4h": 14400, "8h": 28800, "1d": 86400, "1w": 604800, "1mo": 2_629_800, "1q": 7_889_400}


@dataclass
class Obs:
    ts: datetime
    value: float
    payload: dict[str, Any] | None = None


def _utc_ms(ms: int | str) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC)


@dataclass
class Source:
    name: str
    title: str
    params: dict[str, str]                 # param -> help text
    unit: str | None
    poll: timedelta                        # how often to ask for new points
    backfill: timedelta                    # history to load the first time
    defaults: dict[str, str] = field(default_factory=dict)
    lag: timedelta = timedelta(0)          # how far behind real time the newest point normally is
    polled: bool = True                    # False: written by a human or computed in-house, never fetched
    provider: str = "internal"             # who supplies it (ontology.providers)

    def clean(self, params: dict[str, Any]) -> dict[str, str]:
        out = {**self.defaults, **{k: str(v).strip() for k, v in (params or {}).items() if v is not None}}
        unknown = set(out) - set(self.params)
        if unknown:
            raise OntologyError(f"source {self.name} has no params {sorted(unknown)}; it takes {list(self.params)}")
        missing = [p for p in self.params if not out.get(p)]
        if missing:
            raise OntologyError(f"source {self.name} needs params {missing}")
        return out

    def label(self, p: dict[str, str]) -> str:
        return self.title

    def frequency(self, p: dict[str, str]) -> str:
        return "1d"

    def backfill_for(self, p: dict[str, str]) -> timedelta:
        return self.backfill

    def fetch(self, p: dict[str, str], since: datetime) -> list[Obs]:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {"source": self.name, "title": self.title, "params": self.params, "defaults": self.defaults,
                "unit": self.unit, "provider": self.provider}


# Some CDNs (FRED's among them) stall on bare or browser-like agents; a descriptive one with a URL gets through.
USER_AGENT = "onto-research/0.1"


def _get(url: str, *, retry: bool = True, provider: str | None = None, headers: dict[str, str] | None = None,
         **params: Any) -> httpx.Response:
    # Local proxies drop TLS connections now and then; retry with a short backoff before giving up.
    for attempt, pause in enumerate((RETRY_PAUSES if retry else ()) + (None,)):
        throttle(provider)
        try:
            r = httpx.get(url, params=params, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT, **(headers or {})})
            r.raise_for_status()
            return r
        except httpx.HTTPStatusError as e:
            raise OntologyError(f"{url}: HTTP {e.response.status_code}") from None
        except httpx.HTTPError as e:
            if pause is None:
                raise OntologyError(f"{url}: {e or type(e).__name__}（已重试 {attempt} 次）") from None
            time.sleep(pause)
    raise AssertionError("unreachable")


# ------------------------------------------------------------------------- FRED


FRED_FREQ = {"DFF": "1d", "DFEDTARU": "1d", "DFEDTARL": "1d", "DGS10": "1d", "DGS2": "1d", "DTWEXBGS": "1d",
             "M2SL": "1mo", "CPIAUCSL": "1mo", "UNRATE": "1mo", "WALCL": "1w", "T10YIE": "1d"}


def parse_fred_csv(text: str) -> list[Obs]:
    rows = csv.reader(io.StringIO(text))
    header = next(rows, None)
    if not header or len(header) < 2 or "date" not in header[0].lower():
        # FRED sometimes answers with an HTML error or maintenance page and status 200.
        raise OntologyError(f"FRED 返回的不是 CSV：{text.strip()[:80]!r}")
    out = []
    for r in rows:
        if len(r) < 2 or r[1] in ("", "."):
            continue
        try:
            out.append(Obs(datetime.fromisoformat(r[0]).replace(tzinfo=UTC), float(r[1])))
        except ValueError:
            raise OntologyError(f"FRED CSV 里有无法解析的行：{r[:2]}") from None
    return out


class Fred(Source):
    def label(self, p):
        return f"FRED {p['series']}"

    def frequency(self, p):
        return FRED_FREQ.get(p["series"], "1d")

    def fetch(self, p, since):
        text = _get("https://fred.stlouisfed.org/graph/fredgraph.csv", provider=self.provider, id=p["series"],
                    cosd=since.date().isoformat()).text
        return [o for o in parse_fred_csv(text) if o.ts >= since]


# ----------------------------------------------------------------------- in-house


class Manual(Source):
    """Points imported by a human (CSV), e.g. ETF flows copied from Farside. Never polled."""

    def label(self, p):
        return p["name"]

    def frequency(self, p):
        return p.get("frequency") or "1d"

    def fetch(self, p, since):
        return []


class NewsSignal(Source):
    """Computed from news mentions of one node (see ontology.news). Never polled."""

    def label(self, p):
        return f"{p['name']} · {self.title}"

    def frequency(self, p):
        return "1h"

    def fetch(self, p, since):
        return []


SOURCES: dict[str, Source] = {s.name: s for s in [
    Fred("fred", "FRED 经济数据", {"series": "FRED 代码，如 DFF、DGS10、M2SL、CPIAUCSL、DTWEXBGS"},
         None, timedelta(hours=6), timedelta(days=3650), lag=timedelta(days=14), provider="fred"),
    Manual("manual", "手动导入（CSV）", {"name": "序列名称", "frequency": "1h / 1d / 1mo"}, None,
           timedelta(days=36500), timedelta(0), {"frequency": "1d"}, polled=False, provider="manual"),
    NewsSignal("news_heat", "新闻热度", {"node": "节点 id", "name": "节点名称"}, None, timedelta(days=36500),
               timedelta(0), polled=False),
    NewsSignal("news_sentiment", "新闻情绪", {"node": "节点 id", "name": "节点名称"}, None, timedelta(days=36500),
               timedelta(0), polled=False),
]}


def _register_providers() -> None:
    """Sources that live in their own provider modules (they import this one, so this runs last)."""
    from . import ths

    for src in ths.SOURCES:
        SOURCES[src.name] = src


_register_providers()


def source(name: str) -> Source:
    s = SOURCES.get(name)
    if not s:
        raise OntologyError(f"unknown source '{name}'; available: {sorted(SOURCES)}")
    return s


def series_key(name: str, params: dict[str, str]) -> str:
    return f"{name}:" + ",".join(f"{k}={params[k]}" for k in sorted(params))
