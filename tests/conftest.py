import os
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

import pytest

from ontology import series
from ontology.db import connect, migrate
from ontology.store import OntologyStore

os.environ["WORKBENCH_COLLECT"] = "0"  # tests never reach the network
os.environ["COLLECT_NEWS"] = "0"
os.environ["REPORT_AUTO"] = "0"

TEST_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres:pg@localhost:5432/onto_research_test")

LLM = {"kind": "llm_prior"}
USER = {"kind": "user"}


@pytest.fixture
def conn():
    params = conninfo_to_dict(TEST_URL)
    if params.get("dbname") != "onto_research_test" or params.get("host") not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("Tests require the dedicated local onto_research_test database.")
    c = connect(TEST_URL)
    for r in c.execute("SELECT nspname FROM pg_namespace WHERE starts_with(nspname, 'onto_research_p_')").fetchall():
        c.execute(f'DROP SCHEMA "{r["nspname"]}" CASCADE')
    c.execute("DROP SCHEMA IF EXISTS onto_research CASCADE")
    c.execute("DROP SCHEMA public CASCADE")
    c.execute("CREATE SCHEMA public")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def store(conn):
    s = OntologyStore(conn, actor="test-agent")
    s.sync_seed_types()
    return s


@pytest.fixture
def human(conn, store):
    return OntologyStore(conn, actor="human")


def node(store, type, name, **kw):
    kw.setdefault("provenance", LLM)
    kw.setdefault("rationale", "test")
    return store.upsert_node(type, name, **kw)["node"]


def link(store, type, a, b, **kw):
    kw.setdefault("provenance", LLM)
    kw.setdefault("rationale", "test")
    return store.create_link(type, a["id"], b["id"], **kw)["link"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Live sources (Binance, FRED, feeds) must never be reached from tests; fakes are injected where needed."""
    from ontology import sources

    monkeypatch.setattr(series, "TEMPLATES", Path(__file__).parent / "fixtures" / "bindings.yaml")
    from ontology.errors import OntologyError

    def blocked(url, **_kw):
        raise OntologyError(f"network disabled in tests: {url}")

    monkeypatch.setattr(sources, "_get", blocked)
