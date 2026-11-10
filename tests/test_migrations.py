"""Schema upgrades at startup: the shared registry and every existing project reach the shipped migrations."""

import shutil
import threading

import pytest
from fastapi.testclient import TestClient

from ontology import cli, db, projects
from ontology.errors import OntologyError
from tests.conftest import TEST_URL
from workbench import server

PROBE_PROJECT = "CREATE TABLE upgrade_probe (id int PRIMARY KEY);\n"
PROBE_REGISTRY = "CREATE TABLE onto_research.upgrade_probe (id int PRIMARY KEY);\n"


@pytest.fixture
def shipped(conn, monkeypatch, tmp_path):
    """The shipped migrations plus one new migration of each kind, as a future release would add."""
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    project_dir, registry_dir = tmp_path / "migrations", tmp_path / "registry"
    shutil.copytree(db.MIGRATIONS, project_dir)
    shutil.copytree(projects.REGISTRY_MIGRATIONS, registry_dir)
    (project_dir / "010_probe.sql").write_text(PROBE_PROJECT)
    (registry_dir / "002_probe.sql").write_text(PROBE_REGISTRY)

    projects.ensure_registry(conn)
    old = projects.create(conn, "Created before the release", project_id="old-project")
    archived = projects.create(conn, "Archived, someone else's", project_id="archived-project")
    conn.execute("UPDATE onto_research.projects SET status = 'archived', owner_user_id = 'another-user' WHERE id = %s",
                 (archived["id"],))
    monkeypatch.setattr(db, "MIGRATIONS", project_dir)
    monkeypatch.setattr(projects, "REGISTRY_MIGRATIONS", registry_dir)
    return ["onto_research_p_old-project", "onto_research_p_archived-project"]


def versions(conn, schema):
    return {r["version"] for r in conn.execute(f'SELECT version FROM "{schema}".schema_migrations')}


def has_table(conn, name):
    return conn.execute("SELECT to_regclass(%s) AS t", (name,)).fetchone()["t"] is not None


def test_existing_projects_and_registry_are_upgraded_once(conn, shipped):
    old, archived = shipped
    # One project also missed a shipped migration (created by an older release).
    conn.execute(f'DROP TABLE "{old}".onchain_alerts')
    conn.execute(f"DELETE FROM \"{old}\".schema_migrations WHERE version = '009_onchain'")

    assert projects.upgrade_all(conn) == {
        "onto_research": ["002_probe"],
        archived: ["010_probe"],
        old: ["009_onchain", "010_probe"],
    }
    for schema in shipped:
        assert has_table(conn, f'"{schema}".upgrade_probe')
        assert {"009_onchain", "010_probe"} <= versions(conn, schema)
    assert has_table(conn, f'"{old}".onchain_alerts')
    assert has_table(conn, "onto_research.upgrade_probe")
    assert projects.upgrade_all(conn) == {}
    assert conn.execute("SHOW search_path").fetchone()["search_path"] == '"$user", public'


def test_registry_created_before_versions_were_recorded(conn):
    # Databases from earlier releases ran the registry SQL directly, with no record of it.
    conn.execute((projects.REGISTRY_MIGRATIONS / "001_registry.sql").read_text())
    conn.execute("INSERT INTO onto_research.projects (id, name, schema_name) VALUES ('kept', 'Kept', 'onto_research_p_kept')")
    assert projects.upgrade_registry(conn) == ["001_registry"]
    assert projects.upgrade_registry(conn) == []
    assert conn.execute("SELECT name FROM onto_research.projects WHERE id = 'kept'").fetchone()["name"] == "Kept"


def test_a_failing_project_keeps_its_state_and_stops_startup(conn, shipped):
    old, archived = shipped
    conn.execute(f'CREATE TABLE "{old}".upgrade_probe (conflict text)')

    with pytest.raises(OntologyError, match="old-project"):
        projects.upgrade_all(conn)
    assert "010_probe" not in versions(conn, old)
    assert "010_probe" in versions(conn, archived)
    assert "onto_research" not in conn.execute("SHOW search_path").fetchone()["search_path"]

    with pytest.raises(OntologyError, match="old-project"):
        with TestClient(server.app):
            pytest.fail("the workbench started on a database it does not match")


def test_concurrent_instances_apply_each_migration_once(conn, shipped):
    results, errors = [], []

    def start_instance():
        try:
            with db.connect(TEST_URL) as own:
                results.append(projects.upgrade_all(own))
        except Exception as error:  # noqa: BLE001  (reported below)
            errors.append(error)

    threads = [threading.Thread(target=start_instance) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    applied = [(schema, v) for r in results for schema, vs in r.items() for v in vs]
    assert sorted(applied) == sorted({(s, v) for s, v in applied})
    assert len(applied) == 3


def test_startup_upgrades_unless_disabled(conn, shipped, monkeypatch):
    old, _ = shipped
    monkeypatch.setenv("WORKBENCH_AUTO_MIGRATE", "0")
    with TestClient(server.app):
        assert "010_probe" not in versions(conn, old)
    monkeypatch.delenv("WORKBENCH_AUTO_MIGRATE")
    with TestClient(server.app):
        assert "010_probe" in versions(conn, old)


def test_cli_migrate(conn, shipped, capsys, monkeypatch):
    monkeypatch.setattr(cli, "load_env_file", lambda: None)  # keep the developer's .env out of the test run
    assert cli.main(["migrate"]) == 0
    out = capsys.readouterr().out
    assert "onto_research: 002_probe" in out and "已升级 3 个 schema" in out
    assert cli.main(["migrate"]) == 0
    assert "已是最新" in capsys.readouterr().out
