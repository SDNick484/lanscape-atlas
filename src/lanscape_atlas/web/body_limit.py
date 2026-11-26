"""Request-body size limit for the JSON routes: ``/api/*`` and ``/mcp*``.

Pure ASGI middleware, installed by ``create_app``. Only those two prefixes are
limited; the floor-plan image upload (``POST /floorplan/plan``, a form post
outside ``/api``) keeps its own, larger limit in ``web/floorplan.py``.
"""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

JSON_BODY_LIMIT = 64 * 1024
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _limited(path: str) -> bool:
    return path == "/api" or path.startswith("/api/") or path.startswith("/mcp")


class JsonBodyLimitMiddleware:
    """Refuse a body over ``limit`` bytes with 413, before any route reads it.

    A declared ``Content-Length`` is checked first; a body with none (chunked) is
    buffered up to the limit and replayed to the app, so nothing larger is held.
    """

    def __init__(self, app: ASGIApp, limit: int = JSON_BODY_LIMIT) -> None:
        self.app = app
        self._limit = limit

    async def _refuse(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            {"detail": f"request body is larger than {self._limit // 1024} KiB"}, 413
        )
        await response(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method", "GET").upper() not in _BODY_METHODS
            or not _limited(scope.get("path", ""))
        ):
            await self.app(scope, receive, send)
            return
        for key, value in scope.get("headers", ()):
            if key == b"content-length":
                if value.isdigit() and int(value) > self._limit:
                    await self._refuse(scope, receive, send)
                    return
                await self.app(scope, receive, send)
                return
        messages: list[Message] = []
        size = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            size += len(message.get("body", b""))
            if size > self._limit:
                await self._refuse(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        replay = iter(messages)

        async def replayed() -> Message:
            return next(replay, None) or await receive()

        await self.app(scope, replayed, send)
