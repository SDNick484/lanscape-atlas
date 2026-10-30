from datetime import UTC, datetime

from lanscape_atlas.clock import iso, parse_iso, utcnow
from lanscape_atlas.oui import OuiDB


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
