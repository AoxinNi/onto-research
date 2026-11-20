"""The desktop app's client mode: bearer sessions held outside the web view, never cookies or Origin."""

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.conftest import TEST_URL
from tests.test_private_workspaces import ORIGIN, OWNER, WS, cloud_app, login  # noqa: F401  (fixture)
from tests.test_registration import OTP, PASSWORD, PHONE, SMS_USER, WECHAT_USER, setup  # noqa: F401  (fixture)
from workbench import auth, cloud, model_settings, registration, server

DESKTOP = {"x-onto-client": "desktop/0.1.0"}


def desktop(app):
    """No Origin and no cookie jar use: what the Rust side of the app sends."""
    return TestClient(app, base_url=ORIGIN, headers=DESKTOP, follow_redirects=False)


def bearer(token):
    return {"authorization": f"Bearer {token}"}


def expires_in_days(conn, token):
    row = conn.execute("SELECT expires_at FROM onto_research.web_sessions WHERE token_hash = %s",
                       (auth.digest(token),)).fetchone()
    return (row["expires_at"] - datetime.now(UTC)) / timedelta(days=1)


def test_desktop_login_returns_a_bearer_token_and_never_a_cookie(cloud_app, conn):
    with desktop(cloud_app) as client:
        assert client.get("/auth/options").json()["mode"] == "cloud"
        response = login(client)
        assert response.status_code == 200
        assert "set-cookie" not in response.headers
        assert response.headers["cache-control"] == "no-store"
        token = response.json()["token"]
        assert response.json()["expires_in"] == auth.DEFAULT_DESKTOP_SESSION_DAYS * 86400
        assert 13.9 < expires_in_days(conn, token) <= 14
        assert client.cookies.get(auth.COOKIE) is None

        assert client.get("/api/projects").status_code == 401
        assert client.get("/auth/session", headers=bearer(token)).json() == {"authenticated": True}
        created = client.post("/api/projects", json={"name": "Desktop project"}, headers=bearer(token))
        assert created.status_code == 200
        assert [p["id"] for p in client.get("/api/projects", headers=bearer(token)).json()] == [created.json()["id"]]
        assert client.get("/api/projects", headers=bearer("A" * 43)).status_code == 401

        assert client.post("/auth/logout", headers=bearer(token)).status_code == 204
        assert client.get("/api/projects", headers=bearer(token)).status_code == 401


def test_bearer_and_cookie_credentials_do_not_cross_over(cloud_app):
    with desktop(cloud_app) as app_client, TestClient(cloud_app, base_url=ORIGIN, headers={"origin": ORIGIN}) as web:
        token = login(app_client).json()["token"]
        assert login(web).status_code == 204

        # A browser cannot use the desktop credential, and without the client header there is no Origin waiver.
        plain = TestClient(cloud_app, base_url=ORIGIN)
        assert plain.get("/api/projects", headers=bearer(token)).status_code == 401
        assert plain.post("/api/projects", json={"name": "x"}, headers=bearer(token)).status_code == 403
        malformed = {"x-onto-client": "desktop/", **bearer(token)}
        assert plain.post("/api/projects", json={"name": "x"}, headers=malformed).status_code == 403

        # A desktop request ignores ambient cookies entirely.
        app_client.cookies.set(auth.COOKIE, web.cookies[auth.COOKIE], domain="research.example", path="/")
        assert app_client.get("/api/projects").status_code == 401
        assert web.get("/api/projects").status_code == 200


def test_desktop_websocket_authenticates_with_the_bearer_header(cloud_app):
    with desktop(cloud_app) as client:
        token = login(client).json()["token"]
        pid = client.post("/api/projects", json={"name": "Socket"}, headers=bearer(token)).json()["id"]
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"{WS}?project={pid}"):
                pytest.fail("socket without a bearer token was accepted")
        with client.websocket_connect(f"{WS}?project={pid}", headers=bearer(token)) as socket:
            assert {socket.receive_json()["type"], socket.receive_json()["type"]} == {"graph", "session"}
            assert client.post("/auth/logout", headers=bearer(token)).status_code == 204
            socket.send_json({"type": "new_session"})
            with pytest.raises(WebSocketDisconnect) as error:
                socket.receive_json()
            assert error.value.code == 1008


