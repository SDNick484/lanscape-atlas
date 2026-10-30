"""Task 8: IP reuse, online/offline presence and observation pruning."""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import T0, dump_state
from lanscape_atlas.events import EVENT_OFFLINE, EVENT_ONLINE
from lanscape_atlas.observation import Observation

MAC_A = "00:00:5e:00:53:0a"
MAC_B = "00:00:5e:00:53:0b"
IP = "192.0.2.20"
IP2 = "192.0.2.21"


def obs(**kw):
    base = dict(
        source="arp", seen_at=T0, iface_kind=None, mac=None, ipv4=None, hostname=None, attrs={}
    )
    return Observation(**{**base, **kw})


def leases(db):
    return [
        (r["address"], r["ipv4"], r["first_seen"], r["last_seen"], r["closed"])
        for r in db.execute(
            "select i.address, l.ipv4, l.first_seen, l.last_seen, l.closed"
            " from ip_lease l join interface i on i.id = l.interface_id order by l.id"
        )
    ]


def open_owner(db, ipv4=IP):
    rows = db.execute(
        "select i.address from ip_lease l join interface i on i.id = l.interface_id"
        " where l.ipv4 = ? and l.closed = 0",
        (ipv4,),
    ).fetchall()
    assert len(rows) <= 1, rows
    return rows[0][0] if rows else None


def presence_events(bus):
    return [(e.name, e.payload) for e in bus.recorded if e.name in (EVENT_ONLINE, EVENT_OFFLINE)]


def online(db, device_id):
    return db.execute("select online from device where id = ?", (device_id,)).fetchone()[0]


# ------------------------------------------------------------------ IP reuse


