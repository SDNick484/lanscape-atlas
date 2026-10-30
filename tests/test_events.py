import logging
from datetime import UTC, datetime

from lanscape_atlas.events import (
    EVENT_CHANGE_APPLIED,
    EVENT_FIRST_SEEN,
    EVENT_OFFLINE,
    EVENT_ONLINE,
    Event,
    EventBus,
)
from lanscape_atlas.observation import Observation


def test_event_names():
    assert EVENT_FIRST_SEEN == "device.first_seen"
    assert EVENT_ONLINE == "device.online"
    assert EVENT_OFFLINE == "device.offline"
    assert EVENT_CHANGE_APPLIED == "change.applied"


def test_publish_calls_subscribers_in_order():
    bus = EventBus(record=True)
    calls: list[str] = []
    bus.subscribe(EVENT_ONLINE, lambda e: calls.append("a"))
    bus.subscribe(EVENT_ONLINE, lambda e: calls.append("b"))
    bus.subscribe(EVENT_OFFLINE, lambda e: calls.append("x"))
    ev = Event(EVENT_ONLINE, {"device_id": 1})
    bus.publish(ev)
    assert calls == ["a", "b"]
    assert bus.recorded == [ev]


def test_no_record_by_default():
    bus = EventBus()
    bus.publish(Event(EVENT_ONLINE, {}))
    assert bus.recorded == []


def test_handler_exception_isolated(caplog):
    bus = EventBus()
    calls: list[str] = []

    def boom(e: Event) -> None:
        raise RuntimeError("boom")

    bus.subscribe(EVENT_ONLINE, boom)
    bus.subscribe(EVENT_ONLINE, lambda e: calls.append("second"))
    with caplog.at_level(logging.ERROR):
        bus.publish(Event(EVENT_ONLINE, {}))
    assert calls == ["second"]
    assert any(r.exc_info for r in caplog.records)


def _obs() -> Observation:
    return Observation(
        source="arp",
        seen_at=datetime(2026, 10, 29, 12, 0, 5, tzinfo=UTC),
        iface_kind="wifi",
        mac="00:00:5e:00:53:01",
        ipv4="192.0.2.10",
        hostname=None,
        attrs={"services": [{"port": 80, "names": ["http", "web"]}], "n": 1},
    )


def test_observation_json_roundtrip():
    o = _obs()
    s = o.to_json()
    assert "2026-10-29T12:00:05Z" in s
    assert Observation.from_json(s) == o
    assert s == o.to_json()
    assert s.index('"attrs"') < s.index('"source"')  # sorted keys
