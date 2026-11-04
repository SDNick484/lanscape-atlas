from __future__ import annotations

import pytest

from conftest import dump_state

A = "a" * 64


def test_first_upload_creates_row_and_logs(svc, db, events):
    bid = svc.set_floor_plan("user", width=1600, height=1200, sha256=A, file_name=A + ".png")
    assert bid > 0
    assert tuple(db.execute("SELECT width, height FROM floor_plan WHERE id=1").fetchone()) == (
        1600,
        1200,
    )
    assert events.recorded[-1].payload == {"batch_id": bid, "actor": "user"}


def test_replacement_with_same_aspect_ok(svc, db):
    svc.set_floor_plan("user", width=1600, height=1200, sha256=A, file_name="x.png")
    svc.set_floor_plan("user", width=800, height=600, sha256="b" * 64, file_name="y.png")
    assert db.execute("SELECT width FROM floor_plan").fetchone()[0] == 800


def test_different_aspect_refused_and_nothing_written(svc, db):
    svc.set_floor_plan("user", width=1600, height=1200, sha256=A, file_name="x.png")
    before = dump_state(db)
    with pytest.raises(ValueError, match="aspect"):
        svc.set_floor_plan("user", width=1920, height=1080, sha256="c" * 64, file_name="z.png")
    assert dump_state(db) == before


def test_undo_restores_previous_plan(svc, db):
    svc.set_floor_plan("user", width=1600, height=1200, sha256=A, file_name="x.png")
    bid = svc.set_floor_plan("user", width=800, height=600, sha256="b" * 64, file_name="y.png")
    svc.undo("user", bid)
    assert tuple(db.execute("SELECT sha256, width, file_name FROM floor_plan").fetchone()) == (
        A,
        1600,
        "x.png",
    )


def test_undo_of_first_upload_deletes_row(svc, db):
    bid = svc.set_floor_plan("user", width=1600, height=1200, sha256=A, file_name="x.png")
    svc.undo("user", bid)
    assert db.execute("SELECT COUNT(*) FROM floor_plan").fetchone()[0] == 0
    assert (
        db.execute("SELECT COUNT(*) FROM field_source WHERE entity='floor_plan'").fetchone()[0] == 0
    )


def test_redo_of_first_upload_restores_row(svc, db):
    bid = svc.set_floor_plan("user", width=1600, height=1200, sha256=A, file_name="x.png")
    undo = svc.undo("user", bid)
    svc.undo("user", undo)
    assert tuple(db.execute("SELECT width, height, sha256 FROM floor_plan").fetchone()) == (
        1600,
        1200,
        A,
    )


def test_undo_first_upload_conflicts_with_later_replacement(svc):
    from lanscape_atlas.service import UndoConflict

    bid = svc.set_floor_plan("user", width=1600, height=1200, sha256=A, file_name="x.png")
    svc.set_floor_plan("user", width=800, height=600, sha256="b" * 64, file_name="y.png")
    with pytest.raises(UndoConflict):
        svc.undo("user", bid)


def test_claude_actor_accepted_collector_refused(svc):
    svc.set_floor_plan("claude", width=10, height=10, sha256=A, file_name="x.png")
    with pytest.raises(ValueError):
        svc.set_floor_plan("collector:arp", width=10, height=10, sha256="b" * 64, file_name="y.png")
