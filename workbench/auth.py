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


async def cloudbase_sign_in(env_id: str, username: str, password: str) -> tuple[str, int]:
    """The subject comes from CloudBase's HTTPS response, never from the browser."""
    url = f"https://{env_id}.api.tcloudbasegateway.com/auth/v1/signin"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(url, json={"username": username, "password": password})
    except httpx.HTTPError:
        raise LoginError(503, "暂时无法连接登录服务，请稍后重试。") from None
    if response.status_code == 429:
        raise LoginError(429, "登录尝试过多，请稍后重试。")
    if response.status_code in (400, 401, 403):
        raise LoginError(401, "无法登录，请检查账号和密码。")
    if response.status_code != 200:
        raise LoginError(503, "登录服务暂时不可用。")
    try:
        body = response.json()
        user_id, ttl = body["sub"], body["expires_in"]
        if not isinstance(user_id, str) or not USER_ID.fullmatch(user_id) or type(ttl) is not int or ttl <= 0:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise LoginError(503, "登录服务返回了无效的身份信息。") from None
    return user_id, min(ttl, MAX_SESSION_SECONDS)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def start_session(user_id: str, ttl: int) -> str:
    token = secrets.token_urlsafe(32)
    with connect() as conn, conn.transaction():
        conn.execute("DELETE FROM onto_research.web_sessions WHERE expires_at <= now()")
        conn.execute(
            """INSERT INTO onto_research.web_sessions (token_hash, user_id, expires_at)
               VALUES (%s, %s, now() + %s * interval '1 second')""",
            (_digest(token), user_id, ttl),
        )
    return token


def session_user(token: str) -> str | None:
    if not SESSION_TOKEN.fullmatch(token):
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT user_id FROM onto_research.web_sessions WHERE token_hash = %s AND expires_at > now()",
            (_digest(token),),
        ).fetchone()
    return row["user_id"] if row else None


def end_session(token: str) -> None:
    if SESSION_TOKEN.fullmatch(token):
        with connect() as conn:
            conn.execute("DELETE FROM onto_research.web_sessions WHERE token_hash = %s", (_digest(token),))


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
