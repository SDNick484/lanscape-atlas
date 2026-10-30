from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime

import pytest

from conftest import T0, dump_state
from lanscape_atlas.events import EVENT_FIRST_SEEN
from lanscape_atlas.ingest import IngestResult
from lanscape_atlas.observation import Observation

MAC = "00:00:5e:00:53:01"
MAC2 = "00:00:5e:00:53:02"
RMAC = "02:00:5e:00:53:07"
IP = "192.0.2.10"
TV = "Living-Room-TV._googlecast._tcp.local"


def obs(**kw):
    base = dict(
        source="arp", seen_at=T0, iface_kind=None, mac=None, ipv4=None, hostname=None, attrs={}
    )
    return Observation(**{**base, **kw})


def device_rows(db):
    return [dict(r) for r in db.execute("select * from device order by id")]


def iface_rows(db):
    return [dict(r) for r in db.execute("select * from interface order by id")]


def name_of(db, device_id=1):
    return db.execute("select name from device where id = ?", (device_id,)).fetchone()[0]


# ------------------------------------------------------------------ brief tests


def test_new_mac_creates_device_and_event(ing, bus):
    r = ing.ingest([obs(mac="00:00:5E:00:53:01", ipv4=IP)])
    assert r.created == 1
    first = [e for e in bus.recorded if e.name == EVENT_FIRST_SEEN]
    assert [e.payload for e in first] == [{"device_id": 1, "mac": MAC, "is_random": False}]


def test_random_mac_flagged(ing, bus, db):
    ing.ingest([obs(mac=RMAC, ipv4=IP)])
    first = [e for e in bus.recorded if e.name == EVENT_FIRST_SEEN]
    assert first[-1].payload["is_random"] is True
    assert iface_rows(db)[0]["is_random"] == 1


def test_invalid_mac_dropped_not_raised(ing, caplog):
    with caplog.at_level(logging.DEBUG, logger="lanscape_atlas.ingest.ingestor"):
        r = ing.ingest([obs(mac="<incomplete>", ipv4=IP)])
    assert r.dropped == 1 and r.created == 0
    assert any("drop" in m.lower() for m in caplog.messages)


def test_ip_only_attaches_within_window(ing, clock, db):
    ing.ingest([obs(mac=MAC, ipv4=IP)])
    t = clock.advance(minutes=5)
    r = ing.ingest([obs(source="mdns", seen_at=t, ipv4=IP, hostname=TV)])
    assert r.unattached == 0 and r.updated == 1 and r.created == 0
    row = db.execute(
        "select interface_id, attached from observation where source='mdns'"
    ).fetchone()
    assert tuple(row) == (1, 1)


def test_ip_only_outside_window_unattached(ing, clock, db):
    ing.ingest([obs(mac=MAC, ipv4=IP)])
    t = clock.advance(minutes=11)
    r = ing.ingest([obs(source="mdns", seen_at=t, ipv4=IP, hostname=TV)])
    assert r.unattached == 1
    row = db.execute(
        "select interface_id, attached from observation where source='mdns'"
    ).fetchone()
    assert tuple(row) == (None, 0)


def test_unattached_retried_next_run(ing, clock, db):
    r1 = ing.ingest([obs(source="mdns", ipv4=IP, hostname=TV)])
    assert r1.unattached == 1 and r1.created == 0
    t = clock.advance(minutes=1)
    r2 = ing.ingest([obs(source="arp", seen_at=t, mac=MAC, ipv4=IP)])
    assert r2.created == 1 and r2.unattached == 0
    assert name_of(db) == "Living-Room-TV"
    assert db.execute("select count(*) from observation where attached=0").fetchone()[0] == 0


def test_unattached_older_than_window_not_retried(ing, clock, db):
    ing.ingest([obs(source="mdns", ipv4=IP, hostname=TV)])
    t = clock.advance(minutes=30)
    r = ing.ingest([obs(seen_at=t, mac=MAC, ipv4=IP)])
    assert r.unattached == 0
    assert name_of(db) == "ICANN IANA .10"
    assert db.execute("select attached from observation where source='mdns'").fetchone()[0] == 0


