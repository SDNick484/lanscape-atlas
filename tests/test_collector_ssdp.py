from __future__ import annotations

from pathlib import Path

import pytest

from conftest import T0
from lanscape_atlas.collectors.ssdp import (
    MAX_FIELD_LEN,
    location_allowed,
    parse_device_description,
    parse_ssdp_response,
)
from lanscape_atlas.observation import Observation

RESP = (Path(__file__).parent / "fixtures" / "ssdp_responses.txt").read_bytes()
XML = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"><device>
<friendlyName>Example Speaker</friendlyName><manufacturer>Example Corp</manufacturer>
<modelName>ES-1</modelName></device></root>"""


def test_parse_ssdp_response() -> None:
    o = parse_ssdp_response(RESP, "192.0.2.20", T0)
    assert o is not None
    assert (o.source, o.mac, o.ipv4, o.hostname, o.iface_kind) == (
        "ssdp",
        None,
        "192.0.2.20",
        None,
        None,
    )
    assert o.attrs == {
        "server": "Linux/5.4 UPnP/1.0 ExampleServer/1.2",
        "location": "http://192.0.2.20:49152/desc.xml",
        "st": "upnp:rootdevice",
        "name_source": "ssdp",
    }


@pytest.mark.parametrize(
    "data", [b"", b"\xff\xfe garbage", b"M-SEARCH * HTTP/1.1\r\n\r\n", b"just text"]
)
def test_parse_ssdp_garbage_returns_none(data: bytes) -> None:
    assert parse_ssdp_response(data, "192.0.2.20", T0) is None


def test_parse_ssdp_bad_sender_returns_none() -> None:
    assert parse_ssdp_response(RESP, "not-an-ip", T0) is None


def test_parse_device_description() -> None:
    assert parse_device_description(XML) == {
        "friendlyName": "Example Speaker",
        "modelName": "ES-1",
        "manufacturer": "Example Corp",
    }


def test_parse_device_description_untrusted() -> None:
    assert parse_device_description("<root><unclosed>") == {}
    assert parse_device_description("<a>" + "x" * 70_000 + "</a>") == {}
    evil = '<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r><friendlyName>&x;</friendlyName></r>'
    assert parse_device_description(evil) == {}


def test_location_allowed() -> None:
    assert location_allowed("http://192.0.2.20:49152/d.xml", "192.0.2.20")
    assert location_allowed("https://192.0.2.20/d.xml", "192.0.2.20")
    assert not location_allowed("http://192.0.2.21/d.xml", "192.0.2.20")
    assert not location_allowed("file:///etc/passwd", "192.0.2.20")
    assert not location_allowed("http://evil.example/d.xml", "192.0.2.20")
    assert not location_allowed("garbage", "192.0.2.20")


def test_ssdp_friendly_name_becomes_hint(ing, db) -> None:  # type: ignore[no-untyped-def]
    arp = Observation("arp", T0, None, "00:00:5e:00:53:20", "192.0.2.20", None, {})
    ssdp = Observation(
        "ssdp", T0, None, None, "192.0.2.20", "Example Speaker", {"name_source": "ssdp"}
    )
    ing.ingest([arp, ssdp])
    hints = db.execute("select hints from interface").fetchone()[0]
    assert '"ssdp": "Example Speaker"' in hints


def test_parse_ssdp_truncates_long_headers() -> None:
    long = "x" * 1000
    data = (
        "HTTP/1.1 200 OK\r\n"
        f"SERVER: {long}\r\n"
        f"LOCATION: http://192.0.2.20/{long}\r\n"
        f"ST: urn:{long}\r\n\r\n"
    ).encode()
    o = parse_ssdp_response(data, "192.0.2.20", T0)
    assert o is not None
    for key in ("server", "location", "st"):
        value = o.attrs[key]
        assert isinstance(value, str) and len(value) == MAX_FIELD_LEN, key
    assert MAX_FIELD_LEN == 255


def test_parse_device_description_truncates_values() -> None:
    long = "y" * 1000
    xml = (
        '<root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
        f"<friendlyName>{long}</friendlyName><manufacturer>{long}</manufacturer>"
        f"<modelName>{long}</modelName></device></root>"
    )
    found = parse_device_description(xml)
    assert set(found) == {"friendlyName", "manufacturer", "modelName"}
    assert all(v == "y" * 255 for v in found.values())


async def test_truncated_location_is_not_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    from lanscape_atlas.collectors import ssdp

    long_loc = "http://192.0.2.20/" + "z" * 400
    short_loc = "http://192.0.2.21/d.xml"
    replies = [
        (f"HTTP/1.1 200 OK\r\nLOCATION: {loc}\r\nST: x\r\n\r\n".encode(), ip)
        for loc, ip in ((long_loc, "192.0.2.20"), (short_loc, "192.0.2.21"))
    ]
    fetched: list[str] = []

    async def fake_search(self: object) -> list[tuple[bytes, str]]:
        return replies

    async def fake_fetch(location: str) -> dict[str, str]:
        fetched.append(location)
        return {"friendlyName": "Speaker"}

    monkeypatch.setattr(ssdp.SsdpCollector, "_search", fake_search)
    monkeypatch.setattr(ssdp, "_fetch_description", fake_fetch)
    obs = await ssdp.SsdpCollector().collect()
    assert fetched == [short_loc]
    assert [(o.ipv4, o.hostname) for o in obs] == [("192.0.2.20", None), ("192.0.2.21", "Speaker")]
