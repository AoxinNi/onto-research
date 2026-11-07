"""CloudBase entry point. Only explicitly invited accounts reach the workbench."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection, Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

from . import auth

NO_STORE = {"Cache-Control": "no-store"}
LOGIN_PAGE = Path(__file__).parent / "static" / "login.html"


class CloudAccess:
    def __init__(self, app: ASGIApp, env_id: str, origin: str, allowed: frozenset[str]) -> None:
        self.app = app
        self.env_id = env_id
        self.origin = origin
        self.allowed = allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        method = scope.get("method")
        connection = HTTPConnection(scope)
        if kind == "websocket" or method not in ("GET", "HEAD", "OPTIONS"):
            if connection.headers.get("origin") != self.origin:
                if kind == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await JSONResponse({"detail": "请求来源无效。"}, 403, headers=NO_STORE)(scope, receive, send)
                return

        token = connection.cookies.get(auth.COOKIE, "")
        if kind == "http":
            response = await self.public_route(path, method, scope, receive, token)
            if response is not None:
                await response(scope, receive, send)
                return
            if path.startswith("/static/"):
                await self.app(scope, receive, send)
                return

        user_id = await asyncio.to_thread(auth.session_user, token)
        if user_id not in self.allowed:
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            if method == "GET" and (path in ("/", "/sources", "/mcp") or path.startswith("/p/")):
                response = RedirectResponse("/login", status_code=303, headers=NO_STORE)
            else:
                response = JSONResponse({"detail": "请先登录。"}, 401, headers=NO_STORE)
            await response(scope, receive, send)
            return

        scope.setdefault("state", {})["workspace_user"] = user_id
        if kind == "websocket":
            await self.websocket(scope, receive, send, token, user_id)
            return
        if path == "/auth/session" and method == "GET":
            await JSONResponse({"authenticated": True}, headers=NO_STORE)(scope, receive, send)
            return

        async def private_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, private_send)

    async def public_route(self, path: str, method: str, scope: Scope, receive: Receive, token: str) -> Response | None:
        if path == "/healthz" and method == "GET":
            return JSONResponse({"status": "ok"}, headers=NO_STORE)
        if path == "/login" and method == "GET":
            return FileResponse(LOGIN_PAGE, headers=NO_STORE)
        if path == "/auth/logout" and method == "POST":
            await asyncio.to_thread(auth.end_session, token)
            response = Response(status_code=204, headers=NO_STORE)
            response.delete_cookie(auth.COOKIE, path="/", secure=True, httponly=True, samesite="lax")
            return response
        if path == "/auth/login" and method == "POST":
            return await self.login(Request(scope, receive), token)
        return None

    async def login(self, request: Request, previous_token: str) -> Response:
        try:
            data = await request.json()
            username, password = data["username"], data["password"]
            if not isinstance(username, str) or not isinstance(password, str):
                raise ValueError
            username = username.strip()
            if not (1 <= len(username) <= 254 and 1 <= len(password) <= 256):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"detail": "请输入账号和密码。"}, 400, headers=NO_STORE)
        try:
            user_id, ttl = await auth.cloudbase_sign_in(self.env_id, username, password)
        except auth.LoginError as error:
            return JSONResponse({"detail": str(error)}, error.status, headers=NO_STORE)
        if user_id not in self.allowed:
            return JSONResponse({"detail": "该账号尚未获邀。"}, 403, headers=NO_STORE)

        token = await asyncio.to_thread(auth.start_session, user_id, ttl)
        await asyncio.to_thread(auth.end_session, previous_token)
        response = Response(status_code=204, headers=NO_STORE)
        response.set_cookie(auth.COOKIE, token, max_age=ttl, secure=True, httponly=True, samesite="lax", path="/")
        return response

    async def websocket(self, scope: Scope, receive: Receive, send: Send, token: str, user_id: str) -> None:
        closed = False

        async def check_session() -> None:
            nonlocal closed
            current = await asyncio.to_thread(auth.session_user, token)
            if closed:
                raise WebSocketDisconnect(1008)
            if current != user_id:
                closed = True
                await send({"type": "websocket.close", "code": 1008})
                raise WebSocketDisconnect(1008)

        async def private_receive() -> Message:
            message = await receive()
            if message["type"] == "websocket.receive":
                await check_session()
            return message

        async def private_send(message: Message) -> None:
            if message["type"] == "websocket.send":
                await check_session()
            await send(message)

        try:
            await self.app(scope, private_receive, private_send)
        except WebSocketDisconnect:
            return


def create_app() -> ASGIApp:
    env_id = os.environ.get("WORKBENCH_CLOUDBASE_ENV_ID", "").strip()
    origin = os.environ.get("WORKBENCH_PUBLIC_ORIGIN", "").strip()
    allowed = frozenset(uid.strip() for uid in os.environ.get("WORKBENCH_ALLOWED_USER_IDS", "").split(",") if uid.strip())
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,79}", env_id):
        raise RuntimeError("请配置 WORKBENCH_CLOUDBASE_ENV_ID。")
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or origin != f"https://{parsed.netloc}":
        raise RuntimeError("WORKBENCH_PUBLIC_ORIGIN 必须为 HTTPS 来源，且不带路径。")
    if not allowed or not all(auth.USER_ID.fullmatch(uid) for uid in allowed):
        raise RuntimeError("请配置 WORKBENCH_ALLOWED_USER_IDS，填入已邀请的 CloudBase 用户 ID。")

    from .server import app

    return CloudAccess(app, env_id, origin, allowed)
