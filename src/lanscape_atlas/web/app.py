"""``create_app``: wires the DB, Service, Ingestor and scheduler into one FastAPI app.

Everything lives on ``app.state`` (``conn``, ``events``, ``service``, ``oui``,
``ingestor``, ``runner``, ``collectors``, ``bluetooth`` (None without Home Assistant),
``deep_scan_runner``, ``deep_scan_tasks``, ``mcp``) so later tasks can mount onto the same
app. The web UI (``pages``) is installed here; its errors render as HTML,
``/api``/``/mcp`` errors as JSON. The MCP server
(``lanscape_atlas.mcp_server``) is served at ``/mcp`` (and ``/mcp/``) with the
streamable HTTP transport; its session manager runs inside this app's lifespan.

Concurrency (ruling 30): one shared sqlite connection; every DB read in a handler
takes ``service.lock`` and Service writes take it themselves.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from lanscape_atlas.bluetooth.runtime import BluetoothRuntime
from lanscape_atlas.clock import Clock, utcnow
from lanscape_atlas.collectors import build_collectors
from lanscape_atlas.collectors.nmap import ScanRefused, run_nmap
from lanscape_atlas.config import Settings
from lanscape_atlas.events import EventBus
from lanscape_atlas.ingest import Ingestor
from lanscape_atlas.mcp_server import build_mcp
from lanscape_atlas.oui import OuiDB
from lanscape_atlas.scheduler import CollectorRunner, start_scheduler, stop_scheduler
from lanscape_atlas.service import Service, StaleShapes, StaleWrite, UndoConflict
from lanscape_atlas.store.db import migrate, open_db
from lanscape_atlas.web import find, floorplan, pages
from lanscape_atlas.web.api import router as api_router
from lanscape_atlas.web.ble_api import router as ble_router
from lanscape_atlas.web.deep_scan import start_deep_scan
from lanscape_atlas.web.floorplan_api import STALE_MESSAGE, STALE_SHAPES_MESSAGE
from lanscape_atlas.web.floorplan_api import router as floorplan_router

log = logging.getLogger(__name__)

PROTECTED_PREFIXES = ("/api", "/mcp")

# The API description page (Swagger UI, vendored under static/). Outside /api so a browser
# can open it: its spec, /api/openapi.json, is the part behind the bearer token.
API_DOCS_PAGE = Path(__file__).parent / "static" / "api-docs.html"


def load_oui(data_dir: Path) -> OuiDB:
    """``$data_dir/oui.csv`` if present, else the packaged sample."""
    custom = data_dir / "oui.csv"
    if custom.is_file():
        return OuiDB.load(custom)
    with as_file(files("lanscape_atlas") / "data" / "oui_sample.csv") as sample:
        return OuiDB.load(sample)


def _is_protected(path: str) -> bool:
    # "/api" and "/api/..." ; "/mcp" and anything starting with it ("/mcp*").
    return path == "/api" or path.startswith("/api/") or path.startswith("/mcp")


def _route_path(scope: Scope) -> str:
    """``scope["path"]`` minus ``root_path`` (as Starlette's ``get_route_path`` does)."""
    path: str = scope.get("path", "")
    root: str = scope.get("root_path", "")
    if root and path.startswith(root) and (len(path) == len(root) or path[len(root)] == "/"):
        return path[len(root) :]
    return path


def _needs_auth(scope: Scope) -> bool:
    # Check both forms: whichever one routing ends up using, protected stays protected.
    return _is_protected(_route_path(scope)) or _is_protected(scope.get("path", ""))


class BearerAuthMiddleware:
    """Pure ASGI middleware: ``/api/*`` and ``/mcp*`` need ``Authorization: Bearer <token>``.

    Being ASGI-level it also covers routers and apps mounted after ``create_app``,
    and it does not buffer streaming responses (the MCP transport streams).
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._token = token.encode("utf-8")

    def _authorized(self, scope: Scope) -> bool:
        for name, value in scope.get("headers", ()):
            if name == b"authorization":
                scheme, _, cred = value.partition(b" ")
                if scheme.lower() != b"bearer":
                    return False
                return hmac.compare_digest(cred.strip(), self._token)
        return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] in ("http", "websocket")
            and _needs_auth(scope)
            and not self._authorized(scope)
        ):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            response = JSONResponse(
                {"detail": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _message(exc: BaseException, default: str) -> str:
    # str(KeyError("x")) is "'x'"; use the bare argument instead.
    if exc.args:
        return str(exc.args[0])
    return default


JSON_PAGE_PATHS = ("/floorplan/placements", "/floorplan/scale", "/floorplan/shapes")


def _wants_json(scope: Scope) -> bool:
    """``/api*``, ``/mcp*`` and the pages' JSON endpoints (``/topology/...``, floor plan saves)."""
    path = _route_path(scope)
    return _needs_auth(scope) or path.startswith("/topology/") or path in JSON_PAGE_PATHS


def _is_page(request: Request) -> bool:
    """Everything else is the web UI, whose errors render as HTML."""
    return not _wants_json(request.scope)


async def _key_error(request: Request, exc: Exception) -> Response:
    if _is_page(request):
        return pages.error_page(request, 404, _message(exc, "not found"))
    return JSONResponse({"detail": _message(exc, "not found")}, status_code=404)


async def _value_error(request: Request, exc: Exception) -> Response:
    if _is_page(request):
        return pages.error_page(request, 422, _message(exc, "invalid request"))
    return JSONResponse({"detail": _message(exc, "invalid request")}, status_code=422)


async def _undo_conflict(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, UndoConflict)
    if _is_page(request):
        return pages.error_page(request, 409, str(exc))
    body: dict[str, Any] = {"detail": "conflict", "conflicting": list(exc.conflicting)}
    if exc.reason:
        body["reason"] = exc.reason
    return JSONResponse(body, status_code=409)


async def _stale_write(request: Request, exc: Exception) -> Response:
    # JSON on every path: the floor plan page's fetch calls rely on it too.
    assert isinstance(exc, StaleWrite)
    message = STALE_SHAPES_MESSAGE if isinstance(exc, StaleShapes) else STALE_MESSAGE
    return JSONResponse(
        {"detail": "conflict", "conflicting": list(exc.conflicting), "message": message},
        status_code=409,
    )


async def _overflow(request: Request, exc: Exception) -> Response:
    # e.g. an int inside PATCH ``fields`` too large for sqlite.
    if _is_page(request):
        return pages.error_page(request, 422, "integer out of range")
    return JSONResponse({"detail": "integer out of range"}, status_code=422)


async def _scan_refused(request: Request, exc: Exception) -> Response:
    if _is_page(request):
        return pages.error_page(request, 409, _message(exc, "scan refused"))
    return JSONResponse({"detail": _message(exc, "scan refused")}, status_code=409)


async def _http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    if _is_page(request):
        return pages.error_page(request, exc.status_code, str(exc.detail))
    return await http_exception_handler(request, exc)


async def _validation_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)
    if _is_page(request):
        bad = "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg', 'invalid')}"
            for e in exc.errors()
        )
        return pages.error_page(request, 422, bad or "invalid request")
    return await request_validation_exception_handler(request, exc)


_LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
_WILDCARD_BINDS = ("", "0.0.0.0", "::")


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def allowed_host_patterns(settings: Settings) -> list[str] | None:
    """Host header patterns the app accepts, or ``None`` when the check is off.

    - ``allowed_hosts`` contains ``"*"``: off. This explicit opt-out is the only way
      to turn the check off.
    - Otherwise: localhost, ``bind_host`` (unless a wildcard bind) and every entry of
      ``allowed_hosts``. Local names and ``bind_host`` match with or without a port;
      a bare ``allowed_hosts`` entry matches only a Host without a port, and
      ``"name:*"`` matches that name on any port.

    ``api_token`` does not change this: the token guards only ``/api`` and ``/mcp``,
    so a DNS-rebinding page could still read the pages and post their forms. A reverse
    proxy forwards the public name as Host; that name must be in ``allowed_hosts``.
    """
    if "*" in settings.allowed_hosts:
        return None
    names = list(_LOCAL_HOSTS)
    bind = settings.bind_host
    if bind not in _WILDCARD_BINDS:
        names.append(f"[{bind}]" if ":" in bind else bind)
    return _dedupe([h for n in names for h in (n, f"{n}:*")] + list(settings.allowed_hosts))


def host_allowed(host: str, patterns: list[str]) -> bool:
    """The MCP SDK's matching: exact, or ``name:*`` for ``name:<port>`` (case-insensitive)."""
    host = host.lower()
    for pattern in patterns:
        pattern = pattern.lower()
        if host == pattern:
            return True
        if pattern.endswith(":*") and host.startswith(pattern[:-1]):
            return True
    return False


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key == name:
            return str(value.decode("latin-1"))
    return None


# How many distinct rejected Host names get a log line (and are remembered) per process.
REJECTED_HOSTS_LOGGED_MAX = 32
_HOST_SHOWN_MAX = 80
_HOST_UNSAFE = re.compile(r"[^A-Za-z0-9.:_\[\]-]")


