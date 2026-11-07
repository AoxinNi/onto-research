"""Evidence for links: does the data back up what the graph claims?

Two kinds are computed for every active link, about once an hour:
- series  when both ends carry data, how their changes co-move: correlation at lags, on a common frequency,
          with a rough significance test. A link "A drives B" is supported when A's changes lead or match B's.
- news    articles that mention both ends, with how often and how they read.
Agents can add a third kind, a note, citing articles or a series window they looked at.

This is association, not causation; summaries say so in their own words ("同步"、"领先"), never "导致".
"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime, timedelta
from statistics import NormalDist
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .errors import OntologyError
from .series import freq_seconds

REFRESH = timedelta(hours=1)
MIN_POINTS = 20
# For each common frequency: how far back to look, and how many buckets of lead/lag to try.
BUCKETS = {
    "hour": (timedelta(days=30), 24, 3600),
    "day": (timedelta(days=365), 7, 86400),
    "week": (timedelta(days=365 * 3), 4, 604800),
    "month": (timedelta(days=365 * 6), 3, 2_629_800),
}
BUCKET_NAMES = {"hour": "小时", "day": "日度", "week": "周度", "month": "月度"}
LAG_UNITS = {"hour": "小时", "day": "天", "week": "周", "month": "个月"}
CAUSAL = {"drives", "affects", "sets"}  # links that claim one end moves the other; for these, who leads matters
MIN_MOVES = 8                            # a step series (a policy rate) needs this many actual changes to say anything
ROLE_ORDER = ["price_daily", "price", "value", "flow", "supply", "volume", "funding", "open_interest", "sentiment", "heat"]


def bucket_for(*freqs: str) -> str:
    worst = max(freq_seconds(f) for f in freqs)
    return "hour" if worst <= 3600 else "day" if worst <= 86400 else "week" if worst <= 604800 else "month"


def _key(ts: datetime, bucket: str) -> datetime:
    ts = ts.astimezone(UTC)
    if bucket == "hour":
        return ts.replace(minute=0, second=0, microsecond=0)
    if bucket == "day":
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket == "week":
        d = ts.replace(hour=0, minute=0, second=0, microsecond=0)
        return d - timedelta(days=d.weekday())
    return ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def resample(points: list[tuple[datetime, float]], bucket: str) -> dict[datetime, float]:
    """Last value in each bucket."""
    out: dict[datetime, float] = {}
    for ts, v in points:
        out[_key(ts, bucket)] = v
    return out


def changes(levels: dict[datetime, float], log: bool) -> dict[datetime, float]:
    keys = sorted(levels)
    out = {}
    for prev, cur in zip(keys, keys[1:]):
        a, b = levels[prev], levels[cur]
        if log:
            if a > 0 and b > 0:
                out[cur] = math.log(b / a)
        else:
            out[cur] = b - a
    return out


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy)


def lagged(a: dict[datetime, float], b: dict[datetime, float], step: int, max_lag: int) -> dict[int, tuple[float, int]]:
    """corr(a_t, b_{t+k}) for k in [-max_lag, max_lag]; k > 0 means a moves first."""
    out = {}
    for k in range(-max_lag, max_lag + 1):
        pairs = [(a[t], b[t + timedelta(seconds=k * step)]) for t in a if t + timedelta(seconds=k * step) in b]
        r = pearson([p[0] for p in pairs], [p[1] for p in pairs])
        if r is not None:
            out[k] = (r, len(pairs))
    return out


def autocorr(x: dict[datetime, float], step: int, k: int) -> float:
    pairs = [(x[t], x[t + timedelta(seconds=k * step)]) for t in x if t + timedelta(seconds=k * step) in x]
    return pearson([p[0] for p in pairs], [p[1] for p in pairs]) or 0.0


def effective_n(n: int, a: dict[datetime, float], b: dict[datetime, float], step: int) -> int:
    """Bartlett: autocorrelated series carry less information than their length suggests."""
    inflation = 1 + 2 * sum(autocorr(a, step, k) * autocorr(b, step, k) for k in (1, 2, 3))
    return max(3, int(n / max(1.0, inflation)))


def significant(r: float, n: int, tests: int = 1) -> bool:
    """Two-sided p < 0.05 after a Bonferroni correction for the number of lags tried."""
    if n < 4 or abs(r) >= 1:
        return abs(r) >= 1
    z = NormalDist().inv_cdf(1 - 0.05 / (2 * tests))
    return abs(r) * math.sqrt(n - 2) / math.sqrt(1 - r * r) > z


def compare(a: dict[str, Any], b: dict[str, Any], a_points: list[tuple[datetime, float]],
            b_points: list[tuple[datetime, float]], a_name: str, b_name: str, causal: bool = True) -> dict[str, Any]:
    """Co-movement of two series (dicts with label, frequency, log flag). Returns verdict, strength, summary, stat."""
    bucket = bucket_for(a["frequency"], b["frequency"])
    _, max_lag, step = BUCKETS[bucket]
    da = changes(resample(a_points, bucket), a["log"])
    db = changes(resample(b_points, bucket), b["log"])
    table = lagged(da, db, step, max_lag)
    stat: dict[str, Any] = {"bucket": bucket, "a": a["label"], "b": b["label"], "a_series": a["id"], "b_series": b["id"],
                            "lags": {str(k): round(r, 3) for k, (r, _) in table.items()}}
    n0 = table.get(0, (0.0, 0))[1]
    if not table or n0 < MIN_POINTS:
        overlap = max((n for _, n in table.values()), default=0)
        return {"verdict": "insufficient", "strength": 0.0, "stat": stat | {"n": overlap},
                "summary": f"数据不足：{BUCKET_NAMES[bucket]}重叠 {overlap} 个点，至少需要 {MIN_POINTS} 个"}
    moves = min(sum(1 for v in da.values() if v), sum(1 for v in db.values() if v))
    if moves < MIN_MOVES:
        return {"verdict": "insufficient", "strength": 0.0, "stat": stat | {"n": n0, "moves": moves},
                "summary": f"数据不足：一端在窗口内只变动了 {moves} 次，无法判断联动"}
    best_k, (best_r, best_n) = max(table.items(), key=lambda kv: (abs(kv[1][0]), -abs(kv[0])))
    r0 = table[0][0]
    stat |= {"n": best_n, "r0": round(r0, 3), "best_r": round(best_r, 3), "best_lag": best_k}
    n_eff = effective_n(best_n, da, db, step)
    stat["n_eff"] = n_eff
    sig = significant(best_r, n_eff, tests=len(table))
    sign = "同向" if best_r > 0 else "反向"
    unit = LAG_UNITS[bucket]
    when = "同步" if best_k == 0 else (f"{a_name} 领先 {best_k} {unit}" if best_k > 0 else f"{b_name} 领先 {-best_k} {unit}")
    head = f"{BUCKET_NAMES[bucket]}变化{sign}相关 {best_r:+.2f}，{when}（n={best_n}"
    weight = min(1.0, best_n / 60)
    if not sig:
        return {"verdict": "weak", "strength": round(0.3 * abs(best_r) * weight, 3), "stat": stat,
                "summary": f"{head}，不显著）"}
    verdict = "reverse" if causal and best_k < 0 else "supports"
    return {"verdict": verdict, "strength": round(abs(best_r) * weight, 3), "stat": stat,
            "summary": head + ("，显著）" if verdict == "supports" else "，显著；方向与关系相反）")}


def _node_series(conn: psycopg.Connection) -> dict[str, list[dict[str, Any]]]:
    rows = conn.execute(
        """SELECT b.node_id, b.role, s.id, s.label, s.unit, s.frequency, s.source,
                  (SELECT min(value) FROM observations o WHERE o.series_id = s.id) AS minv
           FROM bindings b JOIN series s ON s.id = b.series_id LEFT JOIN changesets c ON c.id = b.changeset_id
           WHERE b.status = 'active' AND (b.changeset_id IS NULL OR c.status = 'accepted') AND s.last_ts IS NOT NULL"""
    ).fetchall()
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        # Levels that are always positive and not rates compare as log returns; rates and scores as differences.
        log = (r["minv"] or 0) > 0 and r["unit"] != "%" and r["source"] not in ("news_sentiment", "binance_funding")
        out.setdefault(r["node_id"], []).append({**r, "log": log})
    for v in out.values():
        v.sort(key=lambda s: ROLE_ORDER.index(s["role"]) if s["role"] in ROLE_ORDER else 99)
    return out


def _points(conn: psycopg.Connection, series_id: str, since: datetime) -> list[tuple[datetime, float]]:
    return [(r["ts"], r["value"]) for r in conn.execute(
        "SELECT ts, value FROM observations WHERE series_id = %s AND ts >= %s ORDER BY ts", (series_id, since))]


def series_evidence(conn: psycopg.Connection, link: dict[str, Any], node_series: dict[str, list[dict[str, Any]]],
                    now: datetime) -> dict[str, Any] | None:
    a_list, b_list = node_series.get(link["from_id"]), node_series.get(link["to_id"])
    if not a_list or not b_list:
        return None
    # Pick the pair by how much data overlaps (then by role order), never by which one looks best:
    # trying pairs until one correlates would be cherry-picking.
    best = None
    for a in a_list[:3]:
        for b in b_list[:3]:
            if a["id"] == b["id"]:
                continue
            look = BUCKETS[bucket_for(a["frequency"], b["frequency"])][0]
            res = compare(a, b, _points(conn, a["id"], now - look), _points(conn, b["id"], now - look),
                          link["from_name"], link["to_name"], causal=link["type"] in CAUSAL)
            res["ref"] = f"{a['id']}~{b['id']}"
            rank = (res["verdict"] != "insufficient", res["stat"].get("n", 0))
            if best is None or rank > best[0]:
                best = (rank, res)
    return best[1] if best else None


def news_evidence(conn: psycopg.Connection, link: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    rows = conn.execute(
        """SELECT d.id, d.title, d.url, d.feed, d.published_at, a.sentiment AS sa, b.sentiment AS sb,
                  least(a.relevance, b.relevance) AS rel
           FROM mentions a JOIN mentions b ON b.document_id = a.document_id JOIN documents d ON d.id = a.document_id
           WHERE a.node_id = %s AND b.node_id = %s AND d.published_at > %s ORDER BY d.published_at DESC""",
        (link["from_id"], link["to_id"], now - timedelta(days=30)),
    ).fetchall()
    if not rows:
        return None
    n = len(rows)
    feeds = len({r["feed"] for r in rows})
    agree = sum(1 for r in rows if (r["sa"] >= 0) == (r["sb"] >= 0)) / n
    strength = round(min(1.0, (n / 6) * (0.5 + 0.5 * min(1.0, feeds / 3))) * (sum(r["rel"] for r in rows) / n), 3)
    return {
        "ref": "co-mentions", "verdict": "supports" if n >= 2 else "weak", "strength": strength,
        "summary": f"近 30 天 {n} 篇报道同时提到两端（{feeds} 个来源）",
        "stat": {"count": n, "feeds": feeds, "agreement": round(agree, 2),
                 "docs": [{"id": r["id"], "title": r["title"], "url": r["url"], "feed": r["feed"],
                           "published_at": r["published_at"].isoformat()} for r in rows[:5]]},
    }


def _save(conn: psycopg.Connection, link_id: str, kind: str, ev: dict[str, Any], author: str) -> None:
    conn.execute(
        """INSERT INTO link_evidence (link_id, kind, ref, verdict, strength, summary, stat, author, computed_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
           ON CONFLICT (link_id, kind, ref) DO UPDATE SET verdict = EXCLUDED.verdict, strength = EXCLUDED.strength,
               summary = EXCLUDED.summary, stat = EXCLUDED.stat, author = EXCLUDED.author, computed_at = now()""",
        (link_id, kind, ev["ref"], ev["verdict"], ev["strength"], ev["summary"], Jsonb(ev["stat"]), author),
    )


def refresh(conn: psycopg.Connection, now: datetime | None = None, force: bool = False) -> int:
    """Recompute series and news evidence for every active link (at most once per REFRESH). Returns links updated."""
    now = now or datetime.now(UTC)
    last = conn.execute("SELECT max(computed_at) AS t FROM link_evidence WHERE author = 'system'").fetchone()["t"]
    if not force and last and now - last < REFRESH:
        return 0
    links = conn.execute(
        """SELECT l.id, l.type, l.from_id, l.to_id, f.name AS from_name, t.name AS to_name
           FROM links l JOIN objects f ON f.id = l.from_id JOIN objects t ON t.id = l.to_id WHERE l.status = 'active'"""
    ).fetchall()
    node_series = _node_series(conn)
    touched = 0
    with conn.transaction():
        for link in links:
            found = False
            for kind, ev in (("series", series_evidence(conn, link, node_series, now)), ("news", news_evidence(conn, link, now))):
                if ev:
                    conn.execute("DELETE FROM link_evidence WHERE link_id = %s AND kind = %s AND ref <> %s AND author = 'system'",
                                 (link["id"], kind, ev["ref"]))
                    _save(conn, link["id"], kind, ev, "system")
                    found = True
                else:
                    conn.execute("DELETE FROM link_evidence WHERE link_id = %s AND kind = %s AND author = 'system'",
                                 (link["id"], kind))
            touched += found
    return touched


def add_note(conn: psycopg.Connection, link_id: str, summary: str, verdict: str, strength: float,
             refs: list[str] | None, author: str) -> dict[str, Any]:
    """An agent's own evidence for (or against) a link, citing what it looked at."""
    if verdict not in ("supports", "contradicts", "weak"):
        raise OntologyError("verdict must be supports, contradicts or weak")
    if not 0 <= strength <= 1:
        raise OntologyError("strength must be between 0 and 1")
    if not summary.strip():
        raise OntologyError("summary is required: say what the evidence shows")
    if not conn.execute("SELECT 1 FROM links WHERE id = %s AND status = 'active'", (link_id,)).fetchone():
        raise OntologyError(f"no active link '{link_id}'")
    ref = f"note:{author}:{hashlib.sha1(summary.strip().encode()).hexdigest()[:10]}"
    _save(conn, link_id, "note", {"ref": ref, "verdict": verdict, "strength": strength, "summary": summary.strip(),
                                  "stat": {"refs": refs or []}}, author)
    return link_evidence(conn, link_id)


def link_evidence(conn: psycopg.Connection, link_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT kind, ref, verdict, strength, summary, stat, author, computed_at FROM link_evidence WHERE link_id = %s "
        "ORDER BY strength DESC", (link_id,),
    ).fetchall()
    items = [{**r, "computed_at": r["computed_at"].isoformat()} for r in rows]
    return {"link_id": link_id, "strength": score(items), "evidence": items}


def score(items: list[dict[str, Any]]) -> float:
    """Combined support: independent pieces add up (1 - Π(1 - s)); contradicting or reversed evidence counts against."""
    keep = 1.0
    against = 0.0
    for e in items:
        if e["verdict"] in ("supports", "weak"):
            keep *= 1 - e["strength"]
        elif e["verdict"] in ("contradicts", "reverse"):
            against = max(against, e["strength"])
    return round(max(0.0, (1 - keep) - against / 2), 3)


def summaries(conn: psycopg.Connection) -> dict[str, dict[str, Any]]:
    """Per link, for the canvas: combined strength and one line per kind."""
    out: dict[str, dict[str, Any]] = {}
    for r in conn.execute("SELECT link_id, kind, verdict, strength, summary FROM link_evidence ORDER BY strength DESC"):
        e = out.setdefault(r["link_id"], {"items": []})
        e["items"].append({"kind": r["kind"], "verdict": r["verdict"], "strength": r["strength"], "summary": r["summary"]})
    for e in out.values():
        e["strength"] = score(e["items"])
    return out
