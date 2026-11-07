"""The data department's first worker: keeps every bound series up to date.

    python -m ontology.collector            # loop forever (the workbench server also runs this loop)
    python -m ontology.collector --once     # one pass, then exit

A pass applies the type templates, then fetches new points for each series that is in effect and due.
Collection is plain code; no model is involved.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import psycopg

from . import evidence, history, news, onchain, projects
from .db import connect
from .providers import load_env_file
from .errors import OntologyError
from .series import freq_seconds
from .sources import source
from .store import OntologyStore

log = logging.getLogger("collector")
TICK = 60  # seconds between passes; each series has its own, longer poll interval
OVERLAP = 3  # re-read the last few points: bars and daily values get revised


def due(s: dict, now: datetime) -> bool:
    if s["last_fetch_at"] is None:
        return True
    poll = source(s["source"]).poll
    if s["status"] == "error":
        poll = min(poll, timedelta(minutes=2))  # a failed series is retried soon, not at its normal interval
    return now - s["last_fetch_at"] >= poll


def collect_series(store: OntologyStore, s: dict, now: datetime) -> int:
    src = source(s["source"])
    if s["last_ts"]:
        step = timedelta(seconds=freq_seconds(s["frequency"]))
        since = s["last_ts"] - step * OVERLAP
    else:
        since = now - src.backfill_for(s["params"])
    try:
        points = src.fetch(s["params"], since)
    except OntologyError as e:
        store.series.mark(s["id"], str(e)[:500])
        log.warning("%s: %s", s["key"], e)
        return 0
    except Exception as e:  # one broken source must not stop the rest of the pass
        store.series.mark(s["id"], f"{type(e).__name__}: {e}"[:500])
        log.exception("%s: unexpected error", s["key"])
        return 0
    n = store.series.write(s["id"], points)
    store.series.mark(s["id"], None)
    if n:
        log.info("%s: %d new or revised points", s["key"], n)
    return n


def run_once(conn: psycopg.Connection) -> list[str]:
    """One pass. Returns the ids of series that gained or changed points (and new template bindings)."""
    store = OntologyStore(conn, actor="system")
    changed = [b["series_id"] for b in store.apply_templates()]
    now = datetime.now(UTC)
    for s in store.series.collectable():
        if due(s, now) and collect_series(store, s, now):
            changed.append(s["id"])
    if os.environ.get("COLLECT_ONCHAIN", "0") == "1":
        try:
            if onchain.promote_freezes(conn, now):
                changed.append("onchain")
        except Exception:
            log.exception("freeze promotion failed")
    if news_enabled():
        try:
            changed += news.run(conn)
        except Exception:  # news must never stop market data collection
            log.exception("news pass failed")
    try:
        if evidence.refresh(conn):
            changed.append("evidence")
    except Exception:
        log.exception("evidence pass failed")
    try:
        # Catches every change to the accepted graph not already committed (templates, CLI decisions…);
        # the first pass on a project records its starting state.
        if history.commit_if_changed(store, "system", "开始记录" if history.last(conn) is None else "后台记录"):
            changed.append("history")
    except Exception:
        log.exception("history pass failed")
    return list(dict.fromkeys(changed))


def refresh_now(conn: psycopg.Connection) -> dict:
    """Fetch every series and feed right away, whatever their poll interval (the workbench's refresh button).

    Fetching runs in parallel; writes stay on this connection. Feeds are refreshed separately (refresh_news_now)
    so the button does not wait on slow news sites; model extraction stays with the background loop.
    """
    started = time.monotonic()
    store = OntologyStore(conn, actor="system")
    store.apply_templates()
    now = datetime.now(UTC)
    series = store.series.collectable()

    def fetch(s: dict):
        src = source(s["source"])
        step = timedelta(seconds=freq_seconds(s["frequency"]))
        since = s["last_ts"] - step * OVERLAP if s["last_ts"] else now - src.backfill_for(s["params"])
        try:
            return s, src.fetch(s["params"], since), None
        except OntologyError as e:
            return s, [], str(e)[:500]

    points = updated = 0
    errors = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for s, got, err in pool.map(fetch, series):
            if err:
                store.series.mark(s["id"], err)
                errors.append({"series": s["label"], "error": err})
                continue
            n = store.series.write(s["id"], got)
            store.series.mark(s["id"], None)
            points += n
            updated += bool(n)
    evidence.refresh(conn, force=True)
    return {"series": len(series), "series_updated": updated, "points": points, "errors": errors,
            "seconds": round(time.monotonic() - started, 1)}


def refresh_news_now(conn: psycopg.Connection) -> int:
    """The news half of the refresh button: every feed now, then heat and sentiment. Returns new articles."""
    if not news_enabled():
        return 0
    now = datetime.now(UTC)
    articles = news.fetch_feeds(conn, now, force=True, timeout=12)
    news.screen(conn)
    news.update_signals(OntologyStore(conn, actor="system"), now)
    return articles


def news_enabled() -> bool:
    return os.environ.get("COLLECT_NEWS", "0") == "1"


def main() -> None:
    p = argparse.ArgumentParser(prog="collector")
    p.add_argument("--once", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    load_env_file()
    while True:
        with connect() as registry:
            ids = projects.active_ids(registry)
        changed = []
        for project_id in ids:
            with projects.project_connect(project_id) as conn:
                changed.extend(run_once(conn))
        if args.once:
            print(f"{len(changed)} series updated")
            return
        time.sleep(TICK)


if __name__ == "__main__":
    main()
