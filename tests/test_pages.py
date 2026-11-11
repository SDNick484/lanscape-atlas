"""Server-rendered web UI (Jinja2 + HTMX): devices, device page, inbox, history, status."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import FixedClock
from lanscape_atlas.bluetooth.aggregator import Candidate
from lanscape_atlas.bluetooth.match import Identity
from lanscape_atlas.config import Settings
from lanscape_atlas.observation import Observation
from lanscape_atlas.service import Service
from lanscape_atlas.service import queries as q
from lanscape_atlas.web.app import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "fake_house.json"
XSS = "<script>alert(1)</script>"
XSS_ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"
EDITABLE = ("name", "type", "icon", "place", "owner", "notes", "trust", "scan_policy")


def _observations() -> list[Observation]:
    raw = json.loads(FIXTURE.read_text())
    return [Observation.from_json(json.dumps(d)) for d in raw]


def _make_app(tmp_path: Path, db: sqlite3.Connection, clock: FixedClock) -> FastAPI:
    # TestClient sends Host: testserver; LAN-only mode must allow it explicitly.
    settings = Settings(data_dir=tmp_path, allowed_hosts=("testserver",))
    app = create_app(settings, conn=db, clock=clock, start_background=False)
    assert app.state.ingestor.ingest(_observations()).dropped == 0
    return app


@pytest.fixture
def app(tmp_path: Path, db: sqlite3.Connection, clock: FixedClock) -> FastAPI:
    return _make_app(tmp_path, db, clock)


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
        assert client.get("/").status_code == 200
    token = client.cookies.get("atlas_csrf")
    assert token
    return token


def _post(client: TestClient, url: str, data: dict[str, Any] | None = None) -> Any:
    body = {"csrf": _token(client), **(data or {})}
    return client.post(url, data=body, follow_redirects=False)


def _device(service: Service, name: str) -> q.DeviceSummary:
    with service.lock:
        return next(d for d in q.list_devices(service.conn, limit=1000) if d.name == name)


def _view(service: Service, device_id: int) -> q.DeviceView:
    with service.lock:
        return q.get_device(service.conn, device_id)


def _device_form(view: q.DeviceView, **changes: str) -> dict[str, str]:
    current = {
        "name": view.name or "",
        "type": view.type or "",
        "icon": view.icon or "",
        "place": view.place or "",
        "owner": view.owner or "",
        "notes": view.notes or "",
        "trust": view.trust,
        "scan_policy": view.scan_policy,
    }
    form = {**current, **changes}
    form.update({f"orig_{k}": v for k, v in current.items()})
    return form


def _is_html(r: Any) -> bool:
    return str(r.headers["content-type"]).startswith("text/html")


# ------------------------------------------------------------ brief tests


def test_devices_page_lists_fake_house(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200 and _is_html(r)
    html = r.text
    assert html.count('class="device-row') == 44
    for name in ("Thermostat", "nas-01", "Kids-Tablet"):
        assert name in html
    assert "192.0.2.80" in html and "00:00:5e:00:53:80" in html
    for label in ("Devices", "Inbox", "Topology", "History", "Status"):
        assert f">{label}</a>" in html
    assert "/static/htmx.min.js" in html and "/static/app.css" in html
    # The random CSRF token can contain "cdn" by chance; check the page without it.
    page = html.replace(client.cookies["atlas_csrf"], "")
    assert "unpkg" not in page and "cdn" not in page.lower()
    assert 'class="dot online"' in html
    assert client.get("/static/htmx.min.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_device_page_shows_set_by_user_after_edit(client: TestClient, service: Service) -> None:
    dev = _device(service, "nas-01")
    view = _view(service, dev.id)
    r = _post(client, f"/devices/{dev.id}", _device_form(view, name="Hall NAS", place="Office"))
    assert r.status_code == 303
    assert r.headers["location"].endswith(f"/devices/{dev.id}")
    after = _view(service, dev.id)
    assert after.name == "Hall NAS" and after.place == "Office"
    # Untouched fields were not written, so the user does not take them over.
    assert set(after.field_sources) == {"name", "place_id"}
    page = client.get(f"/devices/{dev.id}")
    assert page.status_code == 200
    assert "set by user · 2026-01-01" in page.text
    assert 'value="Hall NAS"' in page.text


def test_inbox_lists_new_devices(client: TestClient, service: Service) -> None:
    r = client.get("/inbox")
    assert r.status_code == 200 and _is_html(r)
    assert r.text.count('class="inbox-item') == 44
    thermo = _device(service, "Thermostat")
    plug = _device(service, "smart-plug-1")
    assert _post(client, f"/inbox/{thermo.id}/known").status_code == 303
    assert _post(client, f"/inbox/{plug.id}/unwanted").status_code == 303
    assert _view(service, thermo.id).trust == "known"
    assert _view(service, plug.id).trust == "unwanted"
    r = _post(client, f"/inbox/{plug.id}/name", {"name": "Porch plug", "type": "plug"})
    assert r.status_code == 303
    v = _view(service, plug.id)
    assert (v.name, v.type) == ("Porch plug", "plug")
    html = client.get("/inbox").text
    assert html.count('class="inbox-item') == 42
    assert f'id="inbox-{thermo.id}"' not in html
    # The claim select offers known devices.
    assert f'<option value="{thermo.id}">Thermostat</option>' in html
    assert _post(client, f"/inbox/{plug.id}/maybe").status_code == 404


def test_history_undo_button_works(client: TestClient, service: Service) -> None:
    dev = _device(service, "pve-host")
    old = dev.name
    _post(client, f"/devices/{dev.id}", _device_form(_view(service, dev.id), name="Hypervisor"))
    with service.lock:
        batch = q.recent_changes(service.conn, limit=1)[0].batch_id
    html = client.get("/history").text
    assert f'id="batch-{batch}"' in html and "Hypervisor" in html
    assert f'action="/history/{batch}/undo"' in html
    r = _post(client, f"/history/{batch}/undo")
    assert r.status_code == 303 and r.headers["location"].endswith("/history")
    assert _view(service, dev.id).name == old
    html = client.get("/history").text
    original = html.split(f'id="batch-{batch}"', 1)[1].split("</section>", 1)[0]
    assert "disabled" in original and "undone" in original
    undo_batch = batch + 1
    undo_section = html.split(f'id="batch-{undo_batch}"', 1)[1].split("</section>", 1)[0]
    # Ruling 36a: an undo batch that is not itself undone can be undone (redo).
    assert f"undoes #{batch}" in undo_section and "disabled" not in undo_section
    # Undoing again: inline 422 message, not JSON.
    again = _post(client, f"/history/{batch}/undo")
    assert again.status_code == 422 and _is_html(again)
    assert "already undone" in again.text
    ids = [int(i) for i in re.findall(r'id="batch-(\d+)"', html)]
    assert ids == sorted(ids, reverse=True)
    # Redo: undoing the undo re-applies the edit; then the undo batch is disabled.
    assert _post(client, f"/history/{undo_batch}/undo").status_code == 303
    assert _view(service, dev.id).name == "Hypervisor"
    html = client.get("/history").text
    undo_section = html.split(f'id="batch-{undo_batch}"', 1)[1].split("</section>", 1)[0]
    assert "disabled" in undo_section
    # Actor filter.
    assert client.get("/history", params={"actor": "claude"}).text.count('class="batch') == 0


def test_history_error_keeps_actor_filter(client: TestClient, service: Service) -> None:
    dev = _device(service, "nas-01")
    b1 = service.update_device("user", dev.id, {"name": "A"})
    service.update_device("user", dev.id, {"name": "B"})
    # 409 conflict re-render keeps ?actor=user.
    r = client.post(
        f"/history/{b1}/undo",
        params={"actor": "user"},
        data={"csrf": _token(client), "actor": "user"},
        follow_redirects=False,
    )
    assert r.status_code == 409
    assert 'name="actor" value="user"' in r.text
    assert "collector:mdns" not in r.text  # still filtered
    # 422 (already undone) too; the form posts actor as a hidden field.
    b3 = service.update_device("user", dev.id, {"owner": "Sam"})
    service.undo("user", b3)
    html = client.get("/history", params={"actor": "user"}).text
    assert 'name="actor" value="user"' in html.split(f'action="/history/{b1}/undo"', 1)[1]
    r = _post(client, f"/history/{b3}/undo", {"actor": "user"})
    assert r.status_code == 422
    assert 'name="actor" value="user"' in r.text and "collector:mdns" not in r.text
    # Success redirects back to the filtered view.
    b4 = service.update_device("user", dev.id, {"notes": "n"})
    r = _post(client, f"/history/{b4}/undo", {"actor": "user"})
    assert r.status_code == 303 and r.headers["location"].endswith("/history?actor=user")


def test_deep_scan_button_disabled_for_never(client: TestClient, service: Service) -> None:
    dev = _device(service, "nas-01")
    html = client.get(f"/devices/{dev.id}").text
    assert re.search(r'<button[^>]*id="deep-scan"(?![^>]*disabled)[^>]*>', html)
    service.update_device("user", dev.id, {"scan_policy": "never"})
    html = client.get(f"/devices/{dev.id}").text
    assert re.search(r'<button[^>]*id="deep-scan"[^>]*disabled[^>]*>', html)
    r = _post(client, f"/devices/{dev.id}/deep-scan")
    assert r.status_code == 409 and _is_html(r)
    assert "scan_policy=never" in r.text


def test_status_shows_failing_collector(client: TestClient, db: sqlite3.Connection) -> None:
    with db:
        db.execute(
            "INSERT INTO collector_status(name, last_ok, last_error, consecutive_failures)"
            " VALUES ('arp', NULL, 'boom ***', 3), ('mdns', '2026-01-01T00:00:00Z', NULL, 0)"
        )
    r = client.get("/status")
    assert r.status_code == 200 and _is_html(r)
    arp = r.text.split('id="collector-arp"', 1)[1].split("</tr>", 1)[0]
    mdns = r.text.split('id="collector-mdns"', 1)[1].split("</tr>", 1)[0]
    assert "chip fail" in arp and "boom ***" in arp and ">3<" in arp
    assert "chip ok" in mdns and "chip fail" not in mdns
    assert "44" in r.text  # device count


# ------------------------------------------------------------------- CSRF


def test_csrf_cookie_attributes(client: TestClient, app: FastAPI) -> None:
    r = client.get("/")
    cookie = r.headers["set-cookie"].lower()
    assert "atlas_csrf=" in cookie and "samesite=strict" in cookie
    assert "httponly" in cookie and "path=/" in cookie and "secure" not in cookie
    token = client.cookies["atlas_csrf"]
    assert f'name="csrf" value="{token}"' in client.get("/inbox").text
    # Present cookie: not re-set.
    assert "set-cookie" not in client.get("/inbox").headers
    with TestClient(app, base_url="https://testserver") as tls:
        assert "secure" in tls.get("/").headers["set-cookie"].lower()


POST_ROUTES = (
    "device_save",
    "deep_scan",
    "inbox_known",
    "inbox_unwanted",
    "inbox_name",
    "claim",
    "undo",
    "find_claim",
    "ble_unclaim",
    "scanner_link",
)


class _NoopRunner:
    async def __call__(self, argv: list[str], timeout_s: float) -> str:
        return "<nmaprun/>"


def _post_case(name: str, service: Service, app: FastAPI) -> tuple[str, dict[str, str]]:
    """A valid (url, form) for each state-changing page POST, minus the token."""
    dev = _device(service, "Thermostat")
    if name == "device_save":
        return f"/devices/{dev.id}", _device_form(_view(service, dev.id), name="Renamed")
    if name == "deep_scan":
        app.state.deep_scan_runner = _NoopRunner()
        return f"/devices/{dev.id}/deep-scan", {}
    if name in ("inbox_known", "inbox_unwanted"):
        return f"/inbox/{dev.id}/{name.split('_')[1]}", {}
    if name == "inbox_name":
        return f"/inbox/{dev.id}/name", {"name": "Renamed"}
    if name == "claim":
        a, b = q.list_devices(service.conn, q="office-laptop", limit=10)
        iface = _view(service, b.id).interfaces[0].id
        return f"/interfaces/{iface}/claim", {"device_id": str(a.id)}
    if name == "scanner_link":
        service.upsert_scanners({"00:00:5e:00:53:aa": "proxy"}, service.clock())
        with service.lock:
            sid = int(service.conn.execute("SELECT id FROM ble_scanner").fetchone()[0])
        return f"/status/scanners/{sid}/link", {"device_id": str(dev.id)}
    if name in ("find_claim", "ble_unclaim"):
        return _ble_case(name, service, app, dev.id)
    assert name == "undo"
    batch = service.update_device("system", dev.id, {"owner": "Sam"})
    return f"/history/{batch}/undo", {}


def _ble_case(
    name: str, service: Service, app: FastAPI, device_id: int
) -> tuple[str, dict[str, str]]:
    ident = Identity("address", "00:00:5e:00:53:01")
    if name == "find_claim":

        class _Agg:
            def candidates(self) -> list[Candidate]:
                now = service.clock()
                return [Candidate(ident, "Tag", -50, "00:00:5e:00:53:aa", now, now, 1, None)]

        class _Runtime:
            aggregator = _Agg()

        app.state.bluetooth = _Runtime()
        return "/find/claim", {"identity": ident.key(), "device_id": str(device_id)}
    service.claim_ble("user", ident, device_id=device_id)
    with service.lock:
        iid = int(service.conn.execute("SELECT id FROM ble_identity").fetchone()[0])
    return f"/devices/{device_id}/ble/{iid}/unclaim", {}


def _last_batch(service: Service) -> int:
    with service.lock:
        return int(service.conn.execute("SELECT coalesce(max(id), 0) FROM batch").fetchone()[0])


@pytest.mark.parametrize("route", POST_ROUTES)
def test_csrf_required_on_every_post(
    client: TestClient, service: Service, app: FastAPI, route: str
) -> None:
    url, data = _post_case(route, service, app)
    token = _token(client)
    before = _last_batch(service)
    tasks_before = len(app.state.deep_scan_tasks)
    # Missing token, wrong token: 403 HTML, nothing written.
    r = client.post(url, data=data, follow_redirects=False)
    assert r.status_code == 403 and _is_html(r)
    r = client.post(url, data={**data, "csrf": "wrong"}, follow_redirects=False)
    assert r.status_code == 403
    # A token with no cookie, or a short injected cookie, is refused too.
    client.cookies.clear()
    assert client.post(url, data={**data, "csrf": token}).status_code == 403
    client.cookies.set("atlas_csrf", "x")
    assert client.post(url, data={**data, "csrf": "x"}).status_code == 403
    assert _last_batch(service) == before
    assert len(app.state.deep_scan_tasks) == tasks_before
    client.cookies.clear()
    # With the token (hidden field): the write goes through.
    r = _post(client, url, data)
    assert r.status_code == 303, (route, r.text[:500])
    if route != "deep_scan":
        assert _last_batch(service) > before


def test_csrf_header_works(client: TestClient, service: Service) -> None:
    dev = _device(service, "Thermostat")
    token = _token(client)
    r = client.post(
        f"/inbox/{dev.id}/known", headers={"X-CSRF-Token": token}, follow_redirects=False
    )
    assert r.status_code == 303
    assert _view(service, dev.id).trust == "known"


# -------------------------------------------------------------------- XSS


def test_untrusted_names_are_escaped(client: TestClient, service: Service) -> None:
    dev = _device(service, "Thermostat")
    service.update_device("user", dev.id, {"name": XSS, "notes": XSS, "owner": XSS})
    service.set_place("user", dev.id, XSS)
    service.update_device("user", _device(service, "nas-01").id, {"trust": "known"})
    for url in ("/", "/partials/devices", f"/devices/{dev.id}", "/inbox", "/history"):
        html = client.get(url).text
        assert XSS not in html, url
        assert XSS_ESCAPED in html, url


# ----------------------------------------------------------------- errors


def test_page_errors_are_html(client: TestClient) -> None:
    for url, status in (
        ("/devices/999999", 404),
        ("/devices/abc", 422),
        ("/no-such-page", 404),
    ):
        r = client.get(url)
        assert r.status_code == status, url
        assert _is_html(r), url
        assert "<!doctype html>" in r.text.lower()
    assert _post(client, "/devices/999999", {"name": "x"}).status_code == 404
    assert _post(client, "/history/999999/undo").status_code == 404
    # The API keeps JSON.
    r = client.get("/api/v1/devices/999999")
    assert r.status_code == 404 and r.json() == {"detail": "no device with id 999999"}
    assert client.get("/api/v1/nope").json() == {"detail": "Not Found"}
    assert client.get("/api/v1/devices/abc").status_code == 422
    assert isinstance(client.get("/api/v1/devices/abc").json()["detail"], list)


def test_device_form_value_error_rerenders(client: TestClient, service: Service) -> None:
    dev = _device(service, "nas-01")
    form = _device_form(_view(service, dev.id), name="Typed name", trust="maybe")
    r = _post(client, f"/devices/{dev.id}", form)
    assert r.status_code == 422 and _is_html(r)
    assert "trust must be one of" in r.text
    assert 'value="Typed name"' in r.text  # the user's input is kept
    assert _view(service, dev.id).name == "nas-01"


def test_device_form_clears_place_and_text(client: TestClient, service: Service) -> None:
    dev = _device(service, "nas-01")
    service.set_place("user", dev.id, "Closet")
    service.update_device("user", dev.id, {"owner": "Sam"})
    r = _post(
        client, f"/devices/{dev.id}", _device_form(_view(service, dev.id), place="", owner="")
    )
    assert r.status_code == 303
    v = _view(service, dev.id)
    assert v.place is None and v.owner is None
    # Nothing changed: no new batch.
    with service.lock:
        before = q.recent_changes(service.conn, limit=1)[0].batch_id
    assert _post(client, f"/devices/{dev.id}", _device_form(v)).status_code == 303
    with service.lock:
        assert q.recent_changes(service.conn, limit=1)[0].batch_id == before


def test_undo_conflict_lists_batches(client: TestClient, service: Service) -> None:
    dev = _device(service, "nas-01")
    b1 = service.update_device("user", dev.id, {"name": "A"})
    b2 = service.update_device("user", dev.id, {"name": "B"})
    r = _post(client, f"/history/{b1}/undo")
    assert r.status_code == 409 and _is_html(r)
    msg = r.text.split('class="flash error"', 1)[1].split("</", 1)[0]
    assert f"#{b2}" in msg
    assert _view(service, dev.id).name == "B"


def test_claim_from_inbox(client: TestClient, service: Service) -> None:
    a, b = (d for d in q.list_devices(service.conn, q="office-laptop", limit=10))
    service.update_device("user", a.id, {"trust": "known"})
    iface = _view(service, b.id).interfaces[0].id
    html = client.get("/inbox").text
    assert f'action="/interfaces/{iface}/claim"' in html
    # Refusal: claiming onto the device that already owns it shows inline (422).
    r = _post(client, f"/interfaces/{iface}/claim", {"device_id": str(b.id)})
    assert r.status_code == 422 and _is_html(r)
    assert "already belongs" in r.text
    r = _post(client, f"/interfaces/{iface}/claim", {"device_id": str(a.id)})
    assert r.status_code == 303 and r.headers["location"].endswith("/inbox")
    assert iface in {i.id for i in _view(service, a.id).interfaces}
    assert _post(client, f"/interfaces/{iface}/claim", {"device_id": "999999"}).status_code == 404
    assert _post(client, f"/interfaces/{iface}/claim", {"device_id": "x"}).status_code == 422


# ---------------------------------------------------------------- filters


def test_search_box_finds_by_ip_and_mac(client: TestClient, service: Service) -> None:
    """Ruling 45: the device search box matches IPv4 and MAC too."""
    with service.lock:
        d = next(r for r in q.list_devices(service.conn, limit=1000) if r.ipv4 and r.macs)
    page = client.get("/", params={"q": d.ipv4})
    assert page.status_code == 200
    assert page.text.count('class="device-row') == 1
    assert f'href="/devices/{d.id}"' in page.text
    part = client.get("/partials/devices", params={"q": d.macs[0].upper()})
    assert f'href="/devices/{d.id}"' in part.text
    assert 'placeholder="Search name, IP, MAC, vendor"' in page.text


def test_partial_keeps_filters(client: TestClient, service: Service) -> None:
    page = client.get("/", params={"q": "cam", "trust": "new", "online": "true"})
    assert page.status_code == 200
    m = re.search(r'hx-get="([^"]*/partials/devices[^"]*)"', page.text)
    assert m is not None
    url = m.group(1).replace("&amp;", "&")
    assert "q=cam" in url and "trust=new" in url and "online=true" in url
    assert 'hx-trigger="every 30s"' in page.text
    assert page.text.count('class="device-row') == 4
    part = client.get(url)
    assert part.status_code == 200
    assert "<html" not in part.text and "<nav" not in part.text
    assert part.text.count('class="device-row') == 4
    assert all("cam" in n.lower() for n in re.findall(r'class="name">([^<]*)<', part.text))
    off = client.get("/partials/devices", params={"online": "false"})
    assert off.text.count('class="device-row') == 0
    # Empty filter values from the GET form are "no filter".
    blank = client.get("/", params={"q": "", "type": "", "online": "", "trust": ""})
    assert blank.text.count('class="device-row') == 44
    assert client.get("/", params={"online": "perhaps"}).status_code == 422


def test_device_page_tables_and_deep_scan_start(
    client: TestClient, service: Service, app: FastAPI
) -> None:
    class FakeRunner:
        calls: list[list[str]] = []

        async def __call__(self, argv: list[str], timeout_s: float) -> str:
            self.calls.append(argv)
            return "<nmaprun/>"

    runner = FakeRunner()
    app.state.deep_scan_runner = runner
    dev = _device(service, "nas-01")
    html = client.get(f"/devices/{dev.id}").text
    assert "Interfaces" in html and "IP history" in html and "Services" in html
    assert dev.ipv4 is not None and dev.ipv4 in html
    r = _post(client, f"/devices/{dev.id}/deep-scan")
    assert r.status_code == 303 and "scan=started" in r.headers["location"]
    deadline = time.monotonic() + 5
    while not runner.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runner.calls and runner.calls[0][-1] == dev.ipv4
    assert "Deep scan started" in client.get(r.headers["location"]).text


# ------------------------------------------------------------------ AP node


def test_ap_node_shows_owning_device(
    client: TestClient, service: Service, db: sqlite3.Connection
) -> None:
    mesh = _device(service, "mesh-node-upstairs")
    thermo = _device(service, "Thermostat")
    assert thermo.ap_node == "00:00:5e:00:53:02"
    link = f'<a href="/devices/{mesh.id}" title="00:00:5e:00:53:02">mesh-node-upstairs</a>'
    page = client.get("/")
    row = page.text.split(f'id="device-{thermo.id}"', 1)[1].split("</tr>", 1)[0]
    assert link in row
    part = client.get("/partials/devices", params={"q": "thermostat"}).text
    assert link in part
    assert link in client.get(f"/devices/{thermo.id}").text
    # The ap_node filter still takes the MAC.
    f = client.get("/", params={"ap_node": "00:00:5e:00:53:02"}).text
    assert f'id="device-{thermo.id}"' in f
    assert 'value="00:00:5e:00:53:02"' in f
    # An AP MAC no device owns renders as plain text.
    with db:
        db.execute(
            "UPDATE interface SET ap_node = '00:00:5e:00:53:fe' WHERE device_id = ?", (thermo.id,)
        )
    row = client.get("/").text.split(f'id="device-{thermo.id}"', 1)[1].split("</tr>", 1)[0]
    assert "00:00:5e:00:53:fe" in row and 'title="00:00:5e:00:53:fe"' not in row
    dev_html = client.get(f"/devices/{thermo.id}").text
    assert "00:00:5e:00:53:fe" in dev_html and 'title="00:00:5e:00:53:fe"' not in dev_html


def test_arp_only_router_shows_mac(client: TestClient, service: Service) -> None:
    router = _device(service, "ICANN IANA .1")
    assert router.macs == ["00:00:5e:00:53:01"]
    row = client.get("/").text.split(f'id="device-{router.id}"', 1)[1].split("</tr>", 1)[0]
    assert "00:00:5e:00:53:01" in row
