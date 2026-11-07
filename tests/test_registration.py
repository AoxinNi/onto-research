import json
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from ontology import projects
from tests.conftest import TEST_URL
from workbench import auth, cloud, registration

ORIGIN = "https://research.example"
ENV_ID = "research-test"
INVITED = "invited-user"
SMS_USER = "sms-user"
WECHAT_USER = "wechat-user"
PHONE = "+86 13800000000"
OTP = "123456"


@pytest.fixture
def setup(conn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    monkeypatch.setenv("WORKBENCH_CLOUDBASE_ENV_ID", ENV_ID)
    monkeypatch.setenv("WORKBENCH_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setenv("WORKBENCH_ALLOWED_USER_IDS", INVITED)
    monkeypatch.setenv("WORKBENCH_REGISTRATION_PROVIDERS", "sms,wechat")

    calls = []
    flags = {"phone_exists": False, "wechat_auto": False, "redirect_host": "open.weixin.qq.com"}
    async_client = httpx.AsyncClient

    def reply(request):
        assert request.url.host == ENV_ID + ".api.tcloudbasegateway.com"
        path = request.url.path.removeprefix("/auth/v1/")
        data = json.loads(request.content) if request.content else {}
        device = request.headers.get("x-device-id")
        calls.append((path, data, device))
        if path == "signin" and "username" in data:
            return httpx.Response(200, json={"sub": data["username"], "expires_in": 3600, "access_token": "opaque"})
        assert device
        if path == "verification":
            assert data == {"target": "ANY", "phone_number": PHONE}
            return httpx.Response(200, json={
                "verification_id": "provider-sms-id", "expires_in": 600, "is_user": flags["phone_exists"],
            })
        if path == "verification/verify":
            assert data["verification_id"] == "provider-sms-id"
            if data["verification_code"] != OTP:
                return httpx.Response(400, json={"error": "invalid_verification_code"})
            return httpx.Response(200, json={"verification_token": "provider-sms-token"})
        if path in ("signup", "signin"):
            assert data["verification_token"] == "provider-sms-token"
            if path == "signup":
                assert data["phone_number"] == PHONE
                flags["phone_exists"] = True
            return httpx.Response(200, json={"sub": SMS_USER, "expires_in": 3600, "access_token": "opaque"})
        if path == "provider/uri":
            callback = request.url.params["redirect_uri"]
            state = request.url.params["state"]
            assert request.method == "GET"
            assert request.url.params["provider_id"] == "wx_open"
            assert callback == ORIGIN + registration.WECHAT_CALLBACK
            return httpx.Response(200, json={"uri": f"https://{flags['redirect_host']}/connect/qrconnect?" + urlencode(
                {"redirect_uri": callback, "state": state},
            )})
        if path == "provider/token":
            assert data == {
                "provider_id": "wx_open", "provider_redirect_uri": ORIGIN + registration.WECHAT_CALLBACK,
                "provider_code": "wx-code",
            }
            if flags["wechat_auto"]:
                return httpx.Response(200, json={"sub": WECHAT_USER, "expires_in": 3600, "access_token": "opaque"})
            return httpx.Response(200, json={"provider_token": "provider-wechat-token"})
        if path == "signin/with/provider":
            assert data == {
                "provider_id": "wx_open", "provider_token": "provider-wechat-token", "force_disable_sign_up": False,
            }
            return httpx.Response(200, json={"sub": WECHAT_USER, "expires_in": 3600, "access_token": "opaque"})
        pytest.fail(f"Unexpected provider request {path}")

    monkeypatch.setattr(auth.httpx, "AsyncClient",
                        lambda **kwargs: async_client(transport=httpx.MockTransport(reply), **kwargs))
    return calls, flags


def browser(app):
    return TestClient(app, base_url=ORIGIN, headers={"origin": ORIGIN}, follow_redirects=False)


def send_code(client):
    return client.post("/auth/sms/send", json={"phone": PHONE})


def complete_sms(client, code=OTP, **fields):
    return client.post("/auth/sms/complete", json={"phone": PHONE, "code": code, **fields})


def start_wechat(client):
    response = client.post("/auth/wechat/start")
    assert response.status_code == 200
    return parse_qs(urlsplit(response.json()["url"]).query)["state"][0]


def callback(client, state, code="wx-code"):
    return client.get(registration.WECHAT_CALLBACK, params={"state": state, "code": code})


def test_closed_registration_and_password_still_require_admission(setup, monkeypatch):
    monkeypatch.delenv("WORKBENCH_REGISTRATION_PROVIDERS")
    with browser(cloud.create_app()) as client:
        assert client.get("/auth/options").json() == {"providers": []}
        assert send_code(client).status_code == 404
        assert client.post("/auth/wechat/start").status_code == 404
        assert client.post("/auth/login", json={"username": "uninvited", "password": "password"}).status_code == 403
        assert client.post("/auth/login", json={"username": INVITED, "password": "password"}).status_code == 204
        assert client.get("/api/projects").json() == []


def test_sms_registers_then_signs_in_to_same_private_workspace(setup, conn):
    calls, _ = setup
    app = cloud.create_app()
    with browser(app) as client, browser(app) as other:
        assert client.get("/auth/options").json() == {"providers": ["sms", "wechat"]}
        assert client.post("/auth/sms/send", headers={"origin": "https://untrusted.example"},
                           json={"phone": PHONE}).status_code == 403
        response = send_code(client)
        assert response.json() == {"retry_after": 60}
        token = client.cookies[registration.SMS_COOKIE]
        assert send_code(client).status_code == 429
        assert complete_sms(other).status_code == 400
        assert complete_sms(client, phone="13900000001").status_code == 400
        assert complete_sms(client, "000000").status_code == 400
        assert complete_sms(client, user_id=INVITED).status_code == 204
        assert {device for path, _, device in calls if path != "signin"} == {calls[0][2]}
        pid = client.post("/api/projects", json={"name": "SMS private project"}).json()["id"]
        assert projects.get(conn, pid)["owner_user_id"] == SMS_USER
        other.cookies.set(registration.SMS_COOKIE, token, domain="research.example", path="/")
        assert complete_sms(other).status_code == 400

        state = start_wechat(other)
        assert callback(other, state).headers["location"] == "/"
        assert other.get("/api/projects").json() == []
        assert other.get(f"/api/graph?project={pid}").status_code == 404

        assert client.post("/auth/logout").status_code == 204
        conn.execute("UPDATE onto_research.web_sms_requests SET sent_at = now() - interval '2 minutes'")
        assert send_code(client).status_code == 200
        assert complete_sms(client).status_code == 204
        assert [p["id"] for p in client.get("/api/projects").json()] == [pid]
        assert conn.execute("SELECT count(*) AS n FROM onto_research.web_registered_users WHERE user_id = %s",
                            (SMS_USER,)).fetchone()["n"] == 1


def test_sms_quota_and_wrong_code_cap(setup, conn):
    calls, _ = setup
    with browser(cloud.create_app()) as client:
        assert send_code(client).status_code == 200
        for _ in range(5):
            assert complete_sms(client, "000000").status_code == 400
        assert complete_sms(client).status_code == 400
        assert sum(path == "verification/verify" for path, _, _ in calls) == 5
        conn.execute(
            """INSERT INTO onto_research.web_sms_requests (recipient_hash)
               SELECT i::text FROM generate_series(1, 10) AS i"""
        )
        assert client.post("/auth/sms/send", json={"phone": "13900000001"}).status_code == 429
        assert sum(path == "verification" for path, _, _ in calls) == 1


@pytest.mark.parametrize("auto", [False, True])
def test_wechat_state_is_browser_bound_and_single_use(setup, conn, auto):
    calls, flags = setup
    flags["wechat_auto"] = auto
    app = cloud.create_app()
    with browser(app) as client, browser(app) as other:
        state = start_wechat(client)
        original = client.cookies[registration.WECHAT_COOKIE]
        assert callback(other, state).headers["location"] == "/login?error=wechat-state"
        assert callback(client, "A" * 43).headers["location"] == "/login?error=wechat-state"
        assert [path for path, _, _ in calls] == ["provider/uri"]
        assert callback(client, state).headers["location"] == "/"
        assert client.get("/api/projects").json() == []
        assert conn.execute("SELECT provider FROM onto_research.web_registered_users WHERE user_id = %s",
                            (WECHAT_USER,)).fetchone()["provider"] == "wechat"
        assert len({device for _, _, device in calls}) == 1
        assert ("signin/with/provider" in [path for path, _, _ in calls]) is not auto
        client.cookies.set(registration.WECHAT_COOKIE, original, domain="research.example", path="/")
        assert callback(client, state).headers["location"] == "/login?error=wechat-state"


def test_wechat_refuses_redirect_to_an_unexpected_host(setup):
    _, flags = setup
    flags["redirect_host"] = "untrusted.example"
    with browser(cloud.create_app()) as client:
        response = client.post("/auth/wechat/start")
        assert response.status_code == 503
        assert "url" not in response.json()
        assert registration.WECHAT_COOKIE not in client.cookies
