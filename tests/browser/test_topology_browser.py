"""Playwright pass over /topology on the fake house: zoom, search, legend, edge hiding."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from conftest import FixedClock
from lanscape_atlas.config import Settings
from lanscape_atlas.observation import Observation
from lanscape_atlas.service import Service
from lanscape_atlas.web.app import create_app

pytest.importorskip("playwright.sync_api")

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fake_house.json"
MARKUP_NAME = "<b>x</b>"


class World(NamedTuple):
    base: str
    svc: Service
    # device ids placed in rooms
    in_a: int
    in_b: int
    in_markup: int
    # the topology client used by the edge-hiding test (derived edge to the router/mesh)
    client_dev: int


@pytest.fixture
def world(tmp_path: Any, clock: FixedClock) -> Iterator[World]:
    uvicorn = pytest.importorskip("uvicorn")
    from lanscape_atlas.store.db import migrate, open_db

    conn = open_db(tmp_path / "live.db")
    migrate(conn)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    settings = Settings(data_dir=tmp_path, allowed_hosts=(f"127.0.0.1:{port}",))
    app = create_app(settings, conn=conn, clock=clock, start_background=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)

    obs = [Observation.from_json(json.dumps(d)) for d in json.loads(FIXTURE.read_text())]
    assert app.state.ingestor.ingest(obs).dropped == 0
    svc: Service = app.state.service
    with svc.lock:
        ids = [int(r[0]) for r in conn.execute("SELECT id FROM device ORDER BY id")]
        client_dev = int(
            conn.execute(
                "SELECT i.device_id FROM interface i JOIN interface ap ON ap.address = i.ap_node"
                " JOIN device d ON d.id = i.device_id"
                " WHERE coalesce(d.type, '') NOT IN ('router', 'mesh_node', 'ap')"
                " AND ap.device_id != i.device_id ORDER BY i.id LIMIT 1"
            ).fetchone()[0]
        )
    in_a, in_b, in_markup = ids[0], ids[1], ids[2]
    svc.set_place("user", in_a, "Room A")
    svc.set_place("user", in_b, "Room B")
    svc.set_place("user", in_markup, MARKUP_NAME)
    yield World(f"http://127.0.0.1:{port}", svc, in_a, in_b, in_markup, client_dev)
    server.should_exit = True
    thread.join(timeout=5)
    conn.close()


def _open(page: Any, base: str) -> list[str]:
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}/topology")
    page.wait_for_function("window.atlasCy && window.atlasCy.nodes().length > 0")
    return errors


def _zoom(page: Any) -> float:
    return float(page.evaluate("window.atlasCy.zoom()"))


def _node_bg(page: Any, device_id: int) -> str:
    return str(
        page.evaluate(f"window.atlasCy.getElementById('d{device_id}').style('background-color')")
    )


def _css_rgb(page: Any, var: str) -> str:
    """``rgb(r, g, b)`` for a CSS custom property, in Cytoscape's spelling."""
    value = page.evaluate(
        "(v) => { const e = document.createElement('div'); e.style.background = 'var(' + v + ')';"
        " document.body.appendChild(e); const c = getComputedStyle(e).backgroundColor;"
        " e.remove(); return c; }",
        var,
    )
    return str(value)


def _norm(rgb: str) -> str:
    return rgb.replace(" ", "")


def test_zoom_buttons(page: Any, world: World) -> None:
    errors = _open(page, world.base)
    page.evaluate("window.atlasCy.zoom(1)")
    page.click("#topo-zoom-in")
    assert _zoom(page) == pytest.approx(1.25, rel=1e-3)
    page.click("#topo-zoom-out")
    page.click("#topo-zoom-out")
    assert _zoom(page) == pytest.approx(1 / 1.25, rel=1e-3)
    assert errors == []


def test_zoom_is_about_the_view_centre(page: Any, world: World) -> None:
    _open(page, world.base)
    page.evaluate("window.atlasCy.zoom(1); window.atlasCy.pan({x: 0, y: 0})")
    before = page.evaluate(
        "(() => { const c = window.atlasCy; const z = c.zoom(); const p = c.pan();"
        " return {x: (c.width() / 2 - p.x) / z, y: (c.height() / 2 - p.y) / z}; })()"
    )
    page.click("#topo-zoom-in")
    after = page.evaluate(
        "(() => { const c = window.atlasCy; const z = c.zoom(); const p = c.pan();"
        " return {x: (c.width() / 2 - p.x) / z, y: (c.height() / 2 - p.y) / z}; })()"
    )
    assert after["x"] == pytest.approx(before["x"], abs=0.01)
    assert after["y"] == pytest.approx(before["y"], abs=0.01)


