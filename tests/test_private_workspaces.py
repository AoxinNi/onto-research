import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ontology import mcp_server, projects
from ontology.errors import OntologyError
from tests.conftest import TEST_URL
from tests.fake_claude import FakeClaude
from tests.test_workbench import drain
from workbench import auth, cloud, server
from workbench.agent import WorkbenchAgent, call_mcp_tool

ORIGIN = "https://research.example"
ENV_ID = "research-test"
WS = "wss://research.example/ws"
OWNER = "test-user-a"
OTHER = "test-user-b"


@pytest.fixture
def cloud_app(conn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    monkeypatch.setenv("WORKBENCH_CLOUDBASE_ENV_ID", ENV_ID)
    monkeypatch.setenv("WORKBENCH_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setenv("WORKBENCH_ALLOWED_USER_IDS", f"{OWNER},{OTHER}")
    monkeypatch.setattr(mcp_server, "_store", None)
    monkeypatch.setattr(mcp_server, "_stores", {})
    monkeypatch.setattr(server, "make_agent", lambda: WorkbenchAgent(client=FakeClaude()))

    async def sign_in(env_id, username, password):
        assert env_id == ENV_ID
        if password != "test-password":
            raise auth.LoginError(401, "invalid credentials")
        return username, 3600

    monkeypatch.setattr(auth, "cloudbase_sign_in", sign_in)
    yield cloud.create_app()
    for store in mcp_server._stores.values():
        store.conn.close()


def login(client, username=OWNER, **kwargs):
    return client.post("/auth/login", json={"username": username, "password": "test-password"}, **kwargs)


def test_login_requires_invite_same_origin_and_valid_session(cloud_app, conn):
    with TestClient(cloud_app, base_url=ORIGIN, headers={"origin": ORIGIN}) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/", follow_redirects=False).headers["location"] == "/login"
        assert "登录工作台" in client.get("/login").text
        assert client.get("/api/projects").status_code == 401
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(WS):
                pytest.fail("unauthenticated socket was accepted")
        assert login(client, headers={"origin": "https://untrusted.example"}).status_code == 403
        assert login(client, username="uninvited-user").status_code == 403
        assert client.post("/auth/login", json={"username": OWNER, "password": "invalid"}).status_code == 401
        assert client.get("/api/projects").status_code == 401
        response = login(client)
        assert response.status_code == 204
        assert all(flag in response.headers["set-cookie"] for flag in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"))
        assert client.get("/api/projects").headers["cache-control"] == "no-store"
        assert client.post("/api/projects", json={"name": "blocked"},
                           headers={"origin": "https://untrusted.example"}).status_code == 403
        conn.execute("UPDATE onto_research.web_sessions SET expires_at = now()")
        assert client.get("/api/projects").status_code == 401


def test_two_users_have_independent_projects_and_chat(cloud_app, conn, monkeypatch):
    projects.create(conn, "unassigned legacy", project_id="legacy")
    with (
        TestClient(cloud_app, base_url=ORIGIN, headers={"origin": ORIGIN}) as owner,
        TestClient(cloud_app, base_url=ORIGIN, headers={"origin": ORIGIN}) as other,
    ):
        assert login(owner).status_code == login(other, OTHER).status_code == 204
        assert owner.get("/api/projects").json() == []
        response = owner.post("/api/projects", json={"name": "Owner research", "owner_user_id": OTHER})
        assert response.status_code == 200
        pid = response.json()["id"]
        assert projects.get(conn, pid)["owner_user_id"] == OWNER
        assert owner.get("/p/" + pid).status_code == 200
        assert owner.get(f"/api/projects/{pid}/export").status_code == 200
        other_project = other.post("/api/projects/import", json=projects.template()).json()
        assert projects.get(conn, other_project["id"])["owner_user_id"] == OTHER
        assert [p["id"] for p in other.get("/api/projects").json()] == [other_project["id"]]
        assert [p["id"] for p in owner.get("/api/projects").json()] == [pid]

        class ScopedAgent(WorkbenchAgent):
            async def run(self, text, emit):
                result, failed = await call_mcp_tool("list_projects", {})
                assert not failed
                data = json.loads(result)
                assert data["current"] == pid
                assert [project["id"] for project in data["projects"]] == [pid]
                await super().run(text, emit)

        monkeypatch.setattr(server, "make_agent", lambda: ScopedAgent(client=FakeClaude()))
        with owner.websocket_connect(f"{WS}?project={pid}") as socket:
            assert socket.receive_json()["project"]["id"] == pid
            session = socket.receive_json()["session"]["id"]
            socket.send_json({"type": "chat", "text": "Build a graph"})
            events = drain(socket)
            assert any(e["type"] == "assistant" for e in events)
            assert all(e["ok"] for e in events if e["type"] == "tool")

        [pending] = owner.get(f"/api/graph?project={pid}").json()["review"]
        accept = f"/api/changesets/{pending['id']}/accept?project={pid}"
        assert other.post(accept).status_code == 404
        assert owner.post(accept).status_code == 200
        with owner.websocket_connect(f"{WS}?project={pid}&session={session}") as socket:
            graph = socket.receive_json()["graph"]
            assert graph["nodes"] and not any(node["pending"] for node in graph["nodes"])
            resumed = socket.receive_json()
            assert resumed["session"]["id"] == session and resumed["events"]

        for method, path in (
            ("GET", f"/p/{pid}"),
            ("GET", f"/api/graph?project={pid}"),
            ("GET", f"/api/history?project={pid}"),
            ("GET", f"/api/report?project={pid}"),
            ("GET", f"/api/projects/{pid}/export"),
            ("PATCH", f"/api/projects/{pid}"),
            ("POST", f"/api/refresh?project={pid}"),
            ("POST", f"/api/report/regenerate?project={pid}"),
            ("POST", f"/api/undo?project={pid}"),
        ):
            response = other.request(method, path, json={"name": "denied"} if method == "PATCH" else None)
            assert response.status_code in (404, 409), (method, path, response.status_code)
        with pytest.raises(WebSocketDisconnect):
            with other.websocket_connect(f"{WS}?project={pid}&session={session}"):
                pytest.fail("cross-account socket was accepted")

        assert other.get("/api/mcp").status_code == 403
        assert other.put("/api/sources/ths/key", json={"key": "untrusted"}).status_code == 403
        assert all("key_hint" not in p for p in other.get("/api/sources").json()["providers"])

        with TestClient(server.app) as local:
            assert [p["id"] for p in local.get("/api/projects").json()] == ["legacy"]
            assert local.get(f"/api/graph?project={pid}").status_code == 404


def test_logout_revokes_existing_websocket(cloud_app):
    with TestClient(cloud_app, base_url=ORIGIN, headers={"origin": ORIGIN}) as client:
        assert login(client).status_code == 204
        pid = client.post("/api/projects", json={"name": "Logout test"}).json()["id"]
        with client.websocket_connect(f"{WS}?project={pid}") as socket:
            socket.receive_json()
            socket.receive_json()
            assert client.post("/auth/logout").status_code == 204
            assert client.get("/api/projects").status_code == 401
            socket.send_json({"type": "new_session"})
            with pytest.raises(WebSocketDisconnect) as error:
                socket.receive_json()
            assert error.value.code == 1008


def test_registry_upgrade_and_explicit_claim(conn):
    conn.execute("CREATE SCHEMA onto_research")
    conn.execute("CREATE TABLE onto_research.projects (id text PRIMARY KEY)")
    conn.execute("INSERT INTO onto_research.projects (id) VALUES ('selected'), ('unassigned')")
    projects.ensure_registry(conn)
    assert projects.get(conn, "selected")["owner_user_id"] is None
    projects.claim_legacy(conn, "selected", OWNER)
    projects.claim_legacy(conn, "selected", OWNER)
    assert projects.get(conn, "selected")["owner_user_id"] == OWNER
    assert projects.get(conn, "unassigned")["owner_user_id"] is None
    with pytest.raises(OntologyError, match="already belongs"):
        projects.claim_legacy(conn, "selected", OTHER)


def test_cloudbase_signin_uses_server_response(monkeypatch):
    async_client = httpx.AsyncClient

    def reply(request):
        assert request.url == f"https://{ENV_ID}.api.tcloudbasegateway.com/auth/v1/signin"
        assert request.method == "POST"
        return httpx.Response(200, json={"sub": OWNER, "expires_in": 10000, "access_token": "opaque"})

    monkeypatch.setattr(auth.httpx, "AsyncClient",
                        lambda **kwargs: async_client(transport=httpx.MockTransport(reply), **kwargs))
    assert asyncio.run(auth.cloudbase_sign_in(ENV_ID, "test-account", "test-password")) == (OWNER, 7200)
