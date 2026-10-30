from __future__ import annotations

import json
import sqlite3

import pytest

from lanscape_atlas.events import EVENT_CHANGE_APPLIED, EventBus
from lanscape_atlas.service import Service, UndoConflict
from lanscape_atlas.store.db import migrate


def _source(svc, entity, entity_id, field):
    row = svc.conn.execute(
        "select actor, set_at from field_source where entity=? and entity_id=? and field=?",
        (entity, entity_id, field),
    ).fetchone()
    return None if row is None else tuple(row)


def _all_sources(svc, entity, entity_id):
    return svc.conn.execute(
        "select field from field_source where entity=? and entity_id=?", (entity, entity_id)
    ).fetchall()


def _name(svc, device_id):
    return svc.conn.execute("select name from device where id=?", (device_id,)).fetchone()[0]


def test_undo_restores_value_and_source(svc, make_device, clock, events):
    d = make_device(name="Espressif .23")
    b1 = svc.update_device("user", d, {"name": "Garage Sensor"})
    t1 = _source(svc, "device", d, "name")
    clock.advance(minutes=5)
    b2 = svc.update_device("claude", d, {"name": "Wrong Name", "notes": "guess"})
    assert _source(svc, "device", d, "name")[0] == "claude"

    u = svc.undo("user", b2)

    assert u > b2
    assert _name(svc, d) == "Garage Sensor"
    assert _source(svc, "device", d, "name") == t1  # actor and set_at restored exactly
    assert _source(svc, "device", d, "notes") is None  # no row existed before b2
    row = svc.conn.execute("select actor, undoes_batch_id from batch where id=?", (u,)).fetchone()
    assert tuple(row) == ("user", b2)
    assert events.recorded[-1].name == EVENT_CHANGE_APPLIED
    assert events.recorded[-1].payload == {"batch_id": u, "actor": "user"}
    assert b1 < b2


def test_undo_conflict_when_later_batch_touched_field(svc, make_device):
    d = make_device(name="Espressif .23")
    b1 = svc.update_device("claude", d, {"name": "Thermostat"})
    b2 = svc.update_device("user", d, {"name": "Hall Thermostat"})
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", b1)
    assert exc.value.conflicting == [b2]
    assert _name(svc, d) == "Hall Thermostat"
    assert not svc.conn.in_transaction
    assert svc.conn.execute("select count(*) from batch").fetchone()[0] == 2


def test_undo_conflicts_sorted_and_unique(svc, make_device):
    d = make_device(name="a")
    e = make_device(name="b")
    target = svc.update_device("claude", d, {"name": "p", "notes": "n"})
    later1 = svc.update_device("user", d, {"notes": "m"})
    svc.update_device("user", e, {"name": "unrelated"})
    later2 = svc.update_device("user", d, {"name": "q", "notes": "o"})
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", target)
    assert exc.value.conflicting == [later1, later2]


def test_undo_ok_when_later_batch_touched_other_field(svc, make_device):
    d = make_device(name="Espressif .23")
    b1 = svc.update_device("claude", d, {"name": "Thermostat"})
    svc.update_device("user", d, {"notes": "in the hall"})
    svc.undo("user", b1)
    assert _name(svc, d) == "Espressif .23"
    notes = svc.conn.execute("select notes from device where id=?", (d,)).fetchone()[0]
    assert notes == "in the hall"


def test_undo_twice_rejected(svc, make_device):
    d = make_device(name="a")
    b1 = svc.update_device("claude", d, {"name": "b"})
    svc.undo("user", b1)
    with pytest.raises(ValueError, match="already undone"):
        svc.undo("user", b1)


def test_undo_unknown_batch(svc):
    with pytest.raises(KeyError):
        svc.undo("user", 999)


def test_undo_rejects_bad_actor(svc, make_device):
    d = make_device(name="a")
    b1 = svc.update_device("claude", d, {"name": "b"})
    with pytest.raises(ValueError):
        svc.undo("collector:arp", b1)


def test_undo_link_add_removes_link(svc, make_device):
    a = make_device(name="a")
    b = make_device(name="b")
    batch = svc.add_link("claude", a, b, "wired", "port 3")
    svc.undo("user", batch)
    assert svc.conn.execute("select count(*) from link").fetchone()[0] == 0


def test_undo_link_remove_restores_same_id(svc, make_device):
    a = make_device(name="a")
    b = make_device(name="b")
    svc.add_link("user", a, b, "wired", "port 3")
    link_id = svc.conn.execute("select id from link").fetchone()[0]
    before = tuple(svc.conn.execute("select * from link where id=?", (link_id,)).fetchone())
    rm = svc.remove_link("claude", link_id)
    svc.undo("user", rm)
    after = svc.conn.execute("select * from link where id=?", (link_id,)).fetchone()
    assert after is not None and tuple(after) == before


