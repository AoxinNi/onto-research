"""Private staging entry point for the research workbench."""

from __future__ import annotations

import base64
import os
import secrets
from urllib.parse import urlsplit

from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send


class StagingAccess:
    def __init__(self, app: ASGIApp, password: str) -> None:
        self.app = app
        self.authorization = b"Basic " + base64.b64encode(f"research:{password}".encode())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        if kind == "http" and scope["path"] == "/healthz" and scope["method"] == "GET":
            await JSONResponse({"status": "ok"})(scope, receive, send)
            return

        headers = dict(scope["headers"])
        credential = headers.get(b"authorization", b"")
        if not secrets.compare_digest(credential, self.authorization):
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await Response(
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="onto-research staging"', "Cache-Control": "no-store"},
                )(scope, receive, send)
            return

        check_origin = kind == "websocket" or scope["method"] not in {"GET", "HEAD", "OPTIONS"}
        origin = headers.get(b"origin")
        if check_origin and origin:
            try:
                same_origin = urlsplit(origin).netloc.lower() == headers.get(b"host", b"").lower()
            except ValueError:
                same_origin = False
            if not same_origin:
                if kind == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await Response(status_code=403)(scope, receive, send)
                return

        await self.app(scope, receive, send)


def create_app() -> ASGIApp:
    """Require a staging password even if a service URL is externally reachable."""
    password = os.environ.get("WORKBENCH_STAGE_PASSWORD", "")
    if len(password) < 32:
        raise RuntimeError("WORKBENCH_STAGE_PASSWORD must contain at least 32 characters.")

    from .server import app

    return StagingAccess(app, password)
