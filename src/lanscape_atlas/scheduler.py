"""Run collectors on a schedule, ingest their results and track health/backoff.

Database access (ruling 26): one shared sqlite connection guarded by
``Service.lock``. Every ``collector_status`` read/write here takes that lock;
``collect()`` network I/O runs outside it; ``Ingestor.ingest`` takes it itself.

A collector that hangs, raises, or whose results fail to ingest is recorded in
``collector_status`` and backed off; it never stops the other jobs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from apscheduler.triggers.interval import IntervalTrigger  # type: ignore[import-untyped]
from tzlocal import get_localzone

from lanscape_atlas.clock import Clock, iso
from lanscape_atlas.collectors.base import Collector
from lanscape_atlas.config import Settings, _default_collectors
from lanscape_atlas.ingest import Ingestor, IngestResult
from lanscape_atlas.service import Service

if TYPE_CHECKING:
    from lanscape_atlas.bluetooth.runtime import BluetoothRuntime

log = logging.getLogger(__name__)

BACKOFF_CAP_S = 3600.0
CRON_BACKOFF_BASE_S = 3600.0
DEFAULT_INTERVAL_S = 300.0
MAX_ERROR_LEN = 500
# The runner's outer timeout is ``timeout_s + TIMEOUT_GRACE_S`` so a collector's own
# timeout (with its clearer message and subprocess cleanup) normally fires first.
TIMEOUT_GRACE_S = 5.0
PRESENCE_INTERVAL_S = 60
PRUNE_HOUR, PRUNE_MINUTE = 4, 41


def next_delay(base_s: float, failures: int) -> float:
    """Exponential backoff: ``min(base_s * 2**failures, 3600)``."""
    return float(min(base_s * 2**failures, BACKOFF_CAP_S))


@dataclass(frozen=True)
class RunOutcome:
    ok: bool
    error: str | None
    result: IngestResult | None


class CollectorRunner:
    """Run one collector with a timeout, ingest its output and upsert its status row."""

    def __init__(
        self,
        service: Service,
        ingestor: Ingestor,
        clock: Clock,
        *,
        secrets: Iterable[str] = (),
        intervals: dict[str, float | None] | None = None,
    ) -> None:
        self.service = service
        self.ingestor = ingestor
        self.clock = clock
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)
        # Per-collector interval_s (None = cron-scheduled); used as the backoff base.
        if intervals is None:
            intervals = {n: cs.interval_s for n, cs in _default_collectors().items()}
        self._intervals = intervals
        # Names of long-lived collectors whose start() has succeeded.
        self._started: set[str] = set()

    @classmethod
    def from_settings(
        cls, settings: Settings, service: Service, ingestor: Ingestor, clock: Clock
    ) -> CollectorRunner:
        secrets = [settings.api_token or ""]
        if settings.router is not None:
            secrets.append(settings.router.password)
        if settings.home_assistant is not None:
            secrets.append(settings.home_assistant.token)
        return cls(
            service,
            ingestor,
            clock,
            secrets=secrets,
            intervals={n: cs.interval_s for n, cs in settings.collectors.items()},
        )

    # ------------------------------------------------------------------ helpers

    def _base_delay(self, name: str) -> float:
        if name not in self._intervals:
            return DEFAULT_INTERVAL_S
        interval = self._intervals[name]
        return CRON_BACKOFF_BASE_S if interval is None else float(interval)

    def redact(self, msg: str) -> str:
        """Mask configured secrets in ``msg`` and truncate it (the stored/logged form)."""
        # Redact before truncating so a secret cut at the boundary cannot leak.
        for s in self._secrets:
            msg = msg.replace(s, "***")
        return msg[:MAX_ERROR_LEN]

    def _format_error(self, e: BaseException, c: Collector) -> str:
        if isinstance(e, TimeoutError) and not str(e):
            msg = f"TimeoutError: timed out after {c.timeout_s + TIMEOUT_GRACE_S:g}s"
        else:
            msg = f"{type(e).__name__}: {e}"
        return self.redact(msg)

    @contextmanager
    def _txn(self) -> Iterator[Any]:
        """Hold ``Service.lock`` and one ``BEGIN IMMEDIATE`` transaction."""
        conn = self.service.conn
        with self.service.lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _record_ok(self, name: str) -> None:
        with self._txn() as conn:
            conn.execute(
                "INSERT INTO collector_status(name, last_ok, consecutive_failures, next_run_at)"
                " VALUES (?, ?, 0, NULL)"
                " ON CONFLICT(name) DO UPDATE SET last_ok = excluded.last_ok,"
                " consecutive_failures = 0, next_run_at = NULL",
                (name, iso(self.clock())),
            )

    def _record_failure(self, c: Collector, error: str) -> None:
        self._record_failure_named(c.name, error)

    def _record_failure_named(self, name: str, error: str) -> None:
        now = self.clock()
        with self._txn() as conn:
            row = conn.execute(
                "SELECT consecutive_failures FROM collector_status WHERE name = ?", (name,)
            ).fetchone()
            failures = (int(row[0]) if row else 0) + 1
            next_run = now + timedelta(seconds=next_delay(self._base_delay(name), failures))
            conn.execute(
                "INSERT INTO collector_status(name, last_error, last_error_at,"
                " consecutive_failures, next_run_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET last_error = excluded.last_error,"
                " last_error_at = excluded.last_error_at,"
                " consecutive_failures = excluded.consecutive_failures,"
                " next_run_at = excluded.next_run_at",
                (name, error, iso(now), failures, iso(next_run)),
            )

    # ------------------------------------------------------------------ public

    def record_status(self, name: str, error: str | None) -> None:
        """Record health for a component that is not run via ``run_once`` (e.g. Bluetooth).

        ``error=None`` is a success (``last_ok``, failures reset); otherwise the message is
        redacted and truncated and counted as one more failure. Raises on DB errors.
        """
        if error is None:
            self._record_ok(name)
        else:
            self._record_failure_named(name, self.redact(error))

    async def run_once(self, c: Collector) -> RunOutcome:
        """Collect (with timeout) and ingest. Never raises except ``CancelledError``."""
        try:
            observations = await asyncio.wait_for(
                c.collect(), timeout=c.timeout_s + TIMEOUT_GRACE_S
            )
            result = self.ingestor.ingest(observations)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - one collector must never stop the others
            return self._fail(c, e, "failed")
        try:
            self._record_ok(c.name)
        except Exception as ex:  # noqa: BLE001
            log.error(
                "could not record success of collector %s: %s", c.name, self._format_error(ex, c)
            )
        return RunOutcome(True, None, result)

    def _fail(self, c: Collector, e: BaseException, what: str) -> RunOutcome:
        """Log the redacted error and record it through the normal failure path."""
        error = self._format_error(e, c)
        log.warning("collector %s %s: %s", c.name, what, error)
        try:
            self._record_failure(c, error)
        except Exception as ex:  # noqa: BLE001
            # No exc_info: the chained traceback would carry the unredacted original.
            log.error(
                "could not record failure of collector %s: %s", c.name, self._format_error(ex, c)
            )
        return RunOutcome(False, error, None)

    async def ensure_started(self, c: Collector) -> RunOutcome | None:
        """Await ``start()`` on a long-lived collector not yet started.

        Returns None when there is nothing to do or start succeeded; otherwise the
        failure, recorded in ``collector_status`` (redacted, failures++, backoff).
        """
        start = getattr(c, "start", None)
        if start is None or c.name in self._started:
            return None
        try:
            # Bounded like collect(): a hung start() must not block the scheduler.
            await asyncio.wait_for(start(), timeout=c.timeout_s + TIMEOUT_GRACE_S)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a failed start must not block the rest
            return self._fail(c, e, "failed to start")
        self._started.add(c.name)
        return None

    async def stop(self, c: Collector) -> None:
        """Await ``stop()`` on a long-lived collector; errors are logged redacted."""
        self._started.discard(c.name)
        stop = getattr(c, "stop", None)
        if stop is None:
            return
        try:
            await stop()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("collector %s failed to stop: %s", c.name, self._format_error(e, c))

    async def run_if_due(self, c: Collector) -> RunOutcome | None:
        """Scheduler job body: skip (return None) while ``next_run_at`` is in the future."""
        try:
            with self.service.lock:
                row = self.service.conn.execute(
                    "SELECT next_run_at FROM collector_status WHERE name = ?", (c.name,)
                ).fetchone()
        except Exception as ex:  # noqa: BLE001
            log.error(
                "could not read status of collector %s: %s", c.name, self._format_error(ex, c)
            )
            row = None
        if row is not None and row[0] is not None and row[0] > iso(self.clock()):
            log.debug("collector %s in backoff until %s; skipping", c.name, row[0])
            return None
        failed = await self.ensure_started(c)
        if failed is not None:
            return failed
        return await self.run_once(c)


# ---------------------------------------------------------------------- jobs


async def _prune_job(service: Service, days: int, ble_days: int) -> None:
    """Nightly prune; each prune runs even if the other fails."""
    try:
        n = service.prune_observations(days)
        log.info("pruned %d observations older than %d days", n, days)
    except Exception:  # noqa: BLE001
        log.exception("observation prune failed")
    try:
        n = service.prune_ble(ble_days)
        log.info("pruned %d BLE sightings older than %d days", n, ble_days)
    except Exception:  # noqa: BLE001
        log.exception("BLE sighting prune failed")


async def _presence_job(ingestor: Ingestor) -> None:
    try:
        ingestor.refresh_presence()
    except Exception:  # noqa: BLE001
        log.exception("presence refresh failed")


async def start_scheduler(
    settings: Settings,
    runner: CollectorRunner,
    collectors: list[Collector],
    service: Service,
    bluetooth: BluetoothRuntime | None = None,
) -> AsyncIOScheduler:
    """Start long-lived collectors, register all jobs and start the scheduler.

    Must be awaited inside the running event loop that will execute the jobs.
    """
    tz = get_localzone()
    for c in collectors:
        # A failure is recorded (with backoff); the job retries start() when next due.
        await runner.ensure_started(c)
    if bluetooth is not None:
        await bluetooth.start()

    sched = AsyncIOScheduler(timezone=tz)
    common: dict[str, Any] = {"max_instances": 1, "coalesce": True, "replace_existing": True}
    # Interval jobs also fire once at startup instead of first waiting a whole interval;
    # cron jobs (nmap_light, prune) keep their wall-clock schedule.
    now = datetime.now(tz)
    for c in collectors:
        cs = settings.collectors.get(c.name)
        trigger: CronTrigger | IntervalTrigger
        extra: dict[str, Any] = {}
        if cs is not None and cs.cron:
            trigger = CronTrigger.from_crontab(cs.cron, timezone=tz)
        elif cs is not None and cs.interval_s:
            trigger = IntervalTrigger(seconds=cs.interval_s, timezone=tz)
            extra["next_run_time"] = now
        else:
            log.warning("collector %s has no schedule; not scheduled", c.name)
            continue
        sched.add_job(
            runner.run_if_due,
            trigger,
            args=[c],
            id=f"collector:{c.name}",
            name=f"collector:{c.name}",
            **common,
            **extra,
        )
    sched.add_job(
        _prune_job,
        CronTrigger(hour=PRUNE_HOUR, minute=PRUNE_MINUTE, timezone=tz),
        args=[service, settings.observation_retention_days, settings.bluetooth.retention_days],
        id="prune",
        name="prune",
        **common,
    )
    sched.add_job(
        _presence_job,
        IntervalTrigger(seconds=PRESENCE_INTERVAL_S, timezone=tz),
        args=[runner.ingestor],
        id="presence",
        name="presence",
        next_run_time=now,
        **common,
    )
    if bluetooth is not None:
        sched.add_job(
            bluetooth.flush,
            IntervalTrigger(seconds=settings.bluetooth.flush_s, timezone=tz),
            id="ble_flush",
            name="ble_flush",
            **common,
        )
    sched.start()
    return sched


async def stop_scheduler(
    scheduler: AsyncIOScheduler,
    collectors: list[Collector],
    runner: CollectorRunner | None = None,
    bluetooth: BluetoothRuntime | None = None,
) -> None:
    """Shut the scheduler down, then await ``stop()`` on long-lived collectors.

    The Bluetooth runtime (if any) is stopped last: it flushes its open minutes.

    Pass the runner so stop errors are logged redacted and started-ness is reset;
    without it only the exception type is logged.
    """
    if scheduler.running:
        scheduler.shutdown(wait=False)
        await asyncio.sleep(0)  # AsyncIOScheduler defers _shutdown to the loop
    for c in collectors:
        if runner is not None:
            await runner.stop(c)
            continue
        stop = getattr(c, "stop", None)
        if stop is not None:
            try:
                await stop()
            except Exception as e:  # noqa: BLE001
                log.warning("collector %s failed to stop: %s", c.name, type(e).__name__)
    if bluetooth is not None:
        await bluetooth.stop()