@pytest.mark.parametrize(("days", "expected"), [("3", 3), ("999", 90), ("0", 1), ("soon", 14)])
def test_desktop_session_lifetime_is_bounded(cloud_app, conn, monkeypatch, days, expected):
    monkeypatch.setenv("WORKBENCH_DESKTOP_SESSION_DAYS", days)
    with desktop(cloud_app) as client:
        body = login(client).json()
        assert body["expires_in"] == expected * 86400
        assert expected - 0.1 < expires_in_days(conn, body["token"]) <= expected


def test_desktop_sms_registration_returns_the_challenge_instead_of_a_cookie(setup, conn):
    app = cloud.create_app()
    with desktop(app) as client, desktop(app) as other:
        sent = client.post("/auth/sms/send", json={"phone": PHONE, "intent": "register"})
        assert sent.status_code == 200 and "set-cookie" not in sent.headers
        challenge = sent.json()["challenge"]
        assert sent.json()["retry_after"] == registration.SMS_COOLDOWN

        fields = {"phone": PHONE, "code": OTP, "password": PASSWORD, "intent": "register"}
        assert client.post("/auth/sms/complete", json=fields).status_code == 400
        assert other.post("/auth/sms/complete", json={**fields, "challenge": "A" * 43}).status_code == 400
        completed = client.post("/auth/sms/complete", json={**fields, "challenge": challenge})
        assert completed.status_code == 200 and "set-cookie" not in completed.headers
        token = completed.json()["token"]
        pid = client.post("/api/projects", json={"name": "SMS desktop"}, headers=bearer(token)).json()["id"]
        assert conn.execute("SELECT owner_user_id FROM onto_research.projects WHERE id = %s",
                            (pid,)).fetchone()["owner_user_id"] == SMS_USER
        assert client.post("/auth/sms/complete", json={**fields, "challenge": challenge}).status_code == 400

        assert client.post("/auth/logout", headers=bearer(token)).status_code == 204
        relogin = client.post("/auth/login", json={"username": "13800000000", "password": PASSWORD})
        assert relogin.status_code == 200 and relogin.json()["token"]


def test_desktop_wechat_completion_is_bound_to_its_challenge_and_single_use(setup, conn):
    app = cloud.create_app()
    with desktop(app) as client:
        first = client.post("/auth/wechat/start")
        assert first.status_code == 200 and "set-cookie" not in first.headers
        state = parse_qs(urlsplit(first.json()["url"]).query)["state"][0]
        challenge = first.json()["challenge"]
        other = client.post("/auth/wechat/start").json()["challenge"]

        def complete(**fields):
            return client.post("/auth/wechat/complete", json={"state": state, "code": "wx-code",
                                                               "challenge": challenge, **fields})

        assert complete(challenge=other).status_code == 400
        assert complete(state="A" * 43).status_code == 400
        assert client.get(registration.WECHAT_CALLBACK, params={"state": state, "code": "wx-code"}).status_code == 404
        done = complete()
        assert done.status_code == 200
        token = done.json()["token"]
        assert client.get("/api/projects", headers=bearer(token)).json() == []
        assert conn.execute("SELECT provider FROM onto_research.web_registered_users WHERE user_id = %s",
                            (WECHAT_USER,)).fetchone()["provider"] == "wechat"
        assert complete().status_code == 400

    # The browser flow keeps its cookie-bound callback and has no JSON completion endpoint.
    with TestClient(app, base_url=ORIGIN, headers={"origin": ORIGIN}) as web:
        assert web.post("/auth/wechat/complete", json={"state": state}).status_code == 404


def test_local_workbench_reports_local_mode_and_accepts_desktop_settings(conn, monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(model_settings.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(model_settings.socket, "create_connection",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))
    settings = {"mode": "custom", "base_url": "https://api.anthropic.com", "model": "claude-opus-5-5",
                "auth_kind": "api_key", "api_key": "sk-test-local"}
    with TestClient(server.app, base_url="http://127.0.0.1:8766", client=("127.0.0.1", 12345)) as client:
        assert client.get("/auth/options").json() == {"mode": "local", "api": 1, "providers": []}
        assert client.put("/api/settings/model", json=settings).status_code == 403
        assert client.put("/api/settings/model", json=settings, headers={"x-onto-client": "bogus"}).status_code == 403
        saved = client.put("/api/settings/model", json=settings, headers=DESKTOP)
        assert saved.status_code == 200, saved.text
        assert "sk-test-local" not in json.dumps(saved.json())
