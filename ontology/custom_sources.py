"""Data sources the user adds, edits and removes on the 数据源 page, without writing code.

Three kinds, stored in onto_research.custom_sources and shared by every project on this workbench:

- rss:  a news feed. It joins the built-in feeds of the news desk (ontology.news).
- json: an HTTP endpoint answering JSON. `path` leads to the list of points; `time` and `value` name the fields of
        each point (dot paths, or 0 / 1 when a point is a [time, value] pair).
- csv:  an HTTP endpoint answering CSV with a header row; `time` and `value` name the columns.

A json/csv URL may hold {param} placeholders, e.g. https://example.org/api/{symbol}.json: each one becomes a param
that a binding fills in, so one source serves many nodes. sync() mirrors the table into sources.SOURCES and
news.CUSTOM_FEEDS, which is where the collector, the binding code and the agent look.

Only the local workbench and the desktop app manage these: the server fetches the URLs, so on a shared cloud
server they would let one user make the server request any address.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import string
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlsplit

import psycopg
from psycopg.types.json import Jsonb

from . import news, sources
from .errors import OntologyError
from .sources import FREQ_SECONDS, Obs, Source

log = logging.getLogger("ontology.custom_sources")

KINDS = ("rss", "json", "csv")
PROVIDER = "custom"
NAME = re.compile(r"[a-z][a-z0-9_]{1,39}")
PARAM = re.compile(r"[a-z][a-z0-9_]{0,29}")
TIME_FORMATS = ("auto", "iso", "unix_s", "unix_ms")
LANGS = ("zh", "en")
MAX_POINTS = 50_000

_loaded: set[str] = set()  # names this module put into sources.SOURCES, so a removed one can be taken out again


# ------------------------------------------------------------------ validation


def _text(value: Any, what: str, limit: int, required: bool = True) -> str:
    out = " ".join(str(value or "").split())
    if required and not out:
        raise OntologyError(f"{what}不能为空")
    if len(out) > limit:
        raise OntologyError(f"{what}最多 {limit} 个字符")
    return out


def url_params(url: str) -> list[str]:
    """The {param} placeholders of a URL, in order."""
    try:
        fields = [f for _, f, _, _ in string.Formatter().parse(url) if f is not None]
    except ValueError:
        raise OntologyError("地址里的花括号不成对；参数写成 {symbol} 这样") from None
    out: list[str] = []
    for f in fields:
        if not PARAM.fullmatch(f):
            raise OntologyError(f"参数名「{f}」只能用小写字母、数字和下划线，并以字母开头")
        if f not in out:
            out.append(f)
    return out


def _int(value: Any, what: str, low: int, high: int, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise OntologyError(f"{what}应为整数") from None
    if not low <= n <= high:
        raise OntologyError(f"{what}应在 {low} 到 {high} 之间")
    return n


def clean(defn: dict[str, Any], existing: str | None = None) -> dict[str, Any]:
    """A checked, normalized definition: {name, kind, title, url, config}. existing: the name being edited."""
    kind = str(defn.get("kind") or "").strip()
    if kind not in KINDS:
        raise OntologyError(f"类型应为 {' / '.join(KINDS)}")
    name = existing or str(defn.get("name") or "").strip()
    if not NAME.fullmatch(name):
        raise OntologyError("标识只能用小写字母、数字和下划线，以字母开头，2 到 40 个字符")
    if not existing and (name in sources.SOURCES and name not in _loaded
                         or any(n == name for n, _, _ in news.FEEDS)):
        raise OntologyError(f"「{name}」已被内置数据源使用，请换一个标识")
    title = _text(defn.get("title"), "名称", 80)
    url = _text(defn.get("url"), "地址", 1000)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.username is not None:
        raise OntologyError("地址应以 http:// 或 https:// 开头，且不带用户名密码")
    cfg = defn.get("config") or {}
    if not isinstance(cfg, dict):
        raise OntologyError("config 应为对象")
    if kind == "rss":
        if url_params(url):
            raise OntologyError("RSS 地址不能带 {参数}")
        lang = str(cfg.get("lang") or "zh")
        if lang not in LANGS:
            raise OntologyError(f"语言应为 {' / '.join(LANGS)}")
        return {"name": name, "kind": kind, "title": title, "url": url, "config": {"lang": lang}}

    params = url_params(url)
    helps = cfg.get("params") or {}
    out = {
        "path": _text(cfg.get("path"), "数据所在路径", 200, required=False) if kind == "json" else "",
        "time": _text(cfg.get("time"), "时间字段", 100),
        "value": _text(cfg.get("value"), "数值字段", 100),
        "time_format": str(cfg.get("time_format") or "auto"),
        "frequency": str(cfg.get("frequency") or "1d"),
        "poll_minutes": _int(cfg.get("poll_minutes"), "采集间隔（分钟）", 5, 10080, 60),
        "backfill_days": _int(cfg.get("backfill_days"), "首次回补（天）", 1, 36500, 365),
        "unit": _text(cfg.get("unit"), "单位", 20, required=False) or None,
        "params": {p: _text(helps.get(p) if isinstance(helps, dict) else "", f"参数 {p} 的说明", 80,
                            required=False) or p for p in params},
    }
    if out["time_format"] not in TIME_FORMATS:
        raise OntologyError(f"时间格式应为 {' / '.join(TIME_FORMATS)}")
    if out["frequency"] not in FREQ_SECONDS:
        raise OntologyError(f"频率应为 {' / '.join(FREQ_SECONDS)}")
    return {"name": name, "kind": kind, "title": title, "url": url, "config": out}


# ------------------------------------------------------------------ reading responses


def _parse_time(value: Any, fmt: str) -> datetime:
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError("no time")
    if fmt in ("unix_s", "unix_ms") or (fmt == "auto" and isinstance(value, (int, float))):
        n = float(value)
        if fmt == "unix_ms" or (fmt == "auto" and abs(n) > 1e11):
            n /= 1000
        return datetime.fromtimestamp(n, tz=UTC)
    text = str(value).strip()
    if fmt == "auto" and re.fullmatch(r"-?\d+(\.\d+)?", text):
        return _parse_time(float(text), "auto")
    dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("/", "-"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _value(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError("no value")
    if isinstance(value, str):
        value = value.replace(",", "").replace("%", "").strip()
    return float(value)


def _dig(data: Any, path: str) -> Any:
    for part in [p for p in path.split(".") if p] if path else []:
        if isinstance(data, list) and re.fullmatch(r"-?\d+", part):
            data = data[int(part)]
        elif isinstance(data, dict):
            data = data[part]
        else:
            raise KeyError(part)
    return data


def _points(rows: list[tuple[Any, Any]], fmt: str) -> list[Obs]:
    out, bad = [], 0
    for t, v in rows:
        try:
            out.append(Obs(_parse_time(t, fmt), _value(v)))
        except (ValueError, TypeError, OverflowError, OSError):
            bad += 1  # a gap or a footnote row; one bad row must not lose the rest
    if rows and not out:
        raise OntologyError(f"{len(rows)} 行里没有一行能读出时间和数值；检查时间、数值字段和时间格式")
    out.sort(key=lambda o: o.ts)
    return out[-MAX_POINTS:]


def parse_json(text: str, cfg: dict[str, Any]) -> list[Obs]:
    try:
        data = json.loads(text)
    except ValueError:
        raise OntologyError(f"返回的不是 JSON：{text.strip()[:80]!r}") from None
    try:
        items = _dig(data, cfg.get("path") or "")
    except (KeyError, IndexError, TypeError):
        raise OntologyError(f"JSON 里找不到路径「{cfg.get('path')}」") from None
    if isinstance(items, dict):  # {time: value, ...}
        items = [[k, v] for k, v in items.items()]
    if not isinstance(items, list):
        raise OntologyError(f"路径「{cfg.get('path') or '（根）'}」指向的不是列表")
    rows = []
    for item in items:
        try:
            rows.append((_dig(item, cfg["time"]), _dig(item, cfg["value"])))
        except (KeyError, IndexError, TypeError):
            rows.append((None, None))
    return _points(rows, cfg.get("time_format") or "auto")


def parse_csv(text: str, cfg: dict[str, Any]) -> list[Obs]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    header = reader.fieldnames or []
    for col in (cfg["time"], cfg["value"]):
        if col not in header:
            raise OntologyError(f"CSV 没有列「{col}」；它的列是：{', '.join(header[:12]) or '（没有表头）'}")
    return _points([(r.get(cfg["time"]), r.get(cfg["value"])) for r in reader], cfg.get("time_format") or "auto")


# ------------------------------------------------------------------ the source


@dataclass
class HttpSeries(Source):
    """A json or csv source defined on the 数据源 page."""

    kind: str = "json"
    url: str = ""
    config: dict[str, Any] = field(default_factory=dict)

    def label(self, p):
        return f"{self.title} · {' '.join(p.values())}" if p else self.title

    def frequency(self, p):
        return self.config.get("frequency") or "1d"

    def address(self, p: dict[str, str]) -> str:
        return self.url.format(**{k: quote(v, safe="") for k, v in p.items()})

    def fetch(self, p, since):
        r = sources._get(self.address(p), provider=PROVIDER)
        parse = parse_json if self.kind == "json" else parse_csv
        return [o for o in parse(r.text, self.config) if o.ts >= since]

    def describe(self):
        return super().describe() | {"custom": True, "kind": self.kind}


def build(row: dict[str, Any]) -> HttpSeries:
    cfg = row["config"]
    return HttpSeries(row["name"], row["title"], dict(cfg.get("params") or {}), cfg.get("unit"),
                      timedelta(minutes=cfg.get("poll_minutes") or 60), timedelta(days=cfg.get("backfill_days") or 365),
                      provider=PROVIDER, kind=row["kind"], url=row["url"], config=cfg)


# ------------------------------------------------------------------ storage


def _out(r: dict[str, Any]) -> dict[str, Any]:
    out = {k: r[k] for k in ("name", "kind", "title", "url", "config")}
    out["params"] = list((r["config"].get("params") or {}).keys())
    for k in ("created_at", "updated_at"):
        out[k] = r[k].isoformat() if r.get(k) else None
    return out


def list_all(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return [_out(r) for r in conn.execute("SELECT * FROM onto_research.custom_sources ORDER BY created_at, name")]


def get(conn: psycopg.Connection, name: str) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM onto_research.custom_sources WHERE name = %s", (name,)).fetchone()
    if not r:
        raise OntologyError(f"no custom source '{name}'")
    return _out(r)


def create(conn: psycopg.Connection, defn: dict[str, Any]) -> dict[str, Any]:
    d = clean(defn)
    row = conn.execute(
        """INSERT INTO onto_research.custom_sources (name, kind, title, url, config) VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (name) DO NOTHING RETURNING *""",
        (d["name"], d["kind"], d["title"], d["url"], Jsonb(d["config"]))).fetchone()
    if not row:
        raise OntologyError(f"已经有标识为「{d['name']}」的数据源")
    sync(conn)
    return _out(row)


def update(conn: psycopg.Connection, name: str, defn: dict[str, Any]) -> dict[str, Any]:
    before = get(conn, name)
    d = clean({**defn, "kind": before["kind"]}, existing=name)  # the kind and the name stay: bindings rely on them
    if d["kind"] != "rss" and set(d["config"]["params"]) != set(before["config"].get("params") or {}):
        raise OntologyError("地址里的 {参数} 不能增删：已有的绑定靠它们取数。需要不同参数时，新建一个数据源")
    row = conn.execute(
        """UPDATE onto_research.custom_sources SET title = %s, url = %s, config = %s, updated_at = now()
           WHERE name = %s RETURNING *""", (d["title"], d["url"], Jsonb(d["config"]), name)).fetchone()
    sync(conn)
    return _out(row)


def delete(conn: psycopg.Connection, name: str) -> None:
    get(conn, name)
    conn.execute("DELETE FROM onto_research.custom_sources WHERE name = %s", (name,))
    sync(conn)


def sync(conn: psycopg.Connection) -> None:
    """Make sources.SOURCES and news.CUSTOM_FEEDS match the table."""
    rows = conn.execute("SELECT * FROM onto_research.custom_sources ORDER BY name").fetchall()
    series = {r["name"]: build(r) for r in rows if r["kind"] != "rss"}
    for name in _loaded - set(series):
        sources.SOURCES.pop(name, None)
    for name, src in series.items():
        if name not in sources.SOURCES or name in _loaded:  # never shadow a built-in
            sources.SOURCES[name] = src
    _loaded.clear()
    _loaded.update(n for n in series if sources.SOURCES.get(n) is series[n])
    builtin = {n for n, _, _ in news.FEEDS}
    news.CUSTOM_FEEDS[:] = [(r["name"], r["url"], r["config"].get("lang") or "zh")
                            for r in rows if r["kind"] == "rss" and r["name"] not in builtin]


def sync_quietly(conn: psycopg.Connection) -> None:
    try:
        sync(conn)
    except psycopg.Error:  # a database without the table yet: nothing custom to load
        log.warning("custom sources not loaded", exc_info=True)


# ------------------------------------------------------------------ trying one out


def test(defn: dict[str, Any], params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fetch a definition (saved or not) once and show what came back, without storing anything."""
    name = str(defn.get("name") or "")
    d = clean(defn, existing=name if NAME.fullmatch(name) else "draft_source")  # a draft needs no unique name yet
    if d["kind"] == "rss":
        r = sources._get(d["url"], provider=PROVIDER)
        try:
            docs = news.parse_feed(r.text)
        except ET.ParseError as e:
            raise OntologyError(f"不是可读的 RSS / Atom：{e}") from None
        if not docs:
            raise OntologyError("读到了 feed，但里面没有带链接、标题和日期的文章")
        return {"kind": "rss", "count": len(docs),
                "samples": [{"title": x["title"], "published_at": x["published_at"].isoformat()} for x in docs[:5]]}
    src = build({**d, "config": d["config"]})
    p = src.clean({k: v for k, v in (params or {}).items() if k in src.params})
    since = datetime.now(UTC) - src.backfill
    points = src.fetch(p, since)
    if not points:
        raise OntologyError(f"读到了数据，但最近 {d['config']['backfill_days']} 天里没有数据点")
    return {"kind": d["kind"], "address": src.address(p), "count": len(points), "frequency": src.frequency(p),
            "first": {"ts": points[0].ts.isoformat(), "value": points[0].value},
            "last": {"ts": points[-1].ts.isoformat(), "value": points[-1].value},
            "samples": [{"ts": o.ts.isoformat(), "value": o.value} for o in points[-5:]]}