def _shown_host(host: str) -> str:
    """A client-supplied Host made safe to echo and log: hostname characters only, bounded."""
    shown = _HOST_UNSAFE.sub("?", host[:_HOST_SHOWN_MAX])
    return shown + "..." if len(host) > _HOST_SHOWN_MAX else shown


def _rejected_host_message(host: str | None) -> str:
    if not host:
        return "Request refused: it has no Host header.\n"
    return (
        f'Request refused: Host "{_shown_host(host)}" is not in allowed_hosts.\n'
        "If this is the address or name you use to reach lanscape-atlas, add it to\n"
        "allowed_hosts in config.toml and restart the service. A bare entry matches\n"
        'only a Host without a port; "name:*" matches that name on any port.\n'
    )


class HostAllowlistMiddleware:
    """Every request's Host must be allow-listed, with or without an ``api_token``.

    Blocks DNS rebinding against the whole app (pages, ``/api``, ``/topology``,
    ``/mcp``); a mismatch answers 421 like the MCP SDK's own check, in plain text that
    names the refused Host and the ``allowed_hosts`` setting. Each distinct refused
    name is logged once at WARNING, for at most ``REJECTED_HOSTS_LOGGED_MAX`` names.
    """

    def __init__(self, app: ASGIApp, patterns: list[str]) -> None:
        self.app = app
        self._patterns = patterns
        self._logged: set[str] = set()
        self._log_full = False

    def _log_rejected(self, host: str | None) -> None:
        shown = _shown_host(host).lower() if host else "(no Host header)"
        if shown in self._logged or self._log_full:
            return
        if len(self._logged) >= REJECTED_HOSTS_LOGGED_MAX:
            self._log_full = True
            log.warning(
                "refused requests for more than %d different Host names; "
                "further names are not logged",
                REJECTED_HOSTS_LOGGED_MAX,
            )
            return
        self._logged.add(shown)
        log.warning(
            "refused a request for Host %s: not in allowed_hosts. If you reach the app "
            "by this name, add it to allowed_hosts in config.toml and restart",
            shown,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            host = _header(scope, b"host")
            if not host or not host_allowed(host, self._patterns):
                self._log_rejected(host)
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                response = PlainTextResponse(
                    _rejected_host_message(host),
                    status_code=421,
                    headers={"X-Content-Type-Options": "nosniff"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin_matches_host(origin: str, host: str) -> bool:
    """True when ``origin``'s host and port equal the Host header's (default ports filled)."""
    try:
        o = urlsplit(origin)
        h = urlsplit(f"//{host}")
        default = _DEFAULT_PORTS.get(o.scheme)
        if default is None or not o.hostname or not h.hostname:
            return False
        return o.hostname == h.hostname and (o.port or default) == (h.port or default)
    except ValueError:
        return False


_MCP_PATHS = ("/mcp", "/mcp/")


def _origin_checked(scope: Scope) -> bool:
    """Every unsafe method, except on the MCP endpoint (the path routing will use)."""
    if scope.get("method", "GET").upper() not in _UNSAFE_METHODS:
        return False
    return _route_path(scope) not in _MCP_PATHS


class OriginCheckMiddleware:
    """Unsafe methods on every path but ``/mcp``: an ``Origin`` other than the Host: 403.

    Applies in every mode: browsers attach ``Origin`` to cross-origin form posts and
    fetches, so a page elsewhere cannot trigger edits, undo or scans. This also covers
    the form routes, whose double-submit CSRF cookie another web app on the same host
    (a different port) could plant, since cookies are not scoped to a port. Requests
    without an ``Origin`` (curl, MCP and API clients) are unaffected. ``/mcp`` has the
    MCP SDK's own Origin check (see ``mcp_transport_security``).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and _origin_checked(scope):
            origin = _header(scope, b"origin")
            if origin is not None and not _origin_matches_host(
                origin, _header(scope, b"host") or ""
            ):
                response = JSONResponse({"detail": "cross-origin request refused"}, 403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def mcp_transport_security(settings: Settings) -> TransportSecuritySettings:
    """Host/Origin checks for ``/mcp`` (the SDK's DNS-rebinding protection).

    Uses the same allowlist as ``HostAllowlistMiddleware`` (see
    ``allowed_host_patterns``), with or without an ``api_token``; a browser Origin must
    be ``http(s)://`` one of those. Off only with ``allowed_hosts = ["*"]``.
    """
    hosts = allowed_host_patterns(settings)
    if hosts is None:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    origins = [f"{scheme}://{h}" for h in hosts for scheme in ("http", "https")]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=_dedupe(origins),
    )


class _McpEndpoint:
    """ASGI endpoint for ``/mcp``: the current session manager handles the request.

    Looked up per request because each lifespan builds a fresh session manager
    (the SDK's ``run()`` works once per manager).
    """

    def __init__(self, server: MCPServer[Any]) -> None:
        self._server = server

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._server.session_manager.handle_request(scope, receive, send)


async def _cancel_tasks(tasks: dict[int, asyncio.Task[Any]]) -> None:
    pending = list(tasks.values())
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def create_app(
    settings: Settings,
    *,
    conn: sqlite3.Connection | None = None,
    clock: Clock = utcnow,
    start_background: bool = True,
) -> FastAPI:
    owns_conn = conn is None
    if conn is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        conn = open_db(settings.data_dir / "atlas.db")
        migrate(conn)
    db = conn

    events = EventBus()
    service = Service(db, events, clock, ip_match_window_min=settings.ip_match_window_min)
    oui = load_oui(settings.data_dir)
    ingestor = Ingestor(
        db,
        service,
        events,
        oui,
        clock,
        ip_match_window=timedelta(minutes=settings.ip_match_window_min),
    )
    runner = CollectorRunner.from_settings(settings, service, ingestor, clock)
    collectors = build_collectors(settings, service)
    bluetooth = (
        BluetoothRuntime(service, settings, oui, clock, runner=runner)
        if settings.home_assistant is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        sched = None
        try:
            if start_background:
                sched = await start_scheduler(
                    settings, runner, collectors, service, bluetooth=bluetooth
                )
            # Stateless + JSON responses: no per-client session state to lose behind a
            # proxy or on restart, and no long-lived SSE streams to buffer.
            mcp_server.streamable_http_app(
                stateless_http=True,
                json_response=True,
                transport_security=mcp_transport_security(settings),
            )
            async with mcp_server.session_manager.run():
                yield
        finally:
            await _cancel_tasks(app.state.deep_scan_tasks)
            if sched is not None:
                await stop_scheduler(sched, collectors, runner=runner, bluetooth=bluetooth)
            if owns_conn:
                db.close()

    # The OpenAPI spec stays under /api so the bearer token covers it (Ruling 33d).
    # FastAPI's own docs page loads Swagger UI from a CDN, so it is off; /docs serves
    # the vendored page instead.
    app = FastAPI(
        title="lanscape-atlas",
        version="0.1.0",
        lifespan=lifespan,
        openapi_url="/api/openapi.json",
        docs_url=None,
        redoc_url=None,
    )

    @app.get("/docs", include_in_schema=False)
    async def api_docs() -> FileResponse:
        return FileResponse(API_DOCS_PAGE, media_type="text/html; charset=utf-8")

    app.state.settings = settings
    app.state.conn = db
    app.state.clock = clock
    app.state.events = events
    app.state.service = service
    app.state.oui = oui
    app.state.ingestor = ingestor
    app.state.runner = runner
    app.state.collectors = collectors
    app.state.bluetooth = bluetooth
    app.state.deep_scan_runner = run_nmap
    # device_id -> in-flight deep scan task (at most one per device).
    app.state.deep_scan_tasks = {}

    # Added last = outermost: Host check, then Origin check, then bearer auth.
    if settings.api_token:
        app.add_middleware(BearerAuthMiddleware, token=settings.api_token)
    app.add_middleware(OriginCheckMiddleware)
    host_patterns = allowed_host_patterns(settings)
    if host_patterns is not None:
        app.add_middleware(HostAllowlistMiddleware, patterns=host_patterns)

    app.add_exception_handler(KeyError, _key_error)
    app.add_exception_handler(ValueError, _value_error)
    app.add_exception_handler(OverflowError, _overflow)
    app.add_exception_handler(UndoConflict, _undo_conflict)
    app.add_exception_handler(StaleWrite, _stale_write)
    app.add_exception_handler(ScanRefused, _scan_refused)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)

    async def scanner(device_id: int) -> None:
        start_deep_scan(app.state, device_id)

    mcp_server = build_mcp(
        service,
        scanner,
        lambda: app.state.bluetooth,
        candidate_min_rssi=settings.bluetooth.candidate_min_rssi,
    )
    app.state.mcp = mcp_server
    # Both spellings are served directly: a POST redirect is not reliably followed.
    endpoint = _McpEndpoint(mcp_server)
    for path in ("/mcp", "/mcp/"):
        # A non-function endpoint is mounted as a raw ASGI app (all methods).
        app.router.routes.append(Route(path, endpoint, include_in_schema=False))

    app.include_router(api_router)
    app.include_router(floorplan_router)
    app.include_router(ble_router)
    app.include_router(floorplan.router)
    app.include_router(find.router)
    pages.install(app)
    return app
