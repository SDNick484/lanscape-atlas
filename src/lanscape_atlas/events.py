"""Synchronous in-process event bus."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

EVENT_FIRST_SEEN = "device.first_seen"
EVENT_ONLINE = "device.online"
EVENT_OFFLINE = "device.offline"
EVENT_CHANGE_APPLIED = "change.applied"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    name: str
    payload: dict[str, Any]


Handler = Callable[[Event], None]


class EventBus:
    def __init__(self, record: bool = False) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._record = record
        self.recorded: list[Event] = []

    def subscribe(self, name: str, handler: Handler) -> None:
        self._handlers[name].append(handler)

    def publish(self, event: Event) -> None:
        if self._record:
            self.recorded.append(event)
        for handler in list(self._handlers.get(event.name, ())):
            try:
                handler(event)
            except Exception:
                log.exception("event handler failed for %s", event.name)