def test_undo_the_undo_reapplies(svc, make_device, clock):
    d = make_device(name="Espressif .23")
    b1 = svc.update_device("claude", d, {"name": "Thermostat"})
    src = _source(svc, "device", d, "name")
    clock.advance(minutes=1)
    u1 = svc.undo("user", b1)
    assert _name(svc, d) == "Espressif .23"
    assert _source(svc, "device", d, "name") is None
    clock.advance(minutes=1)
    u2 = svc.undo("user", u1)
    assert _name(svc, d) == "Thermostat"
    assert _source(svc, "device", d, "name") == src
    assert (
        svc.conn.execute("select undoes_batch_id from batch where id=?", (u2,)).fetchone()[0] == u1
    )


def test_undo_place_creation_via_set_place(svc, make_device):
    d = make_device(name="tv")
    b = svc.set_place("claude", d, "Living Room")
    place_id = svc.conn.execute("select id from place where name='Living Room'").fetchone()[0]
    u = svc.undo("user", b)
    assert svc.conn.execute("select count(*) from place").fetchone()[0] == 0
    assert svc.conn.execute("select place_id from device where id=?", (d,)).fetchone()[0] is None
    assert _all_sources(svc, "place", place_id) == []
    assert _source(svc, "device", d, "place_id") is None
    # and re-applying recreates the same place id with its provenance
    svc.undo("user", u)
    assert [tuple(r) for r in svc.conn.execute("select id, name from place")] == [
        (place_id, "Living Room")
    ]
    assert svc.conn.execute("select place_id from device where id=?", (d,)).fetchone()[0] == (
        place_id
    )
    assert _source(svc, "place", place_id, "name")[0] == "claude"


def test_undo_place_creation_conflicts_when_later_device_uses_place(svc, make_device):
    d = make_device(name="tv")
    e = make_device(name="soundbar")
    b1 = svc.set_place("claude", d, "Living Room")
    b2 = svc.set_place("user", e, "Living Room")
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", b1)
    assert exc.value.conflicting == [b2]


def test_undo_placeholder_creation_deletes_device_and_sources(svc):
    d, b = svc.create_placeholder_device("user", "Office Switch", "switch")
    assert _all_sources(svc, "device", d) != []
    u = svc.undo("user", b)
    assert svc.conn.execute("select count(*) from device where id=?", (d,)).fetchone()[0] == 0
    assert _all_sources(svc, "device", d) == []
    # re-apply restores the same id, values and provenance
    svc.undo("claude", u)
    row = svc.conn.execute("select name, type, trust from device where id=?", (d,)).fetchone()
    assert tuple(row) == ("Office Switch", "switch", "known")
    assert _source(svc, "device", d, "name")[0] == "user"


def test_undo_placeholder_conflicts_when_later_link_references_it(svc, make_device):
    other = make_device(name="router")
    d, b = svc.create_placeholder_device("user", "Office Switch", "switch")
    b2 = svc.add_link("user", other, d, "wired")
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", b)
    assert exc.value.conflicting == [b2]


def test_conflict_ignores_later_batches_that_were_undone(svc, make_device):
    d = make_device(name="a")
    b1 = svc.update_device("claude", d, {"name": "b"})
    b2 = svc.update_device("user", d, {"name": "c"})
    svc.undo("user", b2)  # b2 and its undo cancel out
    svc.undo("user", b1)
    assert _name(svc, d) == "a"


def test_conflict_counts_reapplied_undo_of_undo(svc, make_device):
    """b2 undone then re-applied: its effect is live again, so undoing b1 must conflict."""
    d = make_device(name="a")
    b1 = svc.update_device("claude", d, {"name": "b"})
    b2 = svc.update_device("user", d, {"name": "c"})
    u1 = svc.undo("user", b2)
    u2 = svc.undo("user", u1)
    assert _name(svc, d) == "c"
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", b1)
    # b2's effect is live again via u2; b2 and u1 are themselves undone, so only
    # u2 is reported (undoing u2 is what clears the conflict).
    assert exc.value.conflicting == [u2]
    svc.undo("user", u2)
    svc.undo("user", b1)
    assert _name(svc, d) == "a"


def test_deleted_device_id_not_reused(svc):
    d, b = svc.create_placeholder_device("user", "Switch A", "switch")
    svc.undo("user", b)
    d2, _ = svc.create_placeholder_device("user", "Switch B", "switch")
    assert d2 != d


def test_removed_link_id_not_reused(svc, make_device):
    a = make_device(name="a")
    b = make_device(name="b")
    svc.add_link("user", a, b, "wired")
    link_id = svc.conn.execute("select id from link").fetchone()[0]
    svc.remove_link("user", link_id)
    svc.add_link("user", a, b, "wifi")
    assert svc.conn.execute("select id from link").fetchone()[0] != link_id