def test_name_precedence_mdns_over_dhcp(ing, db):
    ing.ingest(
        [
            obs(source="asusrouter", mac=MAC, ipv4=IP, hostname="android-tv"),
            obs(source="mdns", ipv4=IP, hostname=TV),
        ]
    )
    assert name_of(db) == "Living-Room-TV"
    actor = db.execute(
        "select actor from field_source where entity='device' and field='name'"
    ).fetchone()[0]
    assert actor == "collector:mdns"


def test_dhcp_name_then_vendor_fallback(ing, db, make_device):
    ing.ingest([obs(source="asusrouter", mac=MAC, ipv4=IP, hostname="nas")])
    assert name_of(db) == "nas"
    ing.ingest([obs(source="arp", mac=MAC2, ipv4="192.0.2.11")])
    assert name_of(db, 2) == "ICANN IANA .11"


def test_user_name_not_overwritten(ing, svc, db, clock):
    ing.ingest([obs(source="asusrouter", mac=MAC, ipv4=IP, hostname="nas")])
    svc.update_device("user", 1, {"name": "Basement NAS"})
    t = clock.advance(minutes=1)
    ing.ingest([obs(source="mdns", seen_at=t, ipv4=IP, hostname="nas-mdns.local")])
    assert name_of(db) == "Basement NAS"
    assert (
        db.execute("select count(*) from batch where actor like 'collector:%'").fetchone()[0] == 1
    )


def test_reingest_same_batch_is_idempotent(ing, db):
    batch = [obs(source="asusrouter", mac=MAC, ipv4=IP, hostname="nas")]
    ing.ingest(batch)
    snap = dump_state(db)
    n_obs = db.execute("select count(*) from observation").fetchone()[0]
    r = ing.ingest(batch)
    assert dump_state(db) == snap
    assert db.execute("select count(*) from observation").fetchone()[0] == n_obs == 1
    assert r == IngestResult(created=0, updated=0, unattached=0, dropped=0)


# ------------------------------------------------------------- ruling-driven tests


def test_duplicates_within_call_stored_once(ing, db):
    o = obs(mac=MAC, ipv4=IP)
    r = ing.ingest([o, o])
    assert r.created == 1
    assert db.execute("select count(*) from observation").fetchone()[0] == 1


def test_observation_persisted_with_payload(ing, db):
    o = obs(mac=MAC, ipv4=IP)
    ing.ingest([o])
    row = db.execute(
        "select source, seen_at, payload, interface_id, attached from observation"
    ).fetchone()
    assert tuple(row) == ("arp", "2026-01-01T00:00:00Z", o.to_json(), 1, 1)


def test_naive_seen_at_dropped(ing, db):
    r = ing.ingest([obs(mac=MAC, seen_at=datetime(2026, 1, 1))])
    assert r.dropped == 1 and r.created == 0
    assert device_rows(db) == []


def test_drop_reasons(ing, db):
    r = ing.ingest(
        [
            obs(ipv4="999.1.1.1"),  # invalid IPv4, only identifier
            obs(),  # neither MAC nor IPv4
            obs(mac="zz:zz", ipv4=IP),  # invalid MAC
        ]
    )
    assert r == IngestResult(created=0, updated=0, unattached=0, dropped=3)
    assert db.execute("select count(*) from observation").fetchone()[0] == 0


def test_invalid_ipv4_with_valid_mac_keeps_mac(ing, db):
    r = ing.ingest([obs(mac=MAC, ipv4="not-an-ip")])
    assert r.created == 1 and r.dropped == 0
    assert db.execute("select count(*) from ip_lease").fetchone()[0] == 0


