from __future__ import annotations

import json

import pytest

from lanscape_atlas.service import queries as q

T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-01T01:00:00Z"
T2 = "2026-01-01T02:00:00Z"


def _iface(
    db,
    dev,
    address,
    *,
    kind="wifi",
    is_random=0,
    vendor=None,
    ap_node=None,
    band=None,
    hints=None,
    last_seen=T0,
):
    with db:
        cur = db.execute(
            "insert into interface(device_id,kind,address,is_random,vendor,ap_node,band,hints,"
            "first_seen,last_seen) values(?,?,?,?,?,?,?,?,?,?)",
            (
                dev,
                kind,
                address,
                is_random,
                vendor,
                ap_node,
                band,
                json.dumps(hints or {}),
                T0,
                last_seen,
            ),
        )
    return cur.lastrowid


def _lease(db, iface, ipv4, *, last_seen=T0, closed=0):
    with db:
        db.execute(
            "insert into ip_lease(interface_id,ipv4,first_seen,last_seen,closed) values(?,?,?,?,?)",
            (iface, ipv4, T0, last_seen, closed),
        )


def _service(db, dev, port, proto="tcp", name=None):
    with db:
        db.execute(
            "insert into service(device_id,port,proto,name,source,first_seen,last_seen)"
            " values(?,?,?,?,'nmap',?,?)",
            (dev, port, proto, name, T0, T0),
        )


@pytest.fixture
def house(db, make_device):
    """Two devices with a few interfaces, leases and services."""
    tv = make_device(name="Living Room TV", type="media", trust="known", online=1)
    i1 = _iface(
        db,
        tv,
        "00:00:5e:00:53:1a",
        kind="ethernet",
        vendor="Acme",
        last_seen=T1,
        hints={"mdns": "living-room-tv"},
    )
    i2 = _iface(
        db,
        tv,
        "00:00:5e:00:53:0b",
        vendor="WifiCo",
        ap_node="00:00:5e:00:53:f0",
        band="5",
        last_seen=T2,
    )
    _lease(db, i1, "192.0.2.10", last_seen=T1)
    _lease(db, i2, "192.0.2.11", last_seen=T2)
    _lease(db, i2, "192.0.2.99", last_seen=T0, closed=1)
    _service(db, tv, 8008, name="http")
    _service(db, tv, 8009)
    nas = make_device(name="nas", type="storage", trust="known")
    i3 = _iface(
        db,
        nas,
        "00:00:5e:00:53:20",
        kind="ethernet",
        vendor="Synology",
        hints={"dhcp": "DiskStation"},
    )
    _lease(db, i3, "192.0.2.20")
    _service(db, nas, 445)
    return {"tv": tv, "nas": nas}


def test_summary_derived_fields(db, house):
    s = {d.id: d for d in q.list_devices(db)}[house["tv"]]
    assert s.name == "Living Room TV"
    assert s.type == "media"
    assert s.online is True
    assert s.macs == ["00:00:5e:00:53:0b", "00:00:5e:00:53:1a"]
    assert s.ipv4 == "192.0.2.11"  # most recent open lease
    assert s.vendor == "WifiCo"  # most recently seen interface
    assert (s.ap_node, s.band) == ("00:00:5e:00:53:f0", "5")
    assert s.service_count == 2
    assert s.place is None
    assert s.trust == "known"


def test_summary_macs_exclude_non_mac_kinds(db, make_device):
    d = make_device()
    _iface(db, d, "00:00:5e:00:53:30", kind="ethernet")
    _iface(db, d, "00:00:5e:00:53:31", kind="bluetooth")
    _iface(db, d, "00:00:5e:00:53:32", kind="unknown")  # ARP-only: still a MAC
    _iface(db, d, "00:00:5e:00:53:33", kind="wifi")
    _iface(db, d, "0xdeadbeef", kind="zwave")
    (s,) = q.list_devices(db)
    assert s.macs == ["00:00:5e:00:53:30", "00:00:5e:00:53:32", "00:00:5e:00:53:33"]
    assert s.ipv4 is None


def test_ap_device_names(db, make_device):
    ap = make_device(name="Mesh Node")
    unnamed = make_device()
    _iface(db, ap, "00:00:5e:00:53:f0", kind="ethernet")
    _iface(db, unnamed, "00:00:5e:00:53:f1", kind="unknown")
    _iface(db, unnamed, "00:00:5e:00:53:f2", kind="bluetooth")
    got = q.ap_device_names(
        db, ["00:00:5e:00:53:f0", "00:00:5e:00:53:f1", "00:00:5e:00:53:f2", "00:00:5e:00:53:99"]
    )
    assert got == {"00:00:5e:00:53:f0": (ap, "Mesh Node"), "00:00:5e:00:53:f1": (unnamed, None)}
    assert q.ap_device_names(db, []) == {}