def test_zoom_keys_need_graph_focus(page: Any, world: World) -> None:
    _open(page, world.base)
    page.evaluate("window.atlasCy.zoom(1)")
    page.focus("#cy")
    page.keyboard.press("+")
    assert _zoom(page) == pytest.approx(1.25, rel=1e-3)
    page.keyboard.press("=")
    assert _zoom(page) == pytest.approx(1.25**2, rel=1e-3)
    page.keyboard.press("-")
    assert _zoom(page) == pytest.approx(1.25, rel=1e-3)
    # Typing in the search box must not zoom.
    page.focus("#topo-search")
    page.keyboard.type("+-=")
    assert _zoom(page) == pytest.approx(1.25, rel=1e-3)


def test_zoom_in_at_max_is_a_noop(page: Any, world: World) -> None:
    errors = _open(page, world.base)
    page.evaluate("window.atlasCy.zoom(window.atlasCy.maxZoom())")
    top = _zoom(page)
    page.click("#topo-zoom-in")
    page.focus("#cy")
    page.keyboard.press("+")
    assert _zoom(page) == top == 4
    # And the minimum clamps too.
    page.evaluate("window.atlasCy.zoom(window.atlasCy.minZoom())")
    page.click("#topo-zoom-out")
    assert _zoom(page) == pytest.approx(0.1)
    assert errors == []


def test_search_centres_and_selects(page: Any, world: World) -> None:
    _open(page, world.base)
    name = page.evaluate(f"window.atlasCy.getElementById('d{world.in_b}').data('name')")
    page.evaluate("window.atlasCy.zoom(0.5)")
    page.fill("#topo-search", name)
    page.wait_for_selector(f"#topo-search-list [data-device-id='{world.in_b}']")
    assert page.get_attribute("#topo-search", "aria-expanded") == "true"
    ids = page.eval_on_selector_all(
        "#topo-search-list [role=option]", "els => els.map(e => Number(e.dataset.deviceId))"
    )
    for _ in range(ids.index(world.in_b) + 1):
        page.keyboard.press("ArrowDown")
    active = page.get_attribute("#topo-search", "aria-activedescendant")
    assert page.get_attribute(f"#{active}", "data-device-id") == str(world.in_b)
    page.keyboard.press("Enter")
    page.wait_for_function("window.atlasCy.zoom() >= 1.5 - 1e-6")
    node = f"window.atlasCy.getElementById('d{world.in_b}')"
    page.wait_for_function(
        f"(() => {{ const n = {node}; const c = window.atlasCy; const p = n.renderedPosition();"
        " return Math.abs(p.x - c.width() / 2) < 2 && Math.abs(p.y - c.height() / 2) < 2; })()"
    )
    assert page.evaluate(f"{node}.selected()") is True
    assert page.evaluate("window.atlasCy.nodes(':selected').length") == 1
    assert page.is_visible("#topo-panel")
    assert not page.is_visible("#topo-search-list")


def test_search_by_ip_and_escape(page: Any, world: World) -> None:
    _open(page, world.base)
    page.fill("#topo-search", "192.0.2.80")
    page.wait_for_selector("#topo-search-list [role=option]")
    assert page.locator("#topo-search-list [role=option]").count() == 1
    page.keyboard.press("Escape")
    assert not page.is_visible("#topo-search-list")
    assert page.get_attribute("#topo-search", "aria-expanded") == "false"


def test_search_pulses_node_once(page: Any, world: World) -> None:
    _open(page, world.base)
    name = page.evaluate(f"window.atlasCy.getElementById('d{world.in_a}').data('name')")
    page.fill("#topo-search", name)
    page.wait_for_selector(f"#topo-search-list [data-device-id='{world.in_a}']")
    page.locator(f"#topo-search-list [data-device-id='{world.in_a}']").click()
    node = f"window.atlasCy.getElementById('d{world.in_a}')"
    page.wait_for_function(f"{node}.hasClass('pulse')")
    page.wait_for_function(f"!{node}.hasClass('pulse')", timeout=3000)


def test_legend_swatches_match_places_with_nodes(page: Any, world: World) -> None:
    _open(page, world.base)
    # Three places have a device; "no place" is the extra entry.
    assert page.locator("#topo-places .sw-place").count() == 4
    names = page.locator("#topo-places .leg-name").all_inner_texts()
    assert names == ["Room A", "Room B", MARKUP_NAME, "no place"]


def test_place_name_markup_is_text(page: Any, world: World) -> None:
    _open(page, world.base)
    assert page.locator("#topo-places b").count() == 0
    assert MARKUP_NAME in page.locator("#topo-places").inner_text()


