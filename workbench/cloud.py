"""CloudBase entry point for invited and verified self-service accounts."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection, Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

from . import auth, registration

NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
LOGIN_PAGE = Path(__file__).parent / "static" / "login.html"
PRIVATE_COOKIE = {"secure": True, "httponly": True, "samesite": "lax", "path": "/"}


class CloudAccess:
    def __init__(self, app: ASGIApp, env_id: str, origin: str, allowed: frozenset[str],
                 providers: frozenset[str]) -> None:
        self.app = app
        self.env_id = env_id
        self.origin = origin
        self.allowed = allowed
        self.providers = providers

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

        user_id = await asyncio.to_thread(auth.session_user, token, self.allowed)
        if user_id is None:
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
        if path == "/auth/options" and method == "GET":
            return JSONResponse({"providers": sorted(self.providers)}, headers=NO_STORE)
        if path == "/auth/logout" and method == "POST":
            await asyncio.to_thread(auth.end_session, token)
            response = Response(status_code=204, headers=NO_STORE)
            response.delete_cookie(auth.COOKIE, **PRIVATE_COOKIE)
            return response
        if path == "/auth/login" and method == "POST":
            return await self.login(Request(scope, receive), token)
        if path.startswith(("/auth/sms/", "/auth/wechat/")):
            return await self.registration_route(path, method, Request(scope, receive), token)
        return None

    async def login(self, request: Request, previous_token: str) -> Response:
        try:
            data = await _json_body(request)
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
        if user_id not in self.allowed and not await asyncio.to_thread(auth.registered_user, user_id):
            return JSONResponse({"detail": "该账号尚未获邀。"}, 403, headers=NO_STORE)

        response = Response(status_code=204, headers=NO_STORE)
        return await self.login_response(response, user_id, ttl, previous_token)

    async def login_response(self, response: Response, user_id: str, ttl: int, previous_token: str,
                             provider: str | None = None) -> Response:
        enrollment = provider if user_id not in self.allowed else None
        token = await asyncio.to_thread(auth.start_session, user_id, ttl, registration_provider=enrollment)
        await asyncio.to_thread(auth.end_session, previous_token)
        response.set_cookie(auth.COOKIE, token, max_age=ttl, **PRIVATE_COOKIE)
        return response

    async def registration_route(self, path: str, method: str, request: Request, previous_token: str) -> Response:
        provider = "sms" if path.startswith("/auth/sms/") else "wechat"
        if provider not in self.providers:
            return JSONResponse({"detail": "该注册方式暂未开放。"}, 404, headers=NO_STORE)
        try:
            if method == "POST" and path == "/auth/sms/send":
                return await self.sms_send(request)
            if method == "POST" and path == "/auth/sms/complete":
                return await self.sms_complete(request, previous_token)
            if method == "POST" and path == "/auth/wechat/start":
                return await self.wechat_start()
            if method == "GET" and path == registration.WECHAT_CALLBACK:
                return await self.wechat_complete(request, previous_token)
        except auth.LoginError as error:
            return JSONResponse({"detail": str(error)}, error.status, headers=NO_STORE)
        return JSONResponse({"detail": "找不到此入口。"}, 404, headers=NO_STORE)

    async def sms_send(self, request: Request) -> Response:
        phone = registration.phone_number(await _field(request, "phone"))
        await asyncio.to_thread(registration.reserve_sms, phone)
        challenge, ttl = await registration.send_sms(self.env_id, phone)
        token = await asyncio.to_thread(registration.create_challenge, "sms", challenge, ttl)
        response = JSONResponse({"retry_after": registration.SMS_COOLDOWN}, headers=NO_STORE)
        response.set_cookie(registration.SMS_COOKIE, token, max_age=ttl, **PRIVATE_COOKIE)
        return response

    async def sms_complete(self, request: Request, previous_token: str) -> Response:
        data = await _json_body(request)
        code = data.get("code")
        if not isinstance(code, str) or not registration.CODE.fullmatch(code):
            raise auth.LoginError(400, "请输入 6 位短信验证码。")
        phone = registration.phone_number(data.get("phone"))
        token = request.cookies.get(registration.SMS_COOKIE, "")
        challenge = await asyncio.to_thread(registration.sms_attempt, token)
        if auth.digest(phone) != challenge["phone_hash"]:
            raise auth.LoginError(400, "手机号已更换，请重新发送验证码。")
        verification_token = await registration.verify_sms(self.env_id, challenge, code)
        await asyncio.to_thread(registration.consume_challenge, token, "sms")
        user_id, ttl = await registration.sms_identity(self.env_id, challenge, phone, verification_token)
        response = Response(status_code=204, headers=NO_STORE)
        response.delete_cookie(registration.SMS_COOKIE, **PRIVATE_COOKIE)
        return await self.login_response(response, user_id, ttl, previous_token, "sms")

    async def wechat_start(self) -> Response:
        state, device = secrets.token_urlsafe(32), secrets.token_hex(16)
        callback = self.origin + registration.WECHAT_CALLBACK
        uri = await registration.wechat_uri(self.env_id, callback, state, device)
        token = await asyncio.to_thread(registration.create_challenge, "wechat",
                                        {"state": state, "device": device}, registration.CHALLENGE_TTL)
        response = JSONResponse({"url": uri}, headers=NO_STORE)
        response.set_cookie(registration.WECHAT_COOKIE, token, max_age=registration.CHALLENGE_TTL, **PRIVATE_COOKIE)
        return response

    async def wechat_complete(self, request: Request, previous_token: str) -> Response:
        states = request.query_params.getlist("state")
        codes = request.query_params.getlist("code")
        if len(states) != 1 or not auth.SESSION_TOKEN.fullmatch(states[0]):
            return RedirectResponse("/login?error=wechat-state", status_code=303, headers=NO_STORE)
        token = request.cookies.get(registration.WECHAT_COOKIE, "")
        try:
            challenge = await asyncio.to_thread(registration.consume_challenge, token, "wechat", states[0])
        except auth.LoginError:
            return RedirectResponse("/login?error=wechat-state", status_code=303, headers=NO_STORE)

        if len(codes) != 1 or not re.fullmatch(r"[A-Za-z0-9_.-]{1,512}", codes[0]):
            response = RedirectResponse("/login?error=wechat-cancelled", status_code=303, headers=NO_STORE)
        else:
            try:
                user_id, ttl = await registration.wechat_identity(
                    self.env_id, self.origin + registration.WECHAT_CALLBACK, codes[0], challenge["device"],
                )
            except auth.LoginError:
                response = RedirectResponse("/login?error=wechat-failed", status_code=303, headers=NO_STORE)
            else:
                response = RedirectResponse("/", status_code=303, headers=NO_STORE)
                await self.login_response(response, user_id, ttl, previous_token, "wechat")
        response.delete_cookie(registration.WECHAT_COOKIE, **PRIVATE_COOKIE)
        return response

    async def websocket(self, scope: Scope, receive: Receive, send: Send, token: str, user_id: str) -> None:
        closed = False

        async def check_session() -> None:
            nonlocal closed
            current = await asyncio.to_thread(auth.session_user, token, self.allowed)
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


async def _field(request: Request, name: str) -> object:
    return (await _json_body(request)).get(name)


async def _json_body(request: Request) -> dict:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 4096:
            return {}
        body.extend(chunk)
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def create_app() -> ASGIApp:
    env_id = os.environ.get("WORKBENCH_CLOUDBASE_ENV_ID", "").strip()
    origin = os.environ.get("WORKBENCH_PUBLIC_ORIGIN", "").strip()
    allowed = frozenset(uid.strip() for uid in os.environ.get("WORKBENCH_ALLOWED_USER_IDS", "").split(",") if uid.strip())
    providers = frozenset(value.strip() for value in os.environ.get("WORKBENCH_REGISTRATION_PROVIDERS", "").split(",")
                          if value.strip())
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,79}", env_id):
        raise RuntimeError("请配置 WORKBENCH_CLOUDBASE_ENV_ID。")
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or origin != f"https://{parsed.netloc}":
        raise RuntimeError("WORKBENCH_PUBLIC_ORIGIN 必须为 HTTPS 来源，且不带路径。")
    if not allowed or not all(auth.USER_ID.fullmatch(uid) for uid in allowed):
        raise RuntimeError("请配置 WORKBENCH_ALLOWED_USER_IDS，填入已邀请的 CloudBase 用户 ID。")
    if not providers <= registration.PROVIDERS:
        raise RuntimeError("WORKBENCH_REGISTRATION_PROVIDERS 仅支持 sms 和 wechat。")

    from .server import app

    return CloudAccess(app, env_id, origin, allowed, providers)