def test_timestamps_and_last_seen_forward_only(ing, db, clock):
    ing.ingest([obs(mac=MAC, ipv4=IP)])
    t = clock.advance(minutes=3)
    ing.ingest([obs(mac=MAC, ipv4=IP, seen_at=t)])
    ing.ingest([obs(mac=MAC, ipv4=IP, seen_at=t.replace(minute=1), attrs={"x": 1})])
    d = device_rows(db)[0]
    i = iface_rows(db)[0]
    lease = dict(db.execute("select * from ip_lease").fetchone())
    for row in (d, i, lease):
        assert row["first_seen"] == "2026-01-01T00:00:00Z"
        assert row["last_seen"] == "2026-01-01T00:03:00Z"
    assert db.execute("select count(*) from ip_lease").fetchone()[0] == 1


def test_new_ip_opens_second_lease_without_closing(ing, db):
    ing.ingest([obs(mac=MAC, ipv4=IP)])
    ing.ingest([obs(mac=MAC, ipv4="192.0.2.20", attrs={"n": 2})])
    rows = db.execute("select ipv4, closed from ip_lease order by id").fetchall()
    assert [tuple(r) for r in rows] == [(IP, 0), ("192.0.2.20", 0)]


def test_iface_kind_rules(ing, db):
    ing.ingest([obs(mac=MAC, iface_kind="bogus")])
    assert iface_rows(db)[0]["kind"] == "unknown"
    ing.ingest([obs(mac=MAC, iface_kind="wifi", attrs={"n": 1})])
    assert iface_rows(db)[0]["kind"] == "wifi"
    ing.ingest([obs(mac=MAC, iface_kind="ethernet", attrs={"n": 2})])
    ing.ingest([obs(mac=MAC, iface_kind=None, attrs={"n": 3})])
    rows = iface_rows(db)
    assert len(rows) == 1 and rows[0]["kind"] == "wifi"


def test_vendor_set_only_when_empty(ing, db):
    ing.ingest([obs(mac=MAC)])
    assert iface_rows(db)[0]["vendor"] == "ICANN IANA"
    db.execute("update interface set vendor='Custom'")
    db.commit()
    ing.ingest([obs(mac=MAC, attrs={"n": 1})])
    assert iface_rows(db)[0]["vendor"] == "Custom"


def test_ap_node_and_band(ing, db):
    ing.ingest([obs(source="asusrouter", mac=MAC, attrs={"ap_node": "node-a", "band": "5g"})])
    i = iface_rows(db)[0]
    assert (i["ap_node"], i["band"]) == ("node-a", "5g")


def test_hints_per_source_and_junk_skipped(ing, db):
    ing.ingest(
        [
            obs(source="asusrouter", mac=MAC, ipv4=IP, hostname="nas"),
            obs(source="arp", mac=MAC, ipv4=IP, hostname="arp-name"),
            obs(source="ssdp", ipv4=IP, hostname="*"),
            obs(source="nmap", ipv4=IP, hostname="nas.example", attrs={"name_source": "rdns"}),
        ]
    )
    assert json.loads(iface_rows(db)[0]["hints"]) == {"dhcp": "nas", "rdns": "nas.example"}


def test_name_merges_hints_across_interfaces(ing, db, clock):
    ing.ingest([obs(source="asusrouter", mac=MAC, ipv4=IP, hostname="laptop-wired")])
    t = clock.advance(minutes=1)
    ing.ingest(
        [obs(source="asusrouter", seen_at=t, mac=MAC2, ipv4="192.0.2.11", hostname="laptop-wifi")]
    )
    # Move MAC2's interface onto device 1, as a Task 8 claim would.
    db.execute("update interface set device_id=1 where address=?", (MAC2,))
    db.execute("delete from device where id=2")
    db.commit()
    t = clock.advance(minutes=1)
    ing.ingest([obs(source="arp", seen_at=t, mac=MAC2, ipv4="192.0.2.11")])
    assert name_of(db) == "laptop-wifi"  # tie on dhcp: most recently seen interface wins
    t = clock.advance(minutes=1)
    ing.ingest([obs(source="mdns", seen_at=t, ipv4=IP, hostname="Laptop.local")])
    assert name_of(db) == "Laptop"  # mdns on the other interface outranks dhcp


