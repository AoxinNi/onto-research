"""The embedded Postgres: a private server for the desktop app and local runs, so nothing needs Docker or a
database someone set up.

When DATABASE_URL is not set, `ontology.db.database_url()` asks this module. It starts Postgres from the binaries
in the `pgserver` wheel (PostgreSQL 16, about 33 MB, no extensions needed), with its data in the app's data folder:

    macOS    ~/Library/Application Support/onto-research/pg
    Linux    $XDG_DATA_HOME/onto-research/pg (~/.local/share/…)
    Windows  %APPDATA%/onto-research/pg

ONTO_DATA_DIR moves the whole folder. ONTO_LOCAL_DB=0 turns this off (then DATABASE_URL is required, as before);
ONTO_LOCAL_DB=1 insists on it, so a missing pgserver is reported as such rather than as a missing DATABASE_URL.

The server listens on a Unix socket in its data folder (a TCP port on Windows), not on the network. Every process
that uses it holds a handle; the last one to exit stops it (pgserver's cleanup_mode="stop"), so the workbench and
an MCP server started by a client can share one database.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

APP = "onto-research"
DATABASE = "onto_research"

log = logging.getLogger("ontology.localdb")
_lock = threading.Lock()
_server = None
_url: str | None = None


def data_dir() -> Path:
    """Where the app keeps its data: the database, project snapshots, logs."""
    if custom := os.environ.get("ONTO_DATA_DIR", "").strip():
        return Path(custom).expanduser()
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming") / APP
    return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / APP


def available() -> bool:
    try:
        import pgserver  # noqa: F401
    except ImportError:
        return False
    return True


def wanted() -> bool:
    """Whether a missing DATABASE_URL should start the embedded server."""
    mode = os.environ.get("ONTO_LOCAL_DB", "auto").strip().lower()
    if mode in ("0", "false", "no", "off"):
        return False
    return mode in ("1", "true", "yes", "on") or available()


def url() -> str:
    """Start the embedded server if it is not running, make sure the app's database exists, and return its
    connection string. Safe to call from several threads; the server starts once per process."""
    global _server, _url
    with _lock:
        if _url:
            return _url
        try:
            import pgserver
        except ImportError as e:
            raise RuntimeError("没有内置数据库：请安装 onto-research[local]（pgserver），或设置 DATABASE_URL。") from e
        pgdata = data_dir() / "pg"
        pgdata.mkdir(parents=True, exist_ok=True)
        _server = pgserver.get_server(pgdata, cleanup_mode="stop")
        _prune_handles()
        info = _server.get_postmaster_info()
        where = {"host": str(info.socket_dir)} if info.socket_dir else {"host": info.hostname or "127.0.0.1"}
        if info.port:
            where["port"] = str(info.port)
        admin = make_conninfo("", user="postgres", dbname="postgres", **where)
        with psycopg.connect(admin, autocommit=True) as c:
            if not c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DATABASE,)).fetchone():
                c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DATABASE)))
        _url = make_conninfo("", user="postgres", dbname=DATABASE, **where)
        log.info("embedded Postgres in %s", pgdata)
        return _url


def _alive(pid: int) -> bool:
    try:
        import psutil  # comes with pgserver

        return psutil.pid_exists(pid)
    except ImportError:
        pass
    if sys.platform == "win32":
        return True  # cannot tell without psutil; keep it, as pgserver would
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _prune_handles() -> None:
    """Drop processes that died without giving up their handle (killed, crashed), so they do not keep the server
    running for good: pgserver stops Postgres only when the last listed process lets go."""
    if _server is None:
        return
    try:
        handles = _server.global_process_id_list
        with _server._lock:
            live = [pid for pid in handles.get() if _alive(pid)]
            handles.put(live)
    except Exception:  # noqa: BLE001 — pgserver internals; at worst the server keeps running, as before
        log.exception("could not tidy the embedded Postgres's process list")


def stop() -> None:
    """Give up this process's handle; the last process to do so stops the server. pgserver also does this at exit,
    but a process ended by a signal skips exit handlers (uvicorn re-raises SIGTERM after shutting down), which
    would leave Postgres running. Safe to call more than once."""
    global _server, _url
    with _lock:
        if _server is not None:
            _prune_handles()
            try:
                _server.cleanup()
            except Exception:  # noqa: BLE001 — never block a shutdown on the database
                log.exception("could not stop the embedded Postgres")
        _server, _url = None, None


def pg_bin(name: str) -> str | None:
    """A Postgres client program (pg_dump, psql) from the embedded install, when it is there."""
    try:
        from pgserver._commands import POSTGRES_BIN_PATH
    except ImportError:
        return None
    path = Path(POSTGRES_BIN_PATH) / (name + (".exe" if sys.platform == "win32" else ""))
    return str(path) if path.exists() else None
