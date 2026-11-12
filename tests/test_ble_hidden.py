"""'Not mine': Service.hide_ble / unhide_ble, claim of a hidden identity, undo, queries."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from conftest import dump_state
from lanscape_atlas.bluetooth.aggregator import Candidate, hide_label
from lanscape_atlas.bluetooth.match import Identity, hidden_digest
from lanscape_atlas.events import EVENT_CHANGE_APPLIED
from lanscape_atlas.service import Service, UndoConflict
from lanscape_atlas.service import ble_queries as bq

ADDR = "00:00:5e:00:53:01"
ADDR2 = "00:00:5e:00:53:02"
BEACON = "00000000-0000-4000-8000-000000000001"
IDENT = Identity("address", ADDR)
IDENT2 = Identity("address", ADDR2)
BEACON_IDENT = Identity("ibeacon", f"{BEACON}/1/2")


def _salt(db: sqlite3.Connection) -> bytes:
    return bytes(db.execute("select salt from ble_salt").fetchone()[0])


def _hidden(db: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in db.execute("select * from ble_hidden order by id")]


def _identities(db: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in db.execute("select id, device_id, kind, value from ble_identity")]


def _last_batch(db: sqlite3.Connection) -> int:
    return int(db.execute("select max(id) from batch").fetchone()[0])


# ------------------------------------------------------------------ hide / unhide


def test_hide_stores_only_digest_and_label(svc: Service, db, clock, events) -> None:
    hid, batch = svc.hide_ble("user", IDENT, "Tag")
    rows = _hidden(db)
    assert rows == [
        {
            "id": hid,
            "digest": hidden_digest(_salt(db), IDENT),
            "label": "Tag",
            "hidden_at": "2026-01-01T00:00:00Z",
        }
    ]
    assert db.execute("select actor from batch where id = ?", (batch,)).fetchone()[0] == "user"
    assert events.recorded[-1].name == EVENT_CHANGE_APPLIED
    assert events.recorded[-1].payload == {"batch_id": batch, "actor": "user"}


def test_change_log_never_holds_the_raw_identity(svc: Service, db) -> None:
    hid, b1 = svc.hide_ble("claude", IDENT, "Tag")
    b2 = svc.unhide_ble("user", hid)
    b3 = svc.undo("user", b2)
    svc.undo("user", b3)
    svc.hide_ble("user", BEACON_IDENT, "Beacon")
    dumped = "\n".join(
        " ".join(str(v) for v in r) for r in db.execute("select * from change").fetchall()
    )
    for raw in (ADDR, IDENT.key(), BEACON, BEACON_IDENT.key(), "00:53:01"):
        assert raw not in dumped
    assert hidden_digest(_salt(db), IDENT) in dumped
    assert b1 and db.execute("select count(*) from change").fetchone()[0] >= 5


def test_label_is_trimmed_and_truncated(svc: Service, db) -> None:
    svc.hide_ble("user", IDENT, "  " + "x" * 100 + " ")
    assert _hidden(db)[0]["label"] == "x" * 80


@pytest.mark.parametrize("label", ["", "   ", None, 5])
def test_label_required(svc: Service, db, label: Any) -> None:
    with pytest.raises(ValueError):
        svc.hide_ble("user", IDENT, label)
    assert _hidden(db) == []


def test_hide_rejects_bad_actor_and_non_canonical_identity(svc: Service, db) -> None:
    with pytest.raises(ValueError):
        svc.hide_ble("collector:arp", IDENT, "Tag")
    with pytest.raises(ValueError):
        svc.hide_ble("user", Identity("address", ADDR.upper()), "Tag")
    assert db.execute("select count(*) from batch").fetchone()[0] == 0


def test_hide_twice_is_already_hidden(svc: Service, db) -> None:
    svc.hide_ble("user", IDENT, "Tag")
    with pytest.raises(ValueError, match="already hidden"):
        svc.hide_ble("user", IDENT, "Other")
    assert len(_hidden(db)) == 1
    assert db.execute("select count(*) from batch").fetchone()[0] == 1


def test_hide_refused_while_claimed(svc: Service, db, make_device) -> None:
    svc.claim_ble("user", IDENT, device_id=make_device(name="keys"))
    with pytest.raises(ValueError, match="claimed"):
        svc.hide_ble("user", IDENT, "Tag")
    assert _hidden(db) == []


def test_unhide_and_unknown_id(svc: Service, db, events) -> None:
    hid, _ = svc.hide_ble("user", IDENT, "Tag")
    b = svc.unhide_ble("claude", hid)
    assert _hidden(db) == []
    assert events.recorded[-1].payload == {"batch_id": b, "actor": "claude"}
    with pytest.raises(KeyError):
        svc.unhide_ble("user", hid)
    with pytest.raises(KeyError):
        svc.unhide_ble("user", 999)


def test_undo_hide_and_redo(svc: Service, db, clock) -> None:
    before = dump_state(db)
    hid, b = svc.hide_ble("user", IDENT, "Tag")
    hidden = _hidden(db)
    clock.advance(minutes=1)
    u = svc.undo("user", b)
    assert _hidden(db) == []
    assert {k: v for k, v in dump_state(db).items() if k not in ("batch", "change")} == {
        k: v for k, v in before.items() if k not in ("batch", "change")
    }
    clock.advance(minutes=1)
    svc.undo("user", u)
    assert _hidden(db) == hidden  # same id, digest, label, hidden_at
    assert hidden[0]["id"] == hid


def test_undo_unhide_restores_same_row_and_redo(svc: Service, db, clock) -> None:
    svc.hide_ble("user", IDENT, "Tag")
    hidden = _hidden(db)
    clock.advance(minutes=1)
    b = svc.unhide_ble("user", hidden[0]["id"])
    u = svc.undo("user", b)
    assert _hidden(db) == hidden
    svc.undo("user", u)
    assert _hidden(db) == []


def test_undo_hide_conflicts_with_later_unhide(svc: Service, db) -> None:
    hid, b = svc.hide_ble("user", IDENT, "Tag")
    later = svc.unhide_ble("user", hid)
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", b)
    assert exc.value.conflicting == [later]


def test_redo_hide_conflicts_with_a_later_hide_of_the_same_identity(svc: Service, db) -> None:
    _, b = svc.hide_ble("user", IDENT, "Tag")
    u = svc.undo("user", b)
    _, again = svc.hide_ble("user", IDENT, "Tag again")
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", u)  # would re-insert a second row for the same digest
    assert exc.value.conflicting == [again]
    assert not db.in_transaction


def test_undo_unhide_conflicts_with_a_later_claim(svc: Service, db, make_device) -> None:
    hid, _ = svc.hide_ble("user", IDENT, "Tag")
    unhide = svc.unhide_ble("user", hid)
    _, claim = svc.claim_ble("user", IDENT, device_id=make_device(name="keys"))
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", unhide)  # would hide a claimed identity
    assert exc.value.conflicting == [claim]


def test_undo_unclaim_conflicts_with_a_later_hide(svc: Service, db, make_device) -> None:
    svc.claim_ble("user", IDENT, device_id=make_device(name="keys"))
    unclaim = svc.unclaim_ble("user", _identities(db)[0][0])
    _, hide = svc.hide_ble("user", IDENT, "Tag")
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", unclaim)  # would claim a hidden identity
    assert exc.value.conflicting == [hide]


def test_other_identities_do_not_conflict(svc: Service, db, make_device) -> None:
    hid, _ = svc.hide_ble("user", IDENT, "Tag")
    unhide = svc.unhide_ble("user", hid)
    svc.hide_ble("user", IDENT2, "Other")
    svc.claim_ble("user", BEACON_IDENT, device_id=make_device(name="keys"))
    svc.undo("user", unhide)
    assert [r["label"] for r in _hidden(db)] == ["Tag", "Other"]


# ----------------------------------------------------------- claim of a hidden one


def test_claim_of_hidden_unhides_in_the_same_batch(svc: Service, db, make_device) -> None:
    d = make_device(name="keys")
    svc.hide_ble("user", IDENT, "Tag")
    hidden = _hidden(db)
    _, b = svc.claim_ble("claude", IDENT, device_id=d)
    assert _hidden(db) == []
    entities = [
        r[0] for r in db.execute("select entity from change where batch_id = ? order by id", (b,))
    ]
    assert sorted(entities) == ["ble_hidden", "ble_identity"]
    svc.undo("user", b)  # one undo restores both
    assert _identities(db) == []
    assert _hidden(db) == hidden
    svc.undo("user", _last_batch(db))  # and redo claims and unhides again
    assert len(_identities(db)) == 1 and _hidden(db) == []


def test_claim_of_hidden_as_new_device_undoes_cleanly(svc: Service, db) -> None:
    svc.hide_ble("user", BEACON_IDENT, "Beacon")
    hidden = _hidden(db)
    _, b = svc.claim_ble("user", BEACON_IDENT, new_name="Keys", new_type="tag")
    svc.undo("user", b)
    assert db.execute("select count(*) from device").fetchone()[0] == 0
    assert _hidden(db) == hidden


def test_undo_hide_after_claim_conflicts(svc: Service, db, make_device) -> None:
    _, hide = svc.hide_ble("user", IDENT, "Tag")
    _, claim = svc.claim_ble("user", IDENT, device_id=make_device(name="keys"))
    with pytest.raises(UndoConflict) as exc:
        svc.undo("user", hide)
    assert exc.value.conflicting == [claim]
    svc.undo("user", claim)  # undo the claim first: the hide is back ...
    svc.undo("user", hide)  # ... and can then be undone
    assert _hidden(db) == [] and _identities(db) == []


# ------------------------------------------------------------------------ queries


def test_queries_digests_and_listing_newest_first(svc: Service, db, clock) -> None:
    assert bq.hidden_digests(db) == set()
    assert bq.hidden(db) == []
    a, _ = svc.hide_ble("user", IDENT, "Tag")
    clock.advance(minutes=5)
    b, _ = svc.hide_ble("user", BEACON_IDENT, "Beacon")
    salt = bq.ble_salt(db)
    assert salt == _salt(db)
    assert bq.hidden_digests(db) == {
        hidden_digest(salt, IDENT),
        hidden_digest(salt, BEACON_IDENT),
    }
    assert [(h.id, h.label, h.hidden_at) for h in bq.hidden(db)] == [
        (b, "Beacon", "2026-01-01T00:05:00Z"),
        (a, "Tag", "2026-01-01T00:00:00Z"),
    ]


# -------------------------------------------------------------------------- label


def _cand(identity: Identity, name: str | None) -> Candidate:
    from datetime import UTC, datetime

    at = datetime(2026, 1, 1, tzinfo=UTC)
    return Candidate(identity, name, -60, "00:00:5e:00:53:aa", at, at, 1, None)


def test_hide_label_prefers_name_then_vendor_then_placeholder() -> None:
    vendors = {ADDR: "Example Vendor"}

    def vendor(address: str) -> str | None:
        return vendors.get(address)

    assert hide_label(_cand(IDENT, "Tag"), vendor) == "Tag"
    assert hide_label(_cand(IDENT, "  "), vendor) == "Example Vendor"
    assert hide_label(_cand(IDENT, None), vendor) == "Example Vendor"
    assert hide_label(_cand(IDENT2, None), vendor) == "Unnamed device"
    assert hide_label(_cand(BEACON_IDENT, None), vendor) == "Unnamed device"
    assert hide_label(_cand(IDENT, "n" * 90), vendor) == "n" * 80