def test_unused_place_is_not_in_legend(page: Any, world: World) -> None:
    with world.svc.lock:
        world.svc.conn.execute("INSERT INTO place(name) VALUES ('Empty Room')")
        world.svc.conn.commit()
    _open(page, world.base)
    assert "Empty Room" not in page.locator("#topo-places").inner_text()
    assert page.locator("#topo-places .sw-place").count() == 4


def test_node_fill_is_the_place_colour(page: Any, world: World) -> None:
    _open(page, world.base)
    pid = page.evaluate(f"window.atlasCy.getElementById('d{world.in_a}').data('placeId')")
    expected = _css_rgb(page, f"--place-{pid % 10}")
    assert _norm(_node_bg(page, world.in_a)) == _norm(expected)
    # A device with no place keeps the neutral fill.
    free = page.evaluate(
        "window.atlasCy.nodes().filter(n => n.data('placeId') === null)[0].id()"
    ).lstrip("d")
    assert _norm(_node_bg(page, int(free))) == _norm(_css_rgb(page, "--node"))


def test_deleted_place_gets_neutral_fill(page: Any, world: World) -> None:
    with world.svc.lock:
        world.svc.conn.execute("DELETE FROM place WHERE name = 'Room A'")
        world.svc.conn.commit()
    _open(page, world.base)
    assert page.evaluate(f"window.atlasCy.getElementById('d{world.in_a}').data('placeId')") is None
    assert _norm(_node_bg(page, world.in_a)) == _norm(_css_rgb(page, "--node"))
    assert page.locator("#topo-places .sw-place").count() == 3


def test_border_keeps_online_state_with_place_fill(page: Any, world: World) -> None:
    _open(page, world.base)
    sel = f"window.atlasCy.getElementById('d{world.in_a}')"
    page.evaluate(f"{sel}.select()")
    assert _norm(str(page.evaluate(f"{sel}.style('border-color')"))) == _norm(
        _css_rgb(page, "--accent")
    )


def test_connecting_to_new_switch_hides_dashed_edge(page: Any, world: World) -> None:
    _open(page, world.base)
    cl = f"d{world.client_dev}"
    count = f"window.atlasCy.getElementById('{cl}').connectedEdges('[!manual]').length"
    assert page.evaluate(count) == 1
    page.fill("#topo-name", "Switch Z")
    page.select_option("#topo-type", "switch")
    page.click("#topo-add-btn")
    page.wait_for_function("window.atlasCy.nodes('[?placeholder]').length >= 1")
    sw = page.evaluate("window.atlasCy.nodes('[?placeholder]').map(n => n.id())[0]")
    page.evaluate(
        f"(() => {{ const c = window.atlasCy; c.elements().unselect();"
        f" c.getElementById('{cl}').select(); c.getElementById('{sw}').select(); }})()"
    )
    page.wait_for_function("!document.getElementById('topo-connect').disabled")
    page.click("#topo-connect")
    page.wait_for_function(f"{count} === 0")
    # The manual link is there instead, and a reload shows the same.
    assert (
        page.evaluate(f"window.atlasCy.getElementById('{cl}').connectedEdges('[?manual]').length")
        == 1
    )
    page.reload()
    page.wait_for_function("window.atlasCy && window.atlasCy.nodes().length > 0")
    assert page.evaluate(count) == 0


def test_howto_open_by_default_and_remembered(page: Any, world: World) -> None:
    _open(page, world.base)
    assert page.evaluate("document.getElementById('topo-howto').open") is True
    page.click("#topo-howto summary")
    page.wait_for_function("!document.getElementById('topo-howto').open")
    page.wait_for_function("localStorage.getItem('atlas.topology.howto') === 'closed'")
    page.reload()
    page.wait_for_function("window.atlasCy")
    assert page.evaluate("document.getElementById('topo-howto').open") is False
    page.click("#topo-howto summary")
    page.wait_for_function("localStorage.getItem('atlas.topology.howto') === 'open'")
    page.reload()
    page.wait_for_function("window.atlasCy")
    assert page.evaluate("document.getElementById('topo-howto').open") is True


def test_howto_defaults_open_when_storage_throws(page: Any, world: World) -> None:
    page.add_init_script(
        "Object.defineProperty(window, 'localStorage', {get() { throw new Error('denied'); }});"
    )
    errors = _open(page, world.base)
    assert page.evaluate("document.getElementById('topo-howto').open") is True
    page.click("#topo-howto summary")
    page.wait_for_function("!document.getElementById('topo-howto').open")
    assert errors == []
