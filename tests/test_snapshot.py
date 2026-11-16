"""Project snapshots: plain files in a folder, optionally committed with git; and the embedded database's folder."""

import json
import shutil

import pytest

from ontology import localdb, projects, snapshot
from ontology.library import Library
from ontology.manuscripts import Manuscripts
from tests.conftest import TEST_URL

PAPER = {"type": "article-journal", "title": "Screen use before bed", "author": [{"family": "Zhang", "given": "Wei"}],
         "issued": {"date-parts": [[2023]]}, "DOI": "10.1111/jsr.13800"}


def test_a_snapshot_writes_readable_files_and_commits(conn, tmp_path):
    p = projects.create(conn, "睡眠")
    with projects.project_connect(p["id"], TEST_URL) as pc:
        lib, ms = Library(pc, actor="human"), Manuscripts(pc, actor="human")
        key = lib.add_item(PAPER, {"kind": "manual"})["item"]["citekey"]
        m = ms.create("屏幕/与睡眠", sections=[{"heading": "引言", "body": f"如 [@{key}] 所示。"}])
        lib.write_note("读书笔记", "很重要")
    folder = tmp_path / "proj"
    out = snapshot.write(conn, p["id"], git=False, folder=folder)
    assert sorted(out["changed"]) == ["manuscripts/屏幕 与睡眠.md", "notes/读书笔记.md", "project.onto.json",
                                      "references.bib", "references.json"]
    text = (folder / "manuscripts" / "屏幕 与睡眠.md").read_text()
    assert text.startswith("---\n") and '"bibliography": "../references.bib"' not in text
    assert 'bibliography: "../references.bib"' in text and f"# 引言\n\n如 [@{key}] 所示。" in text
    assert f"@article{{{key}," in (folder / "references.bib").read_text()
    assert json.loads((folder / "references.json").read_text())[0]["id"] == key
    assert json.loads((folder / "project.onto.json").read_text())["manuscripts"][0]["title"] == "屏幕/与睡眠"
    assert snapshot.write(conn, p["id"], git=False, folder=folder)["changed"] == []  # nothing new, nothing written

    (folder / "mine.txt").write_text("a person's own file")
    with projects.project_connect(p["id"], TEST_URL) as pc:
        Manuscripts(pc, actor="human").update(m["id"], archived=True)
    out = snapshot.write(conn, p["id"], git=False, folder=folder)
    assert out["removed"] == ["manuscripts/屏幕 与睡眠.md"] and (folder / "mine.txt").exists()

    if not shutil.which("git"):
        pytest.skip("git is not installed")
    first = snapshot.write(conn, p["id"], git=True, folder=folder, message="第一版")
    assert first["commit"] and (folder / ".git").exists()
    assert snapshot.write(conn, p["id"], folder=folder)["commit"] is None  # a repository already: git by default


def test_the_snapshot_endpoints(conn, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from workbench import server

    p = projects.create(conn, "端点")
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    monkeypatch.setenv("ONTO_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setenv("WORKBENCH_SNAPSHOTS", "0")
    with TestClient(server.app) as client:
        assert client.get(f"/api/projects/{p['id']}/snapshot").json() == {"enabled": False}
        assert client.post(f"/api/projects/{p['id']}/snapshot", json={}).status_code == 404
        monkeypatch.setenv("WORKBENCH_SNAPSHOTS", "1")
        assert client.get(f"/api/projects/{p['id']}/snapshot").json()["last"] is None
        out = client.post(f"/api/projects/{p['id']}/snapshot", json={"git": False}).json()
        assert "project.onto.json" in out["changed"] and out["folder"] == str(tmp_path / p["id"])
        assert client.get(f"/api/projects/{p['id']}/snapshot").json()["last"]["name"] == "端点"


def test_snapshots_stay_off_on_a_shared_server(monkeypatch):
    monkeypatch.delenv("WORKBENCH_SNAPSHOTS", raising=False)
    monkeypatch.setattr(localdb, "_url", None)
    assert not snapshot.enabled()
    monkeypatch.setenv("WORKBENCH_SNAPSHOTS", "1")
    assert snapshot.enabled()
    with pytest.raises(Exception, match="not a project id"):
        snapshot.folder_for("../etc")


def test_dead_processes_do_not_keep_the_embedded_database_running(monkeypatch):
    import contextlib
    import os

    class Handles:
        values = [os.getpid(), 999_999_9]  # this process, and one that is long gone

        def get(self):
            return list(self.values)

        def put(self, values):
            self.values = values

    class Server:
        global_process_id_list = Handles()
        _lock = contextlib.nullcontext()
        cleaned = False

        def cleanup(self):
            self.cleaned = True

    server = Server()
    monkeypatch.setattr(localdb, "_server", server)
    monkeypatch.setattr(localdb, "_url", "host=/tmp dbname=x")
    localdb.stop()
    assert server.global_process_id_list.values == [os.getpid()] and server.cleaned
    assert localdb._server is None and localdb._url is None
    localdb.stop()  # a second stop is a no-op


def test_the_data_folder_follows_the_platform(monkeypatch, tmp_path):
    monkeypatch.setenv("ONTO_DATA_DIR", str(tmp_path / "d"))
    assert localdb.data_dir() == tmp_path / "d"
    monkeypatch.delenv("ONTO_DATA_DIR")
    monkeypatch.setattr(localdb.sys, "platform", "darwin")
    assert str(localdb.data_dir()).endswith("Library/Application Support/onto-research")
    monkeypatch.setenv("ONTO_LOCAL_DB", "0")
    assert not localdb.wanted()
