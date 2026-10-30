"""The fake-house fixture ingests cleanly and the ingest-file CLI works on it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lanscape_atlas.cli import main
from lanscape_atlas.ingest import Ingestor
from lanscape_atlas.netutil import is_random_mac
from lanscape_atlas.observation import Observation
from lanscape_atlas.service import Service
from lanscape_atlas.service import queries as q

FIXTURE = Path(__file__).parent / "fixtures" / "fake_house.json"


def _observations() -> list[Observation]:
    raw = json.loads(FIXTURE.read_text())
    assert isinstance(raw, list) and all(isinstance(d, dict) for d in raw)
    return [Observation.from_json(json.dumps(d)) for d in raw]


def test_fixture_shape() -> None:
    obs = _observations()
    assert {o.source for o in obs} == {"asusrouter", "arp", "mdns", "ssdp", "nmap"}
    times = [o.seen_at for o in obs]
    assert min(times).isoformat().startswith("2026-01-01T00:00:00")
    assert (max(times) - min(times)).total_seconds() < 600
    assert {o.attrs.get("band") for o in obs if o.source == "asusrouter"} >= {"2.4", "5", "6"}
    assert any(o.source == "ssdp" and o.attrs.get("name_source") for o in obs)
    assert any(o.source == "nmap" and o.attrs.get("services") for o in obs)
    assert any(o.source == "mdns" and o.attrs.get("services") for o in obs)


def test_fake_house_ingests(ing: Ingestor, svc: Service, db) -> None:  # type: ignore[no-untyped-def]
    res = ing.ingest(_observations())
    assert res.dropped == 0
    devices = q.list_devices(db)
    assert len(devices) >= 40
    views = [q.get_device(db, d.id) for d in devices]
    ifaces = [i for v in views for i in v.interfaces]
    assert sum(1 for i in ifaces if is_random_mac(i.address)) >= 3
    assert {i.band for i in ifaces} >= {"2.4", "5", "6"}

    # The wired+wifi laptop appears as two devices until the user claims one.
    pair = [v for v in views if v.name and v.name.lower().startswith("office-laptop")]
    assert len(pair) == 2
    a, b = pair
    svc.claim_interface("user", b.interfaces[0].id, a.id)
    assert len(q.get_device(db, a.id).interfaces) == 2


def test_ingest_file_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LANSCAPE_DATA_DIR", str(tmp_path))
    assert main(["ingest-file", str(FIXTURE)]) == 0
    out = capsys.readouterr().out
    assert "created" in out and "dropped=0" in out
    assert (tmp_path / "atlas.db").exists()


def test_ingest_file_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LANSCAPE_DATA_DIR", str(tmp_path))
    assert main(["ingest-file", str(tmp_path / "nope.json")]) == 1
    assert "nope.json" in capsys.readouterr().err