class _FailingCommitConnection(sqlite3.Connection):
    fail_commit = False

    def commit(self) -> None:
        if self.fail_commit:
            raise sqlite3.OperationalError("simulated commit failure")
        super().commit()


def test_commit_failure_rolls_back_and_publishes_nothing(tmp_path, clock):
    conn = sqlite3.connect(
        str(tmp_path / "atlas.db"), factory=_FailingCommitConnection, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    with conn:
        cur = conn.execute(
            "insert into device(name, first_seen, last_seen) values ('a', ?, ?)",
            ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
    d = cur.lastrowid
    events = EventBus(record=True)
    svc = Service(conn, events, clock)
    conn.fail_commit = True
    with pytest.raises(sqlite3.OperationalError, match="simulated"):
        svc.update_device("user", d, {"name": "b"})
    assert not conn.in_transaction
    assert events.recorded == []
    conn.fail_commit = False
    assert conn.execute("select name from device where id=?", (d,)).fetchone()[0] == "a"
    assert conn.execute("select count(*) from batch").fetchone()[0] == 0
    conn.close()


def test_undo_conflicts_with_earlier_rooted_undo(svc, make_device):
    """Regression: an odd-depth undo whose chain root precedes the target is a real write."""
    d = make_device(name="orig")
    p = svc.update_device("user", d, {"name": "P"})
    q = svc.update_device("user", d, {"name": "Q"})
    uq = svc.undo("user", q)  # name -> P
    up = svc.undo("user", p)  # name -> orig
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", uq)
    assert exc.value.conflicting == [up]
    assert _name(svc, d) == "orig"


@pytest.mark.parametrize("seed", range(40))
def test_undo_never_clobbers_random_histories(svc, make_device, clock, seed):
    """Fuzz: whenever undo succeeds, every field the target touched is still exactly
    what the target wrote (value and provenance), i.e. nothing later is clobbered."""
    import random

    rng = random.Random(seed)
    d = make_device(name="n0", notes="m0")
    batches: list[int] = []
    for step in range(30):
        clock.advance(seconds=1)
        if batches and rng.random() < 0.55:
            target = rng.choice(batches)
            touched = svc.conn.execute(
                "select field, new_value from change where batch_id=?", (target,)
            ).fetchall()
            srcs = {
                f: svc.conn.execute(
                    "select actor, set_at from field_source where entity='device'"
                    " and entity_id=? and field=?",
                    (d, f),
                ).fetchone()
                for f, _ in touched
            }
            current = {
                f: svc.conn.execute(f"select {f} from device where id=?", (d,)).fetchone()[0]
                for f, _ in touched
            }
            # what the target's own write left in field_source
            wrote_src = {}
            for f, _ in touched:
                nxt = svc.conn.execute(
                    "select old_actor, old_set_at from change where batch_id>? and entity='device'"
                    " and entity_id=? and field=? order by id limit 1",
                    (target, d, f),
                ).fetchone()
                wrote_src[f] = None if nxt is None else tuple(nxt)
            try:
                u = svc.undo(rng.choice(["user", "claude"]), target)
            except (UndoConflict, ValueError):
                continue
            for f, new_value in touched:
                assert current[f] == json.loads(new_value), (seed, step, target, f)
                if wrote_src[f] is not None:
                    got = None if srcs[f] is None else tuple(srcs[f])
                    norm = None if wrote_src[f] == (None, None) else wrote_src[f]
                    assert got == norm, (seed, step, target, f)
            batches.append(u)
        else:
            fields = rng.choice([["name"], ["notes"], ["name", "notes"]])
            vals = {f: f"{f}{rng.randint(1, 4)}" for f in fields}
            b = svc.update_device(rng.choice(["user", "claude"]), d, vals)
            if b:
                batches.append(b)


def test_undo_conflicts_with_deep_chain_rooted_before_target(svc, make_device):
    """Chain p <- u1 <- u2 <- u3 rooted before the target t: u3 (odd, root p < t) is a real
    write over t's field and must be reported; u1/u2 are undone and excluded."""
    d = make_device(name="orig")
    p = svc.update_device("user", d, {"name": "P"})
    t = svc.update_device("claude", d, {"notes": "x"})
    t2 = svc.update_device("claude", d, {"name": "T"})
    u_t2 = svc.undo("user", t2)  # name -> P
    u1 = svc.undo("user", p)  # name -> orig
    u2 = svc.undo("user", u1)  # name -> P
    u3 = svc.undo("user", u2)  # name -> orig
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", u_t2)
    assert exc.value.conflicting == [u3]
    # undoing t (notes only) is unaffected by the name chain
    svc.undo("user", t)
    assert _name(svc, d) == "orig"