def test_services_upsert(ing, db, clock):
    svcs = [{"port": 22, "proto": "tcp", "name": "ssh", "product": "OpenSSH", "version": "9.6"}]
    ing.ingest([obs(source="nmap", mac=MAC, ipv4=IP, attrs={"services": svcs})])
    t = clock.advance(minutes=2)
    svcs2 = [
        {"port": 22, "proto": "tcp", "name": "ssh", "product": "OpenSSH", "version": "9.7"},
        {"port": 80, "proto": "tcp"},
    ]
    ing.ingest([obs(source="nmap", seen_at=t, ipv4=IP, attrs={"services": svcs2})])
    rows = [
        tuple(r)
        for r in db.execute(
            "select device_id, port, proto, name, product, version, source, first_seen, last_seen"
            " from service order by port"
        )
    ]
    assert rows == [
        (
            1,
            22,
            "tcp",
            "ssh",
            "OpenSSH",
            "9.7",
            "nmap",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:02:00Z",
        ),
        (1, 80, "tcp", None, None, None, "nmap", "2026-01-01T00:02:00Z", "2026-01-01T00:02:00Z"),
    ]


def test_one_collector_batch_per_source(ing, db):
    ing.ingest(
        [
            obs(source="asusrouter", mac=MAC, ipv4=IP, hostname="nas"),
            obs(source="asusrouter", mac=MAC2, ipv4="192.0.2.11", hostname="printer"),
        ]
    )
    rows = db.execute("select actor from batch").fetchall()
    assert [r[0] for r in rows] == ["collector:dhcp"]  # actor = winning hint source
    assert db.execute("select count(distinct batch_id) from change").fetchone()[0] == 1


def test_no_batch_when_nothing_written(ing, db, svc):
    ing.ingest([obs(mac=MAC)])  # no hints, no IP -> no fallback name
    assert db.execute("select count(*) from batch").fetchone()[0] == 0
    assert name_of(db) is None


def test_events_published_after_commit(ing, bus, db):
    seen = []

    def handler(ev):
        seen.append((db.in_transaction, db.execute("select count(*) from device").fetchone()[0]))

    bus.subscribe(EVENT_FIRST_SEEN, handler)
    ing.ingest([obs(mac=MAC), obs(mac=MAC2)])
    assert seen == [(False, 2), (False, 2)]


