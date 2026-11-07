import base64
import json
from urllib.parse import urlsplit

import httpx2
import pytest
from fastapi.testclient import TestClient

from ontology import mcp_server
from tests.conftest import TEST_URL
from tests.test_workbench import drain
from workbench import auth, cloud, model_settings, server

ORIGIN = "https://research.example"
OWNER = "model-owner"
OTHER = "other-owner"
KEY = "fake-provider-key"
URL = "https://models.example/anthropic"
MODEL = "provider/test-model"


@pytest.fixture
def model_app(conn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    monkeypatch.setenv("WORKBENCH_CLOUDBASE_ENV_ID", "research-test")
    monkeypatch.setenv("WORKBENCH_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setenv("WORKBENCH_ALLOWED_USER_IDS", f"{OWNER},{OTHER}")
    monkeypatch.setenv("WORKBENCH_MODEL_ALLOWED_HOSTS", "api.anthropic.com,models.example")
    monkeypatch.setenv("WORKBENCH_MODEL_SETTINGS_KEY", base64.urlsafe_b64encode(b"k" * 32).decode())
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "unused-global-bearer")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-Platform-Secret: unused-global-header")
    monkeypatch.setattr(mcp_server, "_stores", {})

    async def signin(env_id, username, password):
        return username, 1200

    monkeypatch.setattr(auth, "cloudbase_sign_in", signin)
    yield cloud.create_app()
    for store in mcp_server._stores.values():
        store.conn.close()


def logged_in(app, name=OWNER):
    client = TestClient(app, base_url=ORIGIN, headers={"origin": ORIGIN})
    assert client.post("/auth/login", json={"username": name, "password": "test"}).status_code == 204
    return client


def config(**override):
    return {"mode": "custom", "base_url": URL, "model": MODEL, "auth_kind": "api_key",
            "api_key": KEY, **override}


def test_cloud_settings_are_account_scoped_and_encrypted(model_app, conn):
    with TestClient(model_app, base_url=ORIGIN, follow_redirects=False) as anonymous:
        assert anonymous.get("/settings").headers["location"] == "/login"
        assert anonymous.get("/api/settings/model").status_code == 401
        owner = logged_in(model_app)
        other = logged_in(model_app, OTHER)
        try:
            assert "平台设置" in owner.get("/settings").text
            saved = owner.put("/api/settings/model", json=config(user_id=OTHER))
            assert saved.status_code == 200 and saved.json()["key_configured"]
            assert KEY not in saved.text and KEY not in owner.get("/api/settings/model").text
            assert other.get("/api/settings/model").json()["mode"] == "environment"
            row = conn.execute("SELECT * FROM onto_research.web_model_settings").fetchone()
            assert row["user_id"] == OWNER and KEY.encode() not in row["encrypted_key"]
            assert model_settings.resolve(OWNER).key == KEY
            assert KEY not in repr(model_settings.resolve(OWNER))
            assert owner.put("/api/settings/model", json=config(model="other-model", api_key="")).status_code == 200
            assert model_settings.resolve(OWNER).key == KEY
            assert owner.put("/api/settings/model", json=config(base_url="https://api.anthropic.com",
                                                               api_key="")).status_code == 400
            for blocked in ("http://127.0.0.1:3425", "https://unapproved.example", "https://models.example@evil.example",
                            "https://models.example:8080"):
                response = owner.put("/api/settings/model", json=config(base_url=blocked))
                assert response.status_code == 400 and KEY not in response.text
            assert owner.put("/api/settings/model", json=config(mode="magpie")).status_code == 400
            assert owner.get("/api/settings/model/local/magpie").status_code == 403
        finally:
            owner.close()
            other.close()


def test_saved_route_is_used_for_model_probe_and_agent_tools(model_app, monkeypatch):
    async_client = model_settings.anthropic.DefaultAsyncHttpxClient
    requests = []

    def provider(request):
        assert request.url == URL + "/v1/messages"
        assert request.headers["x-api-key"] == KEY and "authorization" not in request.headers
        assert "x-platform-secret" not in request.headers
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["model"] == MODEL and "fallbacks" not in payload
        tool_reply = bool(payload.get("tools")) and len(payload["messages"]) == 1
        content = [{"type": "tool_use", "id": "toolu_test", "name": "get_schema", "input": {}}] if tool_reply else [
            {"type": "text", "text": "Configured model replied."}
        ]
        return httpx2.Response(200, json={"id": "msg_test", "type": "message", "role": "assistant", "model": MODEL,
                                          "content": content, "stop_reason": "tool_use" if tool_reply else "end_turn",
                                          "usage": {"input_tokens": 10, "output_tokens": 10}})

    monkeypatch.setattr(model_settings.anthropic, "DefaultAsyncHttpxClient",
                        lambda **kwargs: async_client(transport=httpx2.MockTransport(provider), **kwargs))
    with TestClient(model_app, base_url=ORIGIN, headers={"origin": ORIGIN}) as client:
        assert client.post("/auth/login", json={"username": OWNER, "password": "test"}).status_code == 204
        assert client.put("/api/settings/model", json=config()).status_code == 200
        assert client.post("/api/settings/model/test").json() == {"ok": True, "model": MODEL}
        pid = client.post("/api/projects", json={"name": "Agent model route"}).json()["id"]
        with client.websocket_connect(f"wss://research.example/ws?project={pid}") as socket:
            socket.receive_json()
            socket.receive_json()
            socket.send_json({"type": "chat", "text": "Check the current schema"})
            events = drain(socket)
        assert any(event["type"] == "tool" and event["name"] == "get_schema" and event["ok"] for event in events)
        assert any(event["type"] == "assistant" and event["text"] == "Configured model replied." for event in events)
        assert len(requests) == 3 and requests[-1]["messages"][-1]["content"][0]["type"] == "tool_result"


def test_local_discovery_and_settings_never_import_provider_keys(conn, monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(model_settings.Path, "home", staticmethod(lambda: tmp_path))
    magpie = tmp_path / "config" / "magpie" / "settings.json"
    magpie.parent.mkdir(parents=True)
    magpie.write_text(json.dumps({"port": 3466, "api_key": "unused-magpie-key"}))
    claude = tmp_path / ".claude" / "settings.json"
    claude.parent.mkdir()
    claude.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://localhost:15799",
                                         "ANTHROPIC_AUTH_TOKEN": "PROXY_MANAGED",
                                         "unrelated": "unused-claude-key"}}))
    monkeypatch.setattr(model_settings.socket, "create_connection", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))

    with TestClient(server.app, base_url="http://127.0.0.1:8766", client=("127.0.0.1", 12345),
                    headers={"origin": "http://127.0.0.1:8766"}) as client:
        response = client.get("/api/settings/model")
        assert response.status_code == 200
        local = {route["id"]: urlsplit(route["base_url"]).port for route in response.json()["local_routes"]}
        assert local == {"magpie": 3466, "ccswitch": 15799}
        assert "unused-" not in response.text
        assert client.put("/api/settings/model", headers={"origin": "https://untrusted.example"},
                          json={"mode": "magpie", "model": MODEL}).status_code == 403
        assert client.put("/api/settings/model", json={"mode": "ccswitch", "model": "claude-sonnet-5"}).status_code == 200
        route = model_settings.resolve("")
        assert (route.base_url, route.auth_kind, route.key) == ("http://127.0.0.1:15799", "bearer", "PROXY_MANAGED")
        assert model_settings._local_file().stat().st_mode & 0o777 == 0o600
        assert client.put("/api/settings/model", json={"mode": "magpie", "model": MODEL}).status_code == 200
        assert model_settings.resolve("").base_url == "http://127.0.0.1:3466"
