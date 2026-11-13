"""Topology view (Task 17): edges, dedupe, layout, placeholder and the CSRF page endpoints."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import FixedClock
from lanscape_atlas.clock import iso
from lanscape_atlas.config import Settings
from lanscape_atlas.observation import Observation
from lanscape_atlas.service import Service
from lanscape_atlas.web.app import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "fake_house.json"
NODE_KEYS = {"id", "name", "type", "icon", "online", "placeholder", "place_id", "x", "y"}
EDGE_KEYS = {"id", "source", "target", "kind", "manual"}


def _observations() -> list[Observation]:
    raw = json.loads(FIXTURE.read_text())
    return [Observation.from_json(json.dumps(d)) for d in raw]


@pytest.fixture
def app(tmp_path: Path, db: sqlite3.Connection, clock: FixedClock) -> FastAPI:
    # TestClient sends Host: testserver; LAN-only mode must allow it explicitly.
    settings = Settings(data_dir=tmp_path, allowed_hosts=("testserver",))
    app = create_app(settings, conn=db, clock=clock, start_background=False)
    assert app.state.ingestor.ingest(_observations()).dropped == 0
    return app


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def service(app: FastAPI) -> Service:
    svc: Service = app.state.service
    return svc


def _token(client: TestClient) -> str:
    if "atlas_csrf" not in client.cookies:
        assert client.get("/topology").status_code == 200
    token = client.cookies.get("atlas_csrf")
    assert token
    return token


def _post(client: TestClient, url: str, body: dict[str, Any] | None = None) -> Any:
    return client.post(url, json=body, headers={"X-CSRF-Token": _token(client)})


def _graph(client: TestClient) -> dict[str, Any]:
    r = client.get("/topology/data")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body: dict[str, Any] = r.json()
    return body


def _derived(graph: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in graph["edges"] if not e["manual"]]


def _pair(e: dict[str, Any]) -> frozenset[int]:
    return frozenset((e["source"], e["target"]))


def _last_batch(service: Service) -> int:
    with service.lock:
        return int(service.conn.execute("SELECT coalesce(max(id), 0) FROM batch").fetchone()[0])


def _two_devices(client: TestClient) -> tuple[int, int]:
    nodes = _graph(client)["nodes"]
    return nodes[0]["id"], nodes[1]["id"]


# ------------------------------------------------------------ brief tests


def test_topology_edges_from_ap_node(client: TestClient, db: sqlite3.Connection) -> None:
    """Each client interface's ``ap_node`` yields a dashed (non-manual) edge to the AP's owner."""
    graph = _graph(client)
    expected = {
        frozenset((int(r[0]), int(r[1])))
        for r in db.execute(
            "SELECT i.device_id, ap.device_id FROM interface i"
            " JOIN interface ap ON ap.address = i.ap_node"
            " WHERE i.ap_node IS NOT NULL AND i.device_id != ap.device_id"
        )
    }
    assert expected
    derived = _derived(graph)
    assert {_pair(e) for e in derived} == expected
    for e in derived:
        assert set(e) == EDGE_KEYS
        assert str(e["id"]).startswith("ap-") and e["kind"] in {"wifi", "wired"}
    # The mesh node's clients point at the mesh node's device.
    mesh = next(n for n in graph["nodes"] if n["name"] == "mesh-node-upstairs")
    assert any(e["target"] == mesh["id"] for e in derived)


def test_manual_link_is_solid_kind(client: TestClient) -> None:
    a, b = _two_devices(client)
    r = _post(client, "/topology/links", {"a_device_id": a, "b_device_id": b, "kind": "wired"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"batch_id", "link_id"}
    edges = [e for e in _graph(client)["edges"] if e["id"] == body["link_id"]]
    assert edges == [
        {"id": body["link_id"], "source": a, "target": b, "kind": "wired", "manual": True}
    ]
    # Same edge through the API.
    api = [e for e in client.get("/api/v1/topology").json()["edges"] if e["id"] == body["link_id"]]
    assert api == edges
    # Delete through the page endpoint; a second delete is a JSON 404.
    r = _post(client, f"/topology/links/{body['link_id']}/delete")
    assert r.status_code == 200 and r.json()["batch_id"] > body["batch_id"]
    assert all(e["id"] != body["link_id"] for e in _graph(client)["edges"])
    r = _post(client, f"/topology/links/{body['link_id']}/delete")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
    assert "detail" in r.json()


def test_layout_persisted(client: TestClient) -> None:
    a, b = _two_devices(client)
    r = _post(client, "/topology/layout", {"positions": {str(a): [10, 20.5], str(b): [-3, 4]}})
    assert r.status_code == 200 and r.json() == {"updated": 2}
    r = _post(client, "/topology/layout", {"positions": {str(a): [11.25, 21]}})
    assert r.status_code == 200
    nodes = {n["id"]: n for n in _graph(client)["nodes"]}
    assert (nodes[a]["x"], nodes[a]["y"]) == (11.25, 21)
    assert (nodes[b]["x"], nodes[b]["y"]) == (-3, 4)
    # And the API reads the same rows.
    api = {n["id"]: n for n in client.get("/api/v1/topology").json()["nodes"]}
    assert (api[a]["x"], api[a]["y"]) == (11.25, 21)


def test_placeholder_device_endpoint(client: TestClient, service: Service) -> None:
    r = client.post("/api/v1/devices/placeholder", json={"name": "Desk switch", "type": "switch"})
    assert r.status_code == 200
    assert set(r.json()) == {"device_id", "batch_id"}
    r = _post(client, "/topology/placeholder", {"name": "Garage hub", "type": "hub"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"device_id", "batch_id"}
    nodes = {n["id"]: n for n in _graph(client)["nodes"]}
    node = nodes[body["device_id"]]
    assert (node["name"], node["type"], node["x"], node["y"]) == ("Garage hub", "hub", None, None)
    assert any(n["name"] == "Desk switch" and n["type"] == "switch" for n in nodes.values())
    # Empty name: JSON 422 through the same Service validation, nothing written.
    before = _last_batch(service)
    r = _post(client, "/topology/placeholder", {"name": " ", "type": "switch"})
    assert r.status_code == 422 and r.headers["content-type"].startswith("application/json")
    assert _last_batch(service) == before


# ------------------------------------------------------------------ dedupe


def _ap_mac(db: sqlite3.Connection, device_id: int) -> str:
    row = db.execute(
        "SELECT ap_node FROM interface WHERE ap_node IS NOT NULL"
        " AND ap_node IN (SELECT address FROM interface WHERE device_id = ?) LIMIT 1",
        (device_id,),
    ).fetchone()
    return str(row[0])


def test_dedupe_two_interfaces_same_ap(
    client: TestClient, service: Service, db: sqlite3.Connection, clock: FixedClock
) -> None:
    mesh = next(n for n in _graph(client)["nodes"] if n["name"] == "mesh-node-upstairs")
    ap_mac = _ap_mac(db, mesh["id"])
    dev, _ = service.create_placeholder_device("user", "Dual-NIC box", "other")
    now = iso(clock())
    ids = []
    with db:
        for kind, mac in (("ethernet", "02:00:5e:00:53:e1"), ("wifi", "02:00:5e:00:53:e2")):
            cur = db.execute(
                "INSERT INTO interface(device_id, kind, address, ap_node, first_seen, last_seen)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (dev, kind, mac, ap_mac, now, now),
            )
            ids.append(cur.lastrowid)
    graph = _graph(client)
    mine = [e for e in _derived(graph) if _pair(e) == frozenset((dev, mesh["id"]))]
    # One edge per pair: first interface's id, wifi because one of them is wifi.
    assert mine == [
        {"id": f"ap-{ids[0]}", "source": dev, "target": mesh["id"], "kind": "wifi", "manual": False}
    ]
    pairs = [_pair(e) for e in _derived(graph)]
    assert len(pairs) == len(set(pairs))
    assert client.get("/api/v1/topology").json() == graph


def test_dedupe_manual_link_replaces_derived(client: TestClient) -> None:
    graph = _graph(client)
    derived = _derived(graph)[0]
    pair = _pair(derived)
    # Reversed direction: the pair is unordered.
    body = {"a_device_id": derived["target"], "b_device_id": derived["source"], "kind": "wired"}
    r = _post(client, "/topology/links", body)
    assert r.status_code == 200
    link_id = r.json()["link_id"]
    after = _graph(client)
    on_pair = [e for e in after["edges"] if _pair(e) == pair]
    assert [e["id"] for e in on_pair] == [link_id] and on_pair[0]["manual"] is True
    assert len(after["edges"]) == len(graph["edges"])
    assert client.get("/api/v1/topology").json() == after


def test_deleting_manual_link_restores_derived(client: TestClient) -> None:
    derived = _derived(_graph(client))[0]
    pair = _pair(derived)
    body = {"a_device_id": derived["source"], "b_device_id": derived["target"], "kind": "wired"}
    link_id = _post(client, "/topology/links", body).json()["link_id"]
    assert [e["id"] for e in _graph(client)["edges"] if _pair(e) == pair] == [link_id]
    assert _post(client, f"/topology/links/{link_id}/delete").status_code == 200
    after = [e for e in _graph(client)["edges"] if _pair(e) == pair]
    assert after == [derived]


def test_placeholder_flag(client: TestClient, service: Service) -> None:
    """``placeholder`` is true exactly for devices with no interfaces."""
    dev, _ = service.create_placeholder_device("user", "Closet switch", "switch")
    nodes = {n["id"]: n for n in _graph(client)["nodes"]}
    assert nodes[dev]["placeholder"] is True and nodes[dev]["online"] is False
    with service.lock:
        with_ifaces = {
            int(r[0]) for r in service.conn.execute("SELECT DISTINCT device_id FROM interface")
        }
    assert with_ifaces
    for i, n in nodes.items():
        assert n["placeholder"] is (i not in with_ifaces)
    api = {n["id"]: n for n in client.get("/api/v1/topology").json()["nodes"]}
    assert api[dev]["placeholder"] is True


# ------------------------------------------------------------------ pages


def test_topology_page_renders(client: TestClient) -> None:
    r = client.get("/topology")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    html = r.text
    assert "/static/cytoscape.min.js" in html and "/static/topology.js" in html
    assert 'aria-current="page">Topology' in html
    token = client.cookies.get("atlas_csrf")
    assert token and f'data-csrf="{token}"' in html
    assert "cdn" not in html.lower() and "unpkg" not in html
    for path in ("/static/cytoscape.min.js", "/static/topology.js"):
        assert client.get(path).status_code == 200


def test_topology_data_shape(client: TestClient) -> None:
    graph = _graph(client)
    assert set(graph) == {"nodes", "edges", "places"}
    assert graph == client.get("/api/v1/topology").json()
    devices = client.get("/api/v1/devices", params={"limit": 1000}).json()
    assert {n["id"] for n in graph["nodes"]} == {d["id"] for d in devices}
    ids = {n["id"] for n in graph["nodes"]}
    for n in graph["nodes"]:
        assert set(n) == NODE_KEYS and isinstance(n["online"], bool)
        assert isinstance(n["placeholder"], bool)
    for e in graph["edges"]:
        assert set(e) == EDGE_KEYS
        assert e["source"] in ids and e["target"] in ids and e["source"] != e["target"]


def test_topology_data_needs_no_api_token(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock
) -> None:
    settings = Settings(data_dir=tmp_path, api_token="test-api-token-123")
    app = create_app(settings, conn=db, clock=clock, start_background=False)
    with TestClient(app) as c:
        assert c.get("/api/v1/topology").status_code == 401
        assert c.get("/topology").status_code == 200
        assert c.get("/topology/data").status_code == 200
        a = app.state.service.create_placeholder_device("user", "S", "switch")[0]
        r = _post(c, "/topology/layout", {"positions": {str(a): [1, 2]}})
        assert r.status_code == 200


def _write_case(name: str, client: TestClient, service: Service) -> tuple[str, dict[str, Any]]:
    a, b = _two_devices(client)
    if name == "layout":
        return "/topology/layout", {"positions": {str(a): [5, 6]}}
    if name == "link":
        return "/topology/links", {"a_device_id": a, "b_device_id": b, "kind": "wired"}
    if name == "link_delete":
        service.add_link("user", a, b, "wired")
        with service.lock:
            link_id = int(service.conn.execute("SELECT max(id) FROM link").fetchone()[0])
        return f"/topology/links/{link_id}/delete", {}
    assert name == "placeholder"
    return "/topology/placeholder", {"name": "Switch", "type": "switch"}


def _state(service: Service) -> tuple[Any, ...]:
    with service.lock:
        c = service.conn
        return (
            _last_batch(service),
            c.execute("SELECT * FROM topology_layout ORDER BY device_id").fetchall(),
            c.execute("SELECT count(*) FROM link").fetchone()[0],
            c.execute("SELECT count(*) FROM device").fetchone()[0],
        )


@pytest.mark.parametrize("route", ["layout", "link", "link_delete", "placeholder"])
def test_csrf_required_on_topology_posts(client: TestClient, service: Service, route: str) -> None:
    url, body = _write_case(route, client, service)
    token = _token(client)
    before = _state(service)
    # Missing header, wrong header, a token in the body instead of the header.
    r = client.post(url, json=body)
    assert r.status_code == 403 and r.headers["content-type"].startswith("application/json")
    assert client.post(url, json=body, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post(url, json={**body, "csrf": token}).status_code == 403
    # Header but no cookie, or a short injected cookie.
    client.cookies.clear()
    assert client.post(url, json=body, headers={"X-CSRF-Token": token}).status_code == 403
    client.cookies.set("atlas_csrf", "x")
    assert client.post(url, json=body, headers={"X-CSRF-Token": "x"}).status_code == 403
    assert _state(service) == before
    client.cookies.clear()
    r = _post(client, url, body)
    assert r.status_code == 200, r.text
    assert _state(service) != before


# ---------------------------------------------------- shared layout writer


LAYOUT_ROUTES = ("api", "page")


def _put_layout(client: TestClient, route: str, raw: str) -> Any:
    headers = {"Content-Type": "application/json"}
    if route == "api":
        return client.put("/api/v1/topology/layout", content=raw, headers=headers)
    headers["X-CSRF-Token"] = _token(client)
    return client.post("/topology/layout", content=raw, headers=headers)


@pytest.mark.parametrize("route", LAYOUT_ROUTES)
def test_layout_writer_shared_behaviour(client: TestClient, service: Service, route: str) -> None:
    """Both routes: upsert, finite floats only, unknown id rolls the whole batch back."""
    a, b = _two_devices(client)
    r = _put_layout(client, route, json.dumps({"positions": {str(a): [1, 2]}}))
    assert r.status_code == 200 and r.json() == {"updated": 1}
    r = _put_layout(client, route, json.dumps({"positions": {str(a): [3, 4], str(b): [5, 6]}}))
    assert r.json() == {"updated": 2}
    # Unknown id after a valid one: 404 JSON, the valid one is not written.
    raw = json.dumps({"positions": {str(b): [99, 99], "999999": [1, 2]}})
    r = _put_layout(client, route, raw)
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
    for bad in ("NaN", "Infinity", "-Infinity"):
        r = _put_layout(client, route, f'{{"positions": {{"{a}": [{bad}, 1]}}}}')
        assert r.status_code == 422
    assert _put_layout(client, route, '{"positions": {"x": [1]}}').status_code == 422
    nodes = {n["id"]: n for n in _graph(client)["nodes"]}
    assert (nodes[a]["x"], nodes[a]["y"]) == (3, 4)
    assert (nodes[b]["x"], nodes[b]["y"]) == (5, 6)
    # A preference, never a logged change.
    with service.lock:
        n = service.conn.execute(
            "SELECT count(*) FROM change WHERE entity = 'topology_layout'"
        ).fetchone()[0]
    assert n == 0


def test_save_layout_is_one_function(service: Service) -> None:
    """The shared writer rejects non-finite values itself, not only via the request models."""
    from lanscape_atlas.web import api, pages, topology

    assert api.save_layout is topology.save_layout
    assert pages.save_layout is topology.save_layout
    with service.lock:
        dev = int(service.conn.execute("SELECT min(id) FROM device").fetchone()[0])
    with pytest.raises(ValueError):
        topology.save_layout(service, {dev: (float("nan"), 1.0)})
    assert topology.save_layout(service, {dev: (7.0, 8.0)}) == 1


# ------------------------------------------------- dashed-edge hiding (spec 3.3)

INFRA = {"router", "mesh_node", "ap"}


def _client_edge(client: TestClient, service: Service, dev_type: str) -> dict[str, Any]:
    """A derived edge whose client end is retyped to ``dev_type``; returns the edge."""
    graph = _graph(client)
    types = {n["id"]: n["type"] for n in graph["nodes"]}
    edge = next(e for e in _derived(graph) if (types[e["source"]] or "").lower() not in INFRA)
    service.update_device("user", edge["source"], {"type": dev_type})
    return edge


def _wire(client: TestClient, a: int, b: int) -> int:
    r = _post(client, "/topology/links", {"a_device_id": a, "b_device_id": b, "kind": "wired"})
    assert r.status_code == 200, r.text
    return int(r.json()["link_id"])


def _has_edge(client: TestClient, edge_id: str) -> bool:
    return any(e["id"] == edge_id for e in _graph(client)["edges"])


@pytest.mark.parametrize("sw_type", ["switch", "hub", "Switch ", " HUB"])
def test_wiring_to_switch_hides_dashed_edge(
    client: TestClient, service: Service, sw_type: str
) -> None:
    edge = _client_edge(client, service, "other")
    sw, _ = service.create_placeholder_device("user", "Unmanaged", sw_type)
    assert _has_edge(client, edge["id"])
    _wire(client, edge["source"], sw)
    assert not _has_edge(client, edge["id"])
    assert client.get("/api/v1/topology").json() == _graph(client)


@pytest.mark.parametrize("flip", [False, True])
def test_hiding_works_for_either_link_direction(
    client: TestClient, service: Service, flip: bool
) -> None:
    edge = _client_edge(client, service, "other")
    sw, _ = service.create_placeholder_device("user", "Unmanaged", "switch")
    a, b = (sw, edge["source"]) if flip else (edge["source"], sw)
    _wire(client, a, b)
    assert not _has_edge(client, edge["id"])


def test_hiding_applies_when_client_is_the_target_end(client: TestClient, service: Service) -> None:
    """The rule looks at either endpoint, since the derived direction is not reliable."""
    from lanscape_atlas.web.topology import dedupe_edges

    derived = [{"id": "ap-1", "source": 1, "target": 2, "kind": "wifi", "manual": False}]
    links = [{"id": 9, "source": 2, "target": 3, "kind": "wired", "manual": True}]
    types = {1: "router", 2: "laptop", 3: "switch"}
    assert dedupe_edges(links, derived, types) == links


def test_type_other_keeps_dashed_edge(client: TestClient, service: Service) -> None:
    edge = _client_edge(client, service, "other")
    peer, _ = service.create_placeholder_device("user", "Thing", "other")
    _wire(client, edge["source"], peer)
    assert _has_edge(client, edge["id"])


def test_mesh_node_wired_to_switch_keeps_backhaul(client: TestClient, service: Service) -> None:
    graph = _graph(client)
    mesh = next(n for n in graph["nodes"] if n["name"] == "mesh-node-upstairs")["id"]
    service.update_device("user", mesh, {"type": "mesh_node"})
    edge = next(e for e in _derived(graph) if mesh in (e["source"], e["target"]))
    sw, _ = service.create_placeholder_device("user", "Unmanaged", "switch")
    _wire(client, mesh, sw)
    assert _has_edge(client, edge["id"])
    # Same device retyped to something that is not infrastructure: its edges now hide.
    service.update_device("user", mesh, {"type": "other"})
    assert not _has_edge(client, edge["id"])


def test_deleting_the_switch_link_brings_the_edge_back(
    client: TestClient, service: Service
) -> None:
    edge = _client_edge(client, service, "other")
    sw, _ = service.create_placeholder_device("user", "Unmanaged", "switch")
    link_id = _wire(client, edge["source"], sw)
    assert not _has_edge(client, edge["id"])
    assert _post(client, f"/topology/links/{link_id}/delete").status_code == 200
    assert _has_edge(client, edge["id"])


def test_switch_without_uplink_still_hides(client: TestClient, service: Service) -> None:
    edge = _client_edge(client, service, "other")
    sw, _ = service.create_placeholder_device("user", "Detached", "switch")
    _wire(client, edge["source"], sw)
    graph = _graph(client)
    assert all(sw not in (e["source"], e["target"]) or e["manual"] for e in graph["edges"])
    assert not _has_edge(client, edge["id"])


def test_only_manual_links_count(
    client: TestClient, service: Service, db: sqlite3.Connection
) -> None:
    """A discovered (non-manual) ``link`` row to a switch does not hide the dashed edge."""
    edge = _client_edge(client, service, "other")
    sw, _ = service.create_placeholder_device("user", "Unmanaged", "switch")
    with db:
        db.execute(
            "INSERT INTO link(a_device_id, b_device_id, kind, source)"
            " VALUES (?, ?, 'wired', 'discovered')",
            (edge["source"], sw),
        )
    assert _has_edge(client, edge["id"])


# ------------------------------------------------------------------ places


def test_graph_has_place_id_and_places(client: TestClient, service: Service) -> None:
    graph = _graph(client)
    assert graph["places"] == [] and all(n["place_id"] is None for n in graph["nodes"])
    service.set_place("user", graph["nodes"][0]["id"], "Room A")
    service.set_place("user", graph["nodes"][1]["id"], "Room B")
    graph = _graph(client)
    with service.lock:
        ids = {r["name"]: r["id"] for r in service.conn.execute("SELECT id, name FROM place")}
    assert graph["places"] == [
        {"id": ids["Room A"], "name": "Room A"},
        {"id": ids["Room B"], "name": "Room B"},
    ]
    by_id = {n["id"]: n for n in graph["nodes"]}
    assert by_id[graph["nodes"][0]["id"]]["place_id"] == ids["Room A"]
    assert by_id[graph["nodes"][1]["id"]]["place_id"] == ids["Room B"]
    assert client.get("/api/v1/topology").json() == graph


def test_deleted_place_gives_null_place_id(
    client: TestClient, service: Service, db: sqlite3.Connection
) -> None:
    dev = _graph(client)["nodes"][0]["id"]
    service.set_place("user", dev, "Room A")
    with db:
        db.execute("DELETE FROM place")
    graph = _graph(client)
    assert graph["places"] == []
    assert next(n for n in graph["nodes"] if n["id"] == dev)["place_id"] is None


# ------------------------------------------------------------------ search


def _search(client: TestClient, q: str) -> Any:
    return client.get("/topology/search", params={"q": q})


def _named(service: Service, name: str, type_: str = "other") -> int:
    return service.create_placeholder_device("user", name, type_)[0]


def test_search_by_name_returns_shape(client: TestClient, service: Service) -> None:
    dev = _named(service, "Frobnicator Display", "tablet")
    r = _search(client, "frobnicator")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert r.json() == [{"device_id": dev, "name": "Frobnicator Display", "type": "tablet"}]


def test_search_by_ip_and_full_and_partial_mac(client: TestClient) -> None:
    graph = _graph(client)
    with_ip = _search(client, "192.0.2.80").json()
    assert len(with_ip) == 1
    dev = with_ip[0]["device_id"]
    assert any(n["id"] == dev for n in graph["nodes"])
    mac_hit = _search(client, "00:00:5e:00:53:80").json()
    assert [d["device_id"] for d in mac_hit] == [dev]
    partial = _search(client, "53:80").json()
    assert dev in [d["device_id"] for d in partial]


def test_search_ranking_order(client: TestClient, service: Service, db: sqlite3.Connection) -> None:
    # Created in an order that differs from the expected ranking.
    zeta = _named(service, "zzz Frob room")
    contains = _named(service, "The Frob")
    starts_b = _named(service, "frob B")
    starts_a = _named(service, "Frob a")
    got = [d["device_id"] for d in _search(client, "frob").json()]
    assert got == [starts_a, starts_b, contains, zeta]


def test_search_exact_mac_ranks_before_name_prefix(
    client: TestClient, service: Service, db: sqlite3.Connection
) -> None:
    mac = "02:00:5e:00:53:c1"
    owner = _named(service, "Zed")
    prefixed = _named(service, mac[:8] + " backup")
    with db:
        db.execute(
            "INSERT INTO interface(device_id, kind, address, first_seen, last_seen)"
            " VALUES (?, 'ethernet', ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (owner, mac),
        )
    assert [d["device_id"] for d in _search(client, mac.upper()).json()] == [owner]
    # A name-prefix match on the same text ranks below an exact MAC match.
    assert _search(client, mac[:8]).json()[0]["device_id"] == prefixed


def test_search_empty_and_whitespace(client: TestClient) -> None:
    assert _search(client, "").json() == []
    assert _search(client, "   ").json() == []
    assert client.get("/topology/search").json() == []


def test_search_length_cap(client: TestClient, service: Service) -> None:
    assert _search(client, "a" * 100).status_code == 200
    r = _search(client, "a" * 101)
    assert r.status_code == 422 and r.headers["content-type"].startswith("application/json")


def test_search_limit_20(client: TestClient, service: Service) -> None:
    for i in range(25):
        _named(service, f"Lamp {i:02d}")
    got = _search(client, "lamp").json()
    assert [d["name"] for d in got] == [f"Lamp {i:02d}" for i in range(20)]


@pytest.mark.parametrize("q", ["%", "_"])
def test_search_wildcards_are_literal(client: TestClient, service: Service, q: str) -> None:
    _named(service, "Plain Device")
    literal = _named(service, "50% off_sign")
    assert [d["device_id"] for d in _search(client, q).json()] == [literal]


def test_search_percent_in_middle_is_not_a_wildcard(client: TestClient, service: Service) -> None:
    _named(service, "Plain Device")
    assert _search(client, "p%e").json() == []
    assert _search(client, "p_ain").json() == []


def test_search_is_same_origin_page_route(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock
) -> None:
    """Like ``/topology/data``: GET, no CSRF token, no bearer token needed."""
    settings = Settings(data_dir=tmp_path, api_token="test-api-token-123")
    app = create_app(settings, conn=db, clock=clock, start_background=False)
    with TestClient(app) as c:
        assert c.get("/api/v1/devices", params={"q": "x"}).status_code == 401
        assert c.get("/topology/search", params={"q": "x"}).status_code == 200
        assert c.post("/topology/search").status_code in (403, 405)


def test_search_obeys_host_allowlist(
    tmp_path: Path, db: sqlite3.Connection, clock: FixedClock
) -> None:
    settings = Settings(data_dir=tmp_path, allowed_hosts=("testserver",))
    app = create_app(settings, conn=db, clock=clock, start_background=False)
    with TestClient(app) as c:
        assert c.get("/topology/search", params={"q": "x"}).status_code == 200
        r = c.get("/topology/search", params={"q": "x"}, headers={"Host": "evil.example"})
        assert r.status_code == 421


def test_rank_device_matches_is_reusable() -> None:
    from lanscape_atlas.service.queries import DeviceSummary
    from lanscape_atlas.web.search import MAX_QUERY_LEN, SEARCH_LIMIT, rank_device_matches

    def row(i: int, name: str | None, ipv4: str | None = None, macs: tuple[str, ...] = ()) -> Any:
        return DeviceSummary(
            id=i, name=name, type="other", icon=None, online=True, ipv4=ipv4, macs=list(macs),
            vendor=None, place=None, ap_node=None, band=None, service_count=0,
            last_seen="2026-01-01T00:00:00Z", trust="known",
        )  # fmt: skip

    rows = [
        row(1, "b cam"),
        row(2, None, "192.0.2.5"),
        row(3, "cam z"),
        row(4, "Cam a"),
        row(5, "x", macs=("00:00:5e:00:53:01",)),
    ]
    assert MAX_QUERY_LEN == 100 and SEARCH_LIMIT == 20
    matched = [r for r in rows if "cam" in (r.name or "").lower()]
    assert [r["device_id"] for r in rank_device_matches(matched, "cam")] == [4, 3, 1]
    assert [r["device_id"] for r in rank_device_matches(rows, " 192.0.2.5 ")][:1] == [2]
    assert [r["device_id"] for r in rank_device_matches(rows, "00:00:5E:00:53:01")][:1] == [5]
    assert len(rank_device_matches(rows, "cam", limit=2)) == 2
    assert rank_device_matches(rows, "   ") == []
    assert rank_device_matches(rows, "cam")[0] == {"device_id": 4, "name": "Cam a", "type": "other"}
