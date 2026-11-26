import logging
import os
from datetime import UTC, datetime

from lanscape_atlas.clock import iso, parse_iso, utcnow
from lanscape_atlas.oui import RELOAD_INTERVAL_S, OuiDB

HEADER = "Registry,Assignment,Organization Name,Organization Address\n"


def _write_oui(path, vendor: str, mtime_ns: int) -> None:
    path.write_text(HEADER + f"MA-L,00005E,{vendor},x\n", encoding="utf-8")
    os.utime(path, ns=(mtime_ns, mtime_ns))


def test_replaced_file_is_seen_after_the_check_interval(tmp_path):
    p = tmp_path / "oui.csv"
    _write_oui(p, "Old Vendor", 1_000_000_000)
    now = [0.0]
    db = OuiDB.load(p, clock=lambda: now[0])
    assert db.lookup("00:00:5e:00:53:01") == "Old Vendor"

    _write_oui(p, "New Vendor", 2_000_000_000)
    now[0] = RELOAD_INTERVAL_S - 1
    assert db.lookup("00:00:5e:00:53:01") == "Old Vendor"  # not checked again yet
    now[0] = RELOAD_INTERVAL_S
    assert db.lookup("00:00:5e:00:53:01") == "New Vendor"


def test_broken_file_keeps_the_loaded_table_and_warns_once(tmp_path, caplog):
    p = tmp_path / "oui.csv"
    _write_oui(p, "Old Vendor", 1_000_000_000)
    now = [0.0]
    db = OuiDB.load(p, clock=lambda: now[0])
    assert db.lookup("00:00:5e:00:53:01") == "Old Vendor"

    p.write_bytes(b"\xff\xfe not utf-8 \xff")
    os.utime(p, ns=(2_000_000_000, 2_000_000_000))
    with caplog.at_level(logging.WARNING, logger="lanscape_atlas.oui"):
        for step in (1, 2, 3):
            now[0] = step * RELOAD_INTERVAL_S
            assert db.lookup("00:00:5e:00:53:01") == "Old Vendor"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_first_update_is_seen_when_the_packaged_sample_was_in_use(tmp_path):
    now = [0.0]
    db = OuiDB.load_data_dir(tmp_path, clock=lambda: now[0])
    assert db.lookup("00:00:5e:00:53:01") == "ICANN IANA"

    _write_oui(tmp_path / "oui.csv", "Updated Vendor", 2_000_000_000)
    now[0] = RELOAD_INTERVAL_S
    assert db.lookup("00:00:5e:00:53:01") == "Updated Vendor"


def test_oui_lookup(tmp_path):
    p = tmp_path / "oui.csv"
    p.write_text(
        "Registry,Assignment,Organization Name,Organization Address\nMA-L,00005E,ICANN IANA,x\n"
    )
    db = OuiDB.load(p)
    assert db.lookup("00:00:5e:00:53:01") == "ICANN IANA"
    assert db.lookup("02:00:5e:00:53:01") is None


def test_packaged_sample_loads():
    from importlib.resources import as_file, files

    with as_file(files("lanscape_atlas") / "data" / "oui_sample.csv") as p:
        db = OuiDB.load(p)
    assert db.lookup("00:00:5e:00:53:01") == "ICANN IANA"


def test_clock_roundtrip():
    dt = datetime(2026, 10, 29, 12, 0, 5, tzinfo=UTC)
    assert iso(dt) == "2026-10-29T12:00:05Z"
    assert parse_iso("2026-10-29T12:00:05Z") == dt
    assert utcnow().tzinfo is not None