def test_list_devices_filters(db, house, svc):
    svc.set_place("user", house["nas"], "Office")
    assert [d.id for d in q.list_devices(db, place="Office")] == [house["nas"]]
    assert [d.id for d in q.list_devices(db, online=True)] == [house["tv"]]
    assert [d.id for d in q.list_devices(db, online=False)] == [house["nas"]]
    assert [d.id for d in q.list_devices(db, type="media")] == [house["tv"]]
    assert [d.id for d in q.list_devices(db, ap_node="00:00:5e:00:53:f0")] == [house["tv"]]
    assert [d.id for d in q.list_devices(db, q="disk")] == [house["nas"]]
    assert q.list_devices(db, trust="new") == []
    assert len(q.list_devices(db, limit=1)) == 1
    assert len(q.list_devices(db, limit=1, offset=1)) == 1
    assert q.list_devices(db, limit=1, offset=2) == []


def test_find_by_ipv4(db, house):
    assert [d.id for d in q.find_devices(db, "192.0.2.20")] == [house["nas"]]
    assert [d.id for d in q.find_devices(db, "192.0.2.10")] == [house["tv"]]
    # exact, not prefix
    assert q.find_devices(db, "192.0.2.1") == []


def test_find_by_full_mac_any_format(db, house):
    for query in ("00:00:5e:00:53:20", "00-00-5E-00-53-20", "00005e005320", "0000.5e00.5320"):
        assert [d.id for d in q.find_devices(db, query)] == [house["nas"]], query


def test_find_by_partial_mac(db, house):
    assert [d.id for d in q.find_devices(db, "53:1a")] == [house["tv"]]
    assert [d.id for d in q.find_devices(db, "53-1A")] == [house["tv"]]


def test_find_by_name_case_insensitive(db, house):
    assert [d.id for d in q.find_devices(db, "living ROOM")] == [house["tv"]]
    assert [d.id for d in q.find_devices(db, "NAS")] == [house["nas"]]


def test_find_by_vendor_and_hostname(db, house):
    assert [d.id for d in q.find_devices(db, "synology")] == [house["nas"]]
    assert [d.id for d in q.find_devices(db, "diskstation")] == [house["nas"]]
    assert [d.id for d in q.find_devices(db, "living-room-tv")] == [house["tv"]]


def test_find_no_match_and_blank(db, house):
    assert q.find_devices(db, "toaster") == []
    assert q.find_devices(db, "   ") == []


def test_find_treats_like_wildcards_literally(db, house):
    assert q.find_devices(db, "%") == []
    assert q.find_devices(db, "_") == []


def test_inbox_contains_new_and_random_only(db, make_device):
    new = make_device(name="new thing", trust="new")
    _iface(db, new, "00:00:5e:00:53:40")
    rnd = make_device(name="random phone", trust="known")
    _iface(db, rnd, "02:00:5e:00:53:41", is_random=1)
    mixed = make_device(name="mixed", trust="known")
    _iface(db, mixed, "02:00:5e:00:53:42", is_random=1)
    _iface(db, mixed, "00:00:5e:00:53:43")
    known = make_device(name="known", trust="known")
    _iface(db, known, "00:00:5e:00:53:44")
    make_device(name="placeholder", trust="known")  # no interfaces at all
    assert sorted(d.id for d in q.inbox(db)) == sorted([new, rnd])


def test_get_device(db, house, svc):
    svc.update_device("user", house["tv"], {"owner": "family", "notes": "wall mounted"})
    v = q.get_device(db, house["tv"])
    assert v.id == house["tv"]
    assert v.ipv4 == "192.0.2.11"
    assert (v.owner, v.notes, v.scan_policy) == ("family", "wall mounted", "normal")
    assert [i.address for i in v.interfaces] == ["00:00:5e:00:53:0b", "00:00:5e:00:53:1a"]
    assert v.interfaces[1].hints == {"mdns": "living-room-tv"}
    assert {(le.ipv4, le.closed) for le in v.leases} == {
        ("192.0.2.10", False),
        ("192.0.2.11", False),
        ("192.0.2.99", True),
    }
    assert [s.port for s in v.services] == [8008, 8009]
    assert v.field_sources == {"owner": ("user", T0), "notes": ("user", T0)}


def test_get_device_missing(db):
    with pytest.raises(KeyError):
        q.get_device(db, 42)


def test_list_services(db, house):
    assert [(s.device_id, s.port) for s in q.list_services(db)] == [
        (house["tv"], 8008),
        (house["tv"], 8009),
        (house["nas"], 445),
    ]
    assert [s.port for s in q.list_services(db, device_id=house["nas"])] == [445]
    assert [s.device_id for s in q.list_services(db, port=8008)] == [house["tv"]]


def test_recent_changes(db, svc, make_device):
    d = make_device(name="a", pos_x=1.0)
    b1 = svc.update_device("user", d, {"name": "b"})
    b2 = svc.update_device("claude", d, {"pos_x": None})
    changes = q.recent_changes(db)
    assert [(c.batch_id, c.actor, c.field, c.old_value, c.new_value) for c in changes] == [
        (b2, "claude", "pos_x", 1.0, None),
        (b1, "user", "name", "a", "b"),
    ]
    assert changes[0].at == T0
    assert [c.batch_id for c in q.recent_changes(db, actor="user")] == [b1]
    assert len(q.recent_changes(db, limit=1)) == 1


