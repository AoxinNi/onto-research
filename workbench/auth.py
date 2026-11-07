"""CloudBase sign-in and revocable, same-origin workbench sessions."""

from __future__ import annotations

import hashlib
import re
import secrets

import httpx
from starlette.types import ASGIApp, Receive, Scope, Send

from ontology.db import connect
from ontology.projects import workspace_owner

COOKIE = "__Host-onto_session"
USER_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
SESSION_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
MAX_SESSION_SECONDS = 7200


class LoginError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class CloudBaseError(Exception):
    def __init__(self, status: int, code: str = "") -> None:
        self.status = status
        self.code = code


async def cloudbase_request(env_id: str, path: str, *, data: dict | None = None,
                            query: dict | None = None, device: str | None = None) -> dict:
    url = f"https://{env_id}.api.tcloudbasegateway.com/auth/v1/{path}"
    headers = {"x-device-id": device} if device else None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            if data is None:
                response = await client.get(url, params=query, headers=headers)
            else:
                response = await client.post(url, json=data, headers=headers)
    except httpx.HTTPError:
        raise CloudBaseError(503) from None
    try:
        body = response.json()
    except ValueError:
        body = None
    if not response.is_success:
        raise CloudBaseError(response.status_code, body.get("error", "") if isinstance(body, dict) else "")
    if not isinstance(body, dict):
        raise CloudBaseError(502)
    return body


def verified_identity(body: dict) -> tuple[str, int]:
    """The subject comes from CloudBase's HTTPS response, never from the browser."""
    try:
        user_id, ttl = body["sub"], body["expires_in"]
        access_token = body["access_token"]
        if not isinstance(user_id, str) or not USER_ID.fullmatch(user_id) or type(ttl) is not int or ttl <= 0:
            raise ValueError
        if not isinstance(access_token, str) or not access_token:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise LoginError(503, "登录服务返回了无效的身份信息。") from None
    return user_id, min(ttl, MAX_SESSION_SECONDS)


async def cloudbase_sign_in(env_id: str, username: str, password: str) -> tuple[str, int]:
    try:
        result = await cloudbase_request(env_id, "signin", data={"username": username, "password": password})
    except CloudBaseError as error:
        if error.status == 429:
            raise LoginError(429, "登录尝试过多，请稍后重试。") from None
        if error.status in (400, 401, 403):
            raise LoginError(401, "无法登录，请检查账号和密码。") from None
        raise LoginError(503, "登录服务暂时不可用。") from None
    return verified_identity(result)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def registered_user(user_id: str) -> bool:
    with connect() as conn:
        return conn.execute("SELECT 1 FROM onto_research.web_registered_users WHERE user_id = %s",
                            (user_id,)).fetchone() is not None


def start_session(user_id: str, ttl: int, *, registration_provider: str | None = None) -> str:
    token = secrets.token_urlsafe(32)
    with connect() as conn, conn.transaction():
        if registration_provider:
            conn.execute(
                """INSERT INTO onto_research.web_registered_users (user_id, provider)
                   VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING""",
                (user_id, registration_provider),
            )
        conn.execute("DELETE FROM onto_research.web_sessions WHERE expires_at <= now()")
        conn.execute(
            """INSERT INTO onto_research.web_sessions (token_hash, user_id, expires_at)
               VALUES (%s, %s, now() + %s * interval '1 second')""",
            (digest(token), user_id, ttl),
        )
    return token


def session_user(token: str, invited: frozenset[str]) -> str | None:
    if not SESSION_TOKEN.fullmatch(token):
        return None
    with connect() as conn:
        row = conn.execute(
            """SELECT s.user_id FROM onto_research.web_sessions s WHERE token_hash = %s AND expires_at > now()
               AND (s.user_id = ANY(%s) OR EXISTS (
                   SELECT 1 FROM onto_research.web_registered_users u WHERE u.user_id = s.user_id
               ))""",
            (digest(token), list(invited)),
        ).fetchone()
    return row["user_id"] if row else None


def end_session(token: str) -> None:
    if SESSION_TOKEN.fullmatch(token):
        with connect() as conn:
            conn.execute("DELETE FROM onto_research.web_sessions WHERE token_hash = %s", (digest(token),))


class WorkspaceScope:
    """Apply the account scope to every workbench request, including MCP tool calls."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        token = workspace_owner.set(scope.get("state", {}).get("workspace_user") or "")
        try:
            await self.app(scope, receive, send)
        finally:
            workspace_owner.reset(token)