def test_rollback_on_error(ing, db, bus, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(ing, "_apply_services", boom)
    try:
        ing.ingest([obs(mac=MAC, attrs={"services": [{"port": 1, "proto": "tcp"}]})])
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError")
    assert not db.in_transaction
    assert device_rows(db) == []
    assert db.execute("select count(*) from observation").fetchone()[0] == 0
    assert bus.recorded == []


def test_ingest_waits_for_service_lock(ing, svc):
    held = threading.Event()

    def holder():
        with svc.lock:
            held.set()
            time.sleep(0.3)

    th = threading.Thread(target=holder)
    th.start()
    held.wait()
    start = time.monotonic()
    r = ing.ingest([obs(mac=MAC)])
    elapsed = time.monotonic() - start
    th.join()
    assert elapsed >= 0.25
    assert r.created == 1


def test_older_observation_does_not_replace_latest_facts(ing, db, clock):
    t = clock.advance(minutes=5)
    ing.ingest(
        [
            obs(
                source="asusrouter",
                seen_at=t,
                mac=MAC,
                ipv4=IP,
                hostname="new-name",
                attrs={"ap_node": "node-b", "band": "5g"},
            )
        ]
    )
    ing.ingest(
        [
            obs(
                source="asusrouter",
                seen_at=T0,
                mac=MAC,
                ipv4=IP,
                hostname="old-name",
                attrs={"ap_node": "node-a", "band": "2g"},
            )
        ]
    )
    i = iface_rows(db)[0]
    assert (i["ap_node"], i["band"]) == ("node-b", "5g")
    assert json.loads(i["hints"]) == {"dhcp": "new-name"}
    assert i["first_seen"] == "2026-01-01T00:05:00Z"  # first_seen is set once
    assert name_of(db) == "new-name"


# ------------------------------------------------------------------ fix round 1


def _two_iface_device(ing, db, clock, hostnames):
    """Device 1 with interfaces MAC and MAC2 (MAC2 moved over as a Task 8 claim would)."""
    h1, h2 = hostnames
    ing.ingest([obs(source="asusrouter", mac=MAC, ipv4=IP, hostname=h1)])
    ing.ingest([obs(source="asusrouter", mac=MAC2, ipv4="192.0.2.11", hostname=h2)])
    db.execute("update interface set device_id=1 where address=?", (MAC2,))
    db.execute("delete from field_source where entity='device' and entity_id=2")
    db.execute("delete from device where id=2")
    db.commit()


def _poll(ing, clock, hostnames):
    h1, h2 = hostnames
    t = clock.advance(minutes=2)
    ing.ingest(
        [
            obs(source="asusrouter", seen_at=t, mac=MAC, ipv4=IP, hostname=h1),
            obs(source="asusrouter", seen_at=t, mac=MAC2, ipv4="192.0.2.11", hostname=h2),
        ]
    )


def _counts(db):
    return tuple(db.execute(f"select count(*) from {t}").fetchone()[0] for t in ("change", "batch"))


@pytest.mark.parametrize(
    "hostnames", [("laptop-wired", "laptop-wifi"), (None, None)], ids=["dhcp", "vendor"]
)
def test_two_interface_repoll_writes_no_changes(ing, db, clock, hostnames):
    _two_iface_device(ing, db, clock, hostnames)
    _poll(ing, clock, hostnames)
    before = _counts(db)
    name = name_of(db)
    _poll(ing, clock, hostnames)
    assert _counts(db) == before
    assert name_of(db) == name


def test_batch_actor_is_winning_hint_source(ing, db, clock):
    ing.ingest([obs(source="arp", mac=MAC)])  # no hint, no IP -> no name
    assert name_of(db) is None
    # A dhcp hint is already stored (as an earlier router poll would leave it).
    db.execute("""update interface set hints='{"dhcp": "nas"}'""")
    db.commit()
    t = clock.advance(minutes=1)
    ing.ingest([obs(source="arp", seen_at=t, mac=MAC, ipv4=IP)])
    assert name_of(db) == "nas"
    assert [r[0] for r in db.execute("select actor from batch")] == ["collector:dhcp"]


def test_vendor_fallback_actor(ing, db):
    ing.ingest([obs(source="arp", mac=MAC, ipv4=IP)])
    assert name_of(db) == "ICANN IANA .10"
    assert [r[0] for r in db.execute("select actor from batch")] == ["collector:vendor"]


@pytest.mark.parametrize(
    "hostname",
    ["00-00-5e-00-53-01.local", "0000.5e00.5301.local", "00005e005301.local"],
)
@pytest.mark.parametrize("source", ["mdns", "asusrouter"])
def test_mac_dot_local_is_junk_through_ingest(ing, db, source, hostname):
    ing.ingest([obs(source=source, mac=MAC, ipv4=IP, hostname=hostname)])
    assert json.loads(iface_rows(db)[0]["hints"]) == {}
    assert name_of(db) == "ICANN IANA .10"


def test_other_interface_mac_rejected_as_name(ing, db, clock):
    # MAC2's address arrives as a hint on interface MAC; same device -> junk.
    _two_iface_device(ing, db, clock, ("00-00-5e-00-53-02", None))
    t = clock.advance(minutes=1)
    ing.ingest([obs(source="arp", seen_at=t, mac=MAC, ipv4=IP)])
    assert name_of(db) == "ICANN IANA .10"