def test_ip_moves_to_new_mac_closes_old_lease(ing, db, clock):
    ing.ingest([obs(mac=MAC_A, ipv4=IP)])
    t1 = T0 + timedelta(hours=1)
    clock.now = t1
    ing.ingest([obs(mac=MAC_B, ipv4=IP, seen_at=t1)])
    assert leases(db) == [
        (MAC_A, IP, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", 1),  # last_seen kept
        (MAC_B, IP, "2026-01-01T01:00:00Z", "2026-01-01T01:00:00Z", 0),
    ]
    # No merge: two devices.
    assert db.execute("select count(*) from device").fetchone()[0] == 2


def test_same_run_conflict_latest_seen_wins(ing, db):
    # Router (later) listed first; ARP (earlier) second: processing is by seen_at.
    router = obs(source="asusrouter", mac=MAC_B, ipv4=IP, seen_at=T0 + timedelta(seconds=30))
    arp = obs(source="arp", mac=MAC_A, ipv4=IP, seen_at=T0)
    ing.ingest([router, arp])
    assert open_owner(db) == MAC_B
    assert (MAC_A, IP, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", 1) in leases(db)


def test_same_seen_at_ties_keep_input_order(ing, db):
    ing.ingest([obs(mac=MAC_A, ipv4=IP), obs(source="asusrouter", mac=MAC_B, ipv4=IP)])
    assert open_owner(db) == MAC_B
    ing2 = [obs(source="x", mac=MAC_B, ipv4=IP2), obs(source="y", mac=MAC_A, ipv4=IP2)]
    ing.ingest(ing2)
    assert open_owner(db, IP2) == MAC_A


def test_older_observation_does_not_steal_lease_back(ing, db, clock):
    t1 = T0 + timedelta(minutes=5)
    clock.now = t1
    ing.ingest([obs(source="asusrouter", mac=MAC_B, ipv4=IP, seen_at=t1)])
    # A late ARP report from before B took the address.
    ing.ingest([obs(source="arp", mac=MAC_A, ipv4=IP, seen_at=T0)])
    assert open_owner(db) == MAC_B
    assert leases(db) == [
        (MAC_B, IP, "2026-01-01T00:05:00Z", "2026-01-01T00:05:00Z", 0),
        (MAC_A, IP, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", 1),  # history only
    ]


def test_owner_seen_again_reopens_new_lease(ing, db, clock):
    ing.ingest([obs(mac=MAC_A, ipv4=IP)])
    clock.advance(minutes=5)
    ing.ingest([obs(mac=MAC_B, ipv4=IP, seen_at=clock())])
    clock.advance(minutes=5)
    ing.ingest([obs(mac=MAC_A, ipv4=IP, seen_at=clock())])
    assert open_owner(db) == MAC_A
    assert [row[4] for row in leases(db)] == [1, 1, 0]


def test_ip_only_observation_attaches_to_current_owner(ing, db):
    ing.ingest(
        [
            obs(mac=MAC_A, ipv4=IP),
            obs(source="asusrouter", mac=MAC_B, ipv4=IP, seen_at=T0 + timedelta(seconds=30)),
            obs(source="mdns", ipv4=IP, hostname="Kitchen", seen_at=T0 + timedelta(seconds=40)),
        ]
    )
    iface = db.execute(
        "select i.address from observation o join interface i on i.id = o.interface_id"
        " where o.source = 'mdns'"
    ).fetchone()[0]
    assert iface == MAC_B


# ------------------------------------------------------------------ presence


def test_new_device_goes_online(ing, db, bus):
    ing.ingest([obs(mac=MAC_A)])
    assert online(db, 1) == 1
    assert presence_events(bus) == [(EVENT_ONLINE, {"device_id": 1})]


def test_goes_offline_after_grace(ing, db, bus, clock):
    ing.ingest([obs(mac=MAC_A)])
    ing.refresh_presence(T0 + timedelta(minutes=10))  # boundary: still online
    assert online(db, 1) == 1
    clock.now = T0 + timedelta(minutes=10, seconds=1)
    ing.refresh_presence()
    assert online(db, 1) == 0
    assert presence_events(bus) == [
        (EVENT_ONLINE, {"device_id": 1}),
        (EVENT_OFFLINE, {"device_id": 1}),
    ]


def test_phone_grace_is_30_min(ing, db, svc, clock):
    ing.ingest([obs(mac=MAC_A), obs(mac=MAC_B)])
    svc.update_device("user", 1, {"type": "phone"})
    clock.now = T0 + timedelta(minutes=29)
    ing.refresh_presence()
    assert (online(db, 1), online(db, 2)) == (1, 0)
    clock.now = T0 + timedelta(minutes=31)
    ing.refresh_presence()
    assert online(db, 1) == 0


@pytest.mark.parametrize("dtype", ["tablet", "laptop"])
def test_tablet_and_laptop_grace_is_30_min(ing, db, svc, dtype):
    ing.ingest([obs(mac=MAC_A)])
    svc.update_device("user", 1, {"type": dtype})
    ing.refresh_presence(T0 + timedelta(minutes=29))
    assert online(db, 1) == 1


def test_online_event_only_on_transition(ing, db, bus, clock):
    ing.ingest([obs(mac=MAC_A)])
    clock.advance(minutes=1)
    ing.ingest([obs(mac=MAC_A, seen_at=clock())])
    ing.refresh_presence()
    ing.refresh_presence()
    assert presence_events(bus) == [(EVENT_ONLINE, {"device_id": 1})]
    clock.advance(minutes=30)
    ing.refresh_presence()
    ing.refresh_presence()
    clock.advance(minutes=1)
    ing.ingest([obs(mac=MAC_A, seen_at=clock())])
    assert presence_events(bus) == [
        (EVENT_ONLINE, {"device_id": 1}),
        (EVENT_OFFLINE, {"device_id": 1}),
        (EVENT_ONLINE, {"device_id": 1}),
    ]


def test_presence_uses_max_interface_last_seen(ing, db, svc, clock):
    ing.ingest([obs(mac=MAC_A), obs(mac=MAC_B, seen_at=T0 + timedelta(minutes=8))])
    svc.claim_interface("user", 2, 1)
    clock.now = T0 + timedelta(minutes=15)
    ing.refresh_presence()
    assert online(db, 1) == 1  # MAC_B, seen at +8 min, is within 10 min


def test_placeholder_never_online_no_events(ing, db, svc, bus, make_device):
    dev, _ = svc.create_placeholder_device("user", "Switch", "switch")
    stale = make_device(online=1)  # interface-less but marked online
    bus.recorded.clear()
    ing.refresh_presence()
    assert (online(db, dev), online(db, stale)) == (0, 0)
    assert presence_events(bus) == []


def test_presence_not_logged_in_change(ing, db, clock):
    ing.ingest([obs(mac=MAC_A)])
    before = db.execute("select count(*) from change").fetchone()[0]
    clock.advance(hours=1)
    ing.refresh_presence()
    assert online(db, 1) == 0
    assert db.execute("select count(*) from change").fetchone()[0] == before
    assert db.execute("select count(*) from field_source where field = 'online'").fetchone()[0] == 0


def test_presence_events_published_after_commit(ing, db, bus, clock):
    seen = []
    bus.subscribe(EVENT_ONLINE, lambda e: seen.append((db.in_transaction, online(db, 1))))
    bus.subscribe(EVENT_OFFLINE, lambda e: seen.append((db.in_transaction, online(db, 1))))
    ing.ingest([obs(mac=MAC_A)])
    clock.advance(hours=1)
    ing.refresh_presence()
    assert seen == [(False, 1), (False, 0)]


def test_refresh_presence_rejects_open_transaction(ing, db):
    db.execute("BEGIN")
    try:
        with pytest.raises(RuntimeError):
            ing.refresh_presence()
    finally:
        db.rollback()


def test_reingest_is_still_idempotent_with_presence(ing, db):
    batch = [obs(mac=MAC_A, ipv4=IP), obs(source="asusrouter", mac=MAC_B, ipv4=IP)]
    ing.ingest(batch)
    snap = dump_state(db)
    ing.ingest(batch)
    assert dump_state(db) == snap


# ------------------------------------------------------------------ pruning


def test_prune_observations(ing, svc, db, clock):
    ing.ingest(
        [
            obs(mac=MAC_A, seen_at=T0),
            obs(mac=MAC_A, seen_at=T0 + timedelta(days=5)),
            obs(mac=MAC_B, seen_at=T0 + timedelta(days=9)),
        ]
    )
    clock.now = T0 + timedelta(days=10)
    changes = db.execute("select count(*) from change").fetchone()[0]
    assert svc.prune_observations(6) == 1  # only the day-0 one is older than day 4
    remaining = [r[0] for r in db.execute("select seen_at from observation order by seen_at")]
    assert remaining == ["2026-01-06T00:00:00Z", "2026-01-10T00:00:00Z"]
    assert svc.prune_observations(6) == 0
    assert db.execute("select count(*) from change").fetchone()[0] == changes
    assert not db.in_transaction


def test_prune_observations_rejects_negative(svc):
    with pytest.raises(ValueError):
        svc.prune_observations(-1)
