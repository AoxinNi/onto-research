"""Project snapshots: the project written out as plain files in a folder, optionally committed with git.

The database stays the working copy (every page, the agent and the checks run on it); the folder is what a person
can read, back up, diff and keep without the app:

    <folder>/
      project.onto.json        everything the app can import again (projects.export: graph, library, papers)
      references.bib           the library as BibTeX
      references.json          the library as CSL-JSON
      manuscripts/<title>.md   each paper as Pandoc Markdown with a YAML header (bibliography: ../references.bib)
      notes/<title>.md         each note
      snapshot.json            when it was written, what is in it, the commit

Attached files (PDFs) are not copied; they stay in the database. A snapshot rewrites the folder's files and removes
manuscripts and notes that are gone; files a person adds elsewhere in the folder are left alone.

Where: ONTO_PROJECTS_DIR/<project id>, by default the app's data folder (localdb.data_dir()/projects/<id>). The
folder never comes from a request. Snapshots are on for local runs (the embedded database, or
WORKBENCH_SNAPSHOTS=1) and off on a shared server, where the server's disk is nobody's project folder.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from . import bibio, export as exporter, localdb, projects
from .errors import OntologyError
from .library import Library
from .manuscripts import Manuscripts

INFO = "snapshot.json"


def enabled() -> bool:
    flag = os.environ.get("WORKBENCH_SNAPSHOTS", "").strip().lower()
    if flag in ("0", "false", "off", "no"):
        return False
    if flag in ("1", "true", "on", "yes"):
        return True
    return localdb._url is not None  # the embedded database is in use: this is someone's own machine


def folder_for(project_id: str) -> Path:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,40}", project_id):
        raise OntologyError("not a project id")
    root = os.environ.get("ONTO_PROJECTS_DIR", "").strip()
    return (Path(root).expanduser() if root else localdb.data_dir() / "projects") / project_id


def _name(title: str, taken: set[str]) -> str:
    """A file name from a title: readable (CJK kept), no path characters, unique in its folder."""
    base = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", title or "").strip(" .") or "untitled"
    base = re.sub(r"\s+", " ", base)[:80]
    name, n = base, 2
    while name.lower() in taken:
        name, n = f"{base} ({n})", n + 1
    taken.add(name.lower())
    return f"{name}.md"


def _write(path: Path, text: str) -> bool:
    """Write when the content changed; True when it did (so unchanged files keep their times)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return True


def _git(folder: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, timeout=60)


def _commit(folder: Path, message: str) -> str | None:
    """Commit everything in the folder; the new commit's id, or None when nothing changed."""
    if not shutil.which("git"):
        raise OntologyError("没有找到 git：快照已写入文件夹，但没有提交")
    if not (folder / ".git").exists():
        out = _git(folder, "init", "-q")
        if out.returncode:
            raise OntologyError(f"git init 失败：{out.stderr.strip()[:200]}")
    _git(folder, "add", "-A")
    if not _git(folder, "status", "--porcelain").stdout.strip():
        return None
    who: list[str] = []
    if not _git(folder, "config", "user.email").stdout.strip():  # no identity set up: commit as the app
        who = ["-c", "user.name=onto-research", "-c", "user.email=snapshots@onto-research.invalid"]
    out = subprocess.run(["git", "-C", str(folder), *who, "commit", "-q", "-m", message],
                         capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise OntologyError(f"git commit 失败：{(out.stderr or out.stdout).strip()[:200]}")
    return _git(folder, "rev-parse", "--short", "HEAD").stdout.strip() or None


def write(conn: psycopg.Connection, project_id: str, git: bool | None = None, message: str | None = None,
          folder: Path | None = None) -> dict[str, Any]:
    """Write the project's snapshot. git=None commits when the folder is already a git repository."""
    folder = folder or folder_for(project_id)
    folder.mkdir(parents=True, exist_ok=True)
    doc = projects.export(conn, project_id)
    with projects.project_connect(project_id, projects._url(conn)) as pc:
        items = pc.execute("SELECT citekey, csl FROM library_items WHERE status = 'active' ORDER BY citekey").fetchall()
        papers = Manuscripts(pc).export()
        notes = Library(pc).export()["notes"]
    changed: list[str] = []

    def put(rel: str, text: str) -> None:
        if _write(folder / rel, text):
            changed.append(rel)

    stable = {k: v for k, v in doc.items() if k != "exported_at"}  # the time is in snapshot.json; unchanged stays unchanged
    put("project.onto.json", json.dumps(stable, ensure_ascii=False, indent=1, default=str) + "\n")
    put("references.bib", bibio.to_bibtex(items))
    put("references.json", json.dumps([{**(it["csl"] or {}), "id": it["citekey"]} for it in items],
                                      ensure_ascii=False, indent=1) + "\n")
    kept: dict[str, set[str]] = {"manuscripts": set(), "notes": set()}
    taken: set[str] = set()
    for m in papers:
        meta = {**exporter.metadata(m), "csl-style": m["csl_style"], "bibliography": "../references.bib"}
        rel = f"manuscripts/{_name(m['title'], taken)}"
        kept["manuscripts"].add(rel)
        put(rel, exporter.yaml_block(meta) + exporter.markdown(m))
    taken = set()
    for n in notes:
        rel = f"notes/{_name(n['title'] or '无标题笔记', taken)}"
        kept["notes"].add(rel)
        put(rel, f"# {(n['title'] or '无标题笔记').strip()}\n\n{(n['body'] or '').strip()}\n")
    removed = []
    for sub, keep in kept.items():
        for f in sorted((folder / sub).glob("*.md")) if (folder / sub).exists() else []:
            rel = f"{sub}/{f.name}"
            if rel not in keep:
                f.unlink()
                removed.append(rel)
    at = datetime.now(UTC).isoformat(timespec="seconds")
    info = {"project": project_id, "name": doc["project"]["name"], "at": at,
            "counts": {"manuscripts": len(papers), "notes": len(notes), "references": len(items),
                       "nodes": len(doc["nodes"]), "links": len(doc["links"])}}
    if changed or removed or not (folder / INFO).exists():
        _write(folder / INFO, json.dumps(info, ensure_ascii=False, indent=1) + "\n")
    use_git = (folder / ".git").exists() if git is None else git
    commit = None
    if use_git:
        commit = _commit(folder, message or f"快照 {at}")
    out = {**info, "folder": str(folder), "changed": changed, "removed": removed, "git": use_git, "commit": commit}
    return out


def last(project_id: str) -> dict[str, Any] | None:
    """The last snapshot's info, or None."""
    folder = folder_for(project_id)
    try:
        info = json.loads((folder / INFO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    git = (folder / ".git").exists()
    commit = _git(folder, "rev-parse", "--short", "HEAD").stdout.strip() if git and shutil.which("git") else None
    return {**info, "folder": str(folder), "git": git, "commit": commit or None}
