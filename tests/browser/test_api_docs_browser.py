"""Playwright pass over /docs: Swagger UI renders from the vendored assets, token or not."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from typing import Any, NamedTuple

import pytest

from lanscape_atlas.config import Settings
from lanscape_atlas.web.app import create_app

pytest.importorskip("playwright.sync_api")

TOKEN = "docs-browser-token"


class Served(NamedTuple):
    base: str


def _serve(tmp_path: Any, token: str | None) -> Iterator[Served]:
    uvicorn = pytest.importorskip("uvicorn")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    settings = Settings(data_dir=tmp_path, api_token=token, allowed_hosts=(f"127.0.0.1:{port}",))
    app = create_app(settings, start_background=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield Served(f"http://127.0.0.1:{port}")
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def open_server(tmp_path: Any) -> Iterator[Served]:
    yield from _serve(tmp_path, None)


@pytest.fixture
def token_server(tmp_path: Any) -> Iterator[Served]:
    yield from _serve(tmp_path, TOKEN)


def _track(page: Any) -> tuple[list[str], list[str]]:
    urls: list[str] = []
    errors: list[str] = []
    page.on("request", lambda r: urls.append(r.url))
    page.on("pageerror", lambda e: errors.append(str(e)))
    return urls, errors


def test_docs_renders_without_token(page: Any, open_server: Served) -> None:
    urls, errors = _track(page)
    page.goto(f"{open_server.base}/docs")
    page.wait_for_selector(".swagger-ui .opblock", timeout=15000)
    assert page.is_hidden("#api-docs-gate")
    assert errors == []
    assert urls and all(u.startswith(open_server.base) for u in urls), urls


def test_docs_asks_for_token_then_renders(page: Any, token_server: Served) -> None:
    urls, errors = _track(page)
    page.goto(f"{token_server.base}/docs")
    page.wait_for_selector("#api-docs-token", state="visible", timeout=15000)
    page.fill("#api-docs-token", "not-the-token")
    page.click("#api-docs-form button")
    page.wait_for_function(
        "document.getElementById('api-docs-error').textContent.includes('rejected')"
    )
    page.fill("#api-docs-token", TOKEN)
    page.click("#api-docs-form button")
    page.wait_for_selector(".swagger-ui .opblock", timeout=15000)
    assert page.input_value("#api-docs-token") == ""
    assert errors == []
    assert urls and all(u.startswith(token_server.base) for u in urls), urls
