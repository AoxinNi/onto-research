"""Folders on this computer that the user has opened to the agent, for adding papers to a project's library.

The user names them in ONTOLOGY_LIBRARY_FOLDERS (absolute paths, separated by os.pathsep). Nothing outside them can be
listed or read: every path is resolved, symlinks included, and must land inside one of them. A signed-in cloud
workspace never gets this: there the disk belongs to the server, not to the user.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Callable

from . import projects
from .errors import OntologyError
from .library import Library, file_limit

ENV = "ONTOLOGY_LIBRARY_FOLDERS"
SUFFIXES = {".pdf", ".md", ".markdown", ".txt"}
MAX_LIST = 500
MAX_ADD = 20


def roots() -> list[Path]:
    """The allowed folders that exist, resolved."""
    out: list[Path] = []
    for part in os.environ.get(ENV, "").split(os.pathsep):
        p = Path(part.strip()).expanduser()
        if not part.strip() or not p.is_absolute():
            continue
        try:
            p = p.resolve(strict=True)
        except OSError:
            continue
        if p.is_dir() and p not in out:
            out.append(p)
    return out


def allowed_roots() -> list[Path]:
    if projects.workspace_owner.get():
        raise OntologyError("local folders are only available in the local workbench and the desktop app")
    found = roots()
    if not found:
        raise OntologyError(f"no folder on this computer is open to the agent; the user can allow one with {ENV}")
    return found


def locate(path: str, base: list[Path] | None = None) -> tuple[Path, Path]:
    """(the real path, the allowed folder holding it). Relative paths are taken from each allowed folder in turn."""
    base = base if base is not None else allowed_roots()
    raw = Path(str(path or "").strip()).expanduser()
    for candidate in [raw] if raw.is_absolute() else [r / raw for r in base]:
        try:
            real = candidate.resolve(strict=True)
        except OSError:
            continue
        for root in base:
            if real.is_relative_to(root):
                return real, root
    raise OntologyError(f"'{path}' is not in a folder the user allowed, or does not exist")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _library_files(lib: Library) -> tuple[dict[int, int], dict[str, str]]:
    """Sizes of the stored files (to hash only plausible matches) and sha256 -> item id."""
    rows = lib.conn.execute(
        """SELECT f.size, f.sha256, f.item_id FROM library_files f
           JOIN library_items i ON i.id = f.item_id WHERE i.status = 'active'""").fetchall()
    sizes: dict[int, int] = {}
    for r in rows:
        sizes[r["size"]] = sizes.get(r["size"], 0) + 1
    return sizes, {r["sha256"]: r["item_id"] for r in rows}


def list_files(lib: Library, folder: str | None = None) -> dict[str, Any]:
    """Paper files under an allowed folder (or all of them), with whether each is already in the library."""
    base = allowed_roots()
    starts = [locate(folder, base)] if folder else [(r, r) for r in base]
    sizes, known = _library_files(lib)
    files: list[dict[str, Any]] = []
    truncated = False
    for start, root in starts:
        if not start.is_dir():
            raise OntologyError(f"'{folder}' is a file, not a folder")
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if name.startswith(".") or path.suffix.lower() not in SUFFIXES or path.is_symlink():
                    continue
                if len(files) >= MAX_LIST:
                    truncated = True
                    break
                size = path.stat().st_size
                item = known.get(_sha256(path)) if size in sizes else None
                files.append({"path": str(path), "folder": str(path.parent.relative_to(root)), "name": name,
                              "size": size, "in_library": item is not None, "item_id": item})
            if truncated:
                break
    return {"folders": [str(r) for r in base], "files": files, "truncated": truncated}


def add_files(lib: Library, paths: list[str], tags: list[str] | None,
              lookup: Callable[[str], dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Add each file as a new source (a PDF's printed DOI fills in its metadata). A file already in the library is
    reported, not added twice. One file failing does not stop the others."""
    if not paths:
        raise OntologyError("give at least one path")
    if len(paths) > MAX_ADD:
        raise OntologyError(f"at most {MAX_ADD} files per call; add the rest in another call")
    base = allowed_roots()
    out = []
    for given in paths:
        try:
            out.append({"path": given, **_add_one(lib, given, base, tags, lookup)})
        except OntologyError as e:
            out.append({"path": given, "ok": False, "error": str(e)})
    return out


def _add_one(lib: Library, given: str, base: list[Path], tags: list[str] | None,
             lookup: Callable[[str], dict[str, Any]] | None) -> dict[str, Any]:
    path, _ = locate(given, base)
    if not path.is_file():
        raise OntologyError("not a file")
    if path.suffix.lower() not in SUFFIXES:
        raise OntologyError("only PDF, Markdown and text files can be added")
    if path.stat().st_size > file_limit():
        raise OntologyError(f"the file is larger than {file_limit() // (1024 * 1024)} MB")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if same := lib.conn.execute(
            """SELECT f.item_id FROM library_files f JOIN library_items i ON i.id = f.item_id
               WHERE f.sha256 = %s AND i.status = 'active' LIMIT 1""", (digest,)).fetchone():
        return {"ok": True, "already_in_library": True, "item": _brief(lib.get_item(same["item_id"]))}
    with lib.conn.transaction():
        added = lib.add_upload(path.name, data, None, lookup)
        item = added["item"]
        if tags:
            item = lib.update_item(item["id"], tags=list(item.get("tags") or []) + list(tags))
    return {"ok": True, "already_in_library": False, "item": _brief(item), "detected_doi": added["detected_doi"],
            "needs_metadata": added["needs_metadata"], "pages": added["file"].get("pages"),
            "text": added["file"].get("extract_status"), "text_error": added["file"].get("extract_error")}


def _brief(item: dict[str, Any]) -> dict[str, Any]:
    return {k: item.get(k) for k in ("id", "citekey", "title", "authors", "year", "doi", "tags")}