def test_find_by_ipv4_matches_current_holder_only(db, make_device):
    a = make_device(name="old holder")
    b = make_device(name="new holder")
    ia = _iface(db, a, "00:00:5e:00:53:50")
    ib = _iface(db, b, "00:00:5e:00:53:51")
    _lease(db, ia, "192.0.2.50", closed=1)
    _lease(db, ib, "192.0.2.50", last_seen=T1)
    assert [d.id for d in q.find_devices(db, "192.0.2.50")] == [b]


# ---------------------------------------------------------------- Ruling 45


def test_list_devices_q_uses_find_matcher(db, house):
    """list_devices(q=) finds by current IPv4 and full/partial MAC like find_devices."""
    assert [d.id for d in q.list_devices(db, q="192.0.2.20")] == [house["nas"]]
    assert [d.id for d in q.list_devices(db, q=" 192.0.2.11 ")] == [house["tv"]]
    assert q.list_devices(db, q="192.0.2.99") == []  # closed lease
    for query in ("00:00:5e:00:53:20", "00-00-5E-00-53-20", "0000.5e00.5320", "00005e005320"):
        assert [d.id for d in q.list_devices(db, q=query)] == [house["nas"]], query
    assert [d.id for d in q.list_devices(db, q="53-1A")] == [house["tv"]]
    assert [d.id for d in q.list_devices(db, q="5e00.531a")] == [house["tv"]]
    assert [d.id for d in q.list_devices(db, q="living")] == [house["tv"]]
    # Filters still combine with q.
    assert q.list_devices(db, q="192.0.2.20", online=True) == []


def test_find_by_partial_dotted_mac(db, house):
    assert [d.id for d in q.find_devices(db, "5e00.531a")] == [house["tv"]]
    assert [d.id for d in q.find_devices(db, "5E00.5320")] == [house["nas"]]


# ---------------------------------------------------------------- Ruling 44

T1_MINUS_5 = "2026-01-01T00:55:00Z"


@pytest.fixture
def mover(db, make_device):
    """A device whose interface moved from 192.0.2.60 (T0) to 192.0.2.61 (T1).

    The old lease was never closed (no other device took the IP).
    """
    d = make_device(name="mover", online=1)
    i = _iface(db, d, "00:00:5e:00:53:60", last_seen=T1)
    _lease(db, i, "192.0.2.60", last_seen=T0)
    _lease(db, i, "192.0.2.61", last_seen=T1)
    return d, i


def test_stale_open_lease_is_not_current(db, mover):
    d, _ = mover
    assert q.find_devices(db, "192.0.2.60") == []
    assert [x.id for x in q.find_devices(db, "192.0.2.61")] == [d]
    assert q.list_devices(db, q="192.0.2.60") == []
    (s,) = q.list_devices(db)
    assert s.ipv4 == "192.0.2.61"
    assert q.get_device(db, d).ipv4 == "192.0.2.61"


def test_stale_lease_ignored_even_when_most_recent_by_id(db, make_device):
    d = make_device(name="mover2", online=1)
    i = _iface(db, d, "00:00:5e:00:53:62", last_seen=T2)
    _lease(db, i, "192.0.2.63", last_seen=T2)
    _lease(db, i, "192.0.2.62", last_seen=T0)
    assert q.get_device(db, d).ipv4 == "192.0.2.63"
    assert q.find_devices(db, "192.0.2.62") == []


def test_lease_within_window_is_current(db, mover):
    d, i = mover
    _lease(db, i, "192.0.2.64", last_seen=T1_MINUS_5)
    assert [x.id for x in q.find_devices(db, "192.0.2.64")] == [d]
    # A wider window makes the hour-old lease current again.
    assert [x.id for x in q.find_devices(db, "192.0.2.60", window_min=120)] == [d]
    assert [x.id for x in q.list_devices(db, q="192.0.2.60", window_min=120)] == [d]
    assert q.list_devices(db, q="192.0.2.60", window_min=5) == []


def test_ingested_move_leaves_old_ip_unfindable(db, ing, clock):
    """End to end: the Ingestor leaves IP1's lease open; queries ignore it."""
    from lanscape_atlas.collectors.nmap import scan_targets
    from lanscape_atlas.observation import Observation

    def seen(ip):
        return Observation(
            source="arp",
            seen_at=clock(),
            iface_kind=None,
            mac="00:00:5e:00:53:65",
            ipv4=ip,
            hostname=None,
            attrs={},
        )

    ing.ingest([seen("192.0.2.65")])
    clock.advance(minutes=30)
    ing.ingest([seen("192.0.2.66")])
    open_leases = db.execute("select ipv4 from ip_lease where closed = 0 order by ipv4").fetchall()
    assert [r[0] for r in open_leases] == ["192.0.2.65", "192.0.2.66"]
    assert q.find_devices(db, "192.0.2.65") == []
    (d,) = q.find_devices(db, "192.0.2.66")
    assert d.ipv4 == "192.0.2.66"
    assert scan_targets(db) == ["192.0.2.66"]
