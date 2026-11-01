"""The only owner of the BESO connection: a single task that executes jobs one at a time.

Jobs wait in bounded lanes ordered by priority (control > monitor > write > read > background). A job runs
to completion before the next one starts, so a multi-step command is atomic on the link. Callers only await
the job's future; cancelling a caller never interrupts I/O (which would desynchronise the link) – a job that
has not started yet is simply skipped.
"""
import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, List, Optional

from obsrv.protocols.beso.beso_config import Settings
from obsrv.protocols.beso.beso_errors import (BesoArgumentError, BesoBusyError, BesoClosedError, BesoLinkDownError,
                                              BesoLinkError, BesoTimeoutError)
from obsrv.protocols.beso.beso_link import BesoLink

logger = logging.getLogger(__name__.rsplit('.')[-1])

CONTROL, MONITOR, WRITE, READ, BACKGROUND = range(5)
LANE_NAMES = ('control', 'monitor', 'write', 'read', 'background')


class Executor:
    """Handed to a running job; the only way to talk to BESO."""

    def __init__(self, worker: 'CommandWorker'):
        self._worker = worker

    async def exchange(self, command: str, timeout: Optional[float] = None) -> str:
        return await self._worker._exchange(command, timeout)


JobRun = Callable[[Executor], Awaitable[Any]]


@dataclass(eq=False)
class Job:
    lane: int
    run: JobRun
    label: str
    future: asyncio.Future
    enqueued: float
    max_wait: Optional[float] = None
    tag: Optional[str] = None
    extra: dict = field(default_factory=dict)


class CommandWorker:
    def __init__(self, host: str, port: int, settings: Settings, *,
                 on_connected: Optional[Callable[[Executor], Awaitable[None]]] = None,
                 on_link_lost: Optional[Callable[[], None]] = None,
                 link_factory=BesoLink.open,
                 clock: Callable[[], float] = time.monotonic):
        self.host = host
        self.port = port
        self.address = f"{host}:{port}"
        self._settings = settings
        self._on_connected = on_connected
        self._on_link_lost = on_link_lost
        self._link_factory = link_factory
        self._clock = clock
        q = settings.queue
        self._caps = (q.control, q.monitor, q.write, q.read, q.background)
        self._lanes: List[Deque[Job]] = [deque() for _ in LANE_NAMES]
        self._wakeup = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._link: Optional[BesoLink] = None
        self._executor = Executor(self)
        self._closed = False
        self._current: Optional[Job] = None
        self._backoff = settings.backoff.initial
        self._next_attempt = 0.0
        self._failed_attempts = 0
        self._rejected = 0
        self._outage = False
        self._last_activity = clock()
        self.connections_opened = 0

    # ------------------------------------------------------------------------------------- public
    @property
    def connected(self) -> bool:
        return self._link is not None and self._link.is_open

    @property
    def idle_for(self) -> float:
        return self._clock() - self._last_activity

    def queued(self) -> int:
        return sum(len(lane) for lane in self._lanes)

    def submit(self, lane: int, run: JobRun, *, label: str, max_wait: Optional[float] = None,
               tag: Optional[str] = None) -> asyncio.Future:
        """Queue a job; raises at once when closed, while the link is down, or when the lane is full."""
        if self._closed:
            raise BesoClosedError("BESO connector is closed")
        if self._link is None and self._clock() < self._next_attempt:
            self._rejected += 1
            raise BesoLinkDownError(f"BESO at {self.address} is not reachable (next reconnect attempt in "
                                    f"{self._next_attempt - self._clock():.1f} s)")
        queue = self._lanes[lane]
        if len(queue) >= self._caps[lane]:
            raise BesoBusyError(f"BESO {LANE_NAMES[lane]} queue is full ({self._caps[lane]} requests waiting)")
        loop = asyncio.get_running_loop()
        job = Job(lane, run, label, loop.create_future(), self._clock(), max_wait, tag)
        queue.append(job)
        self._ensure_task()
        self._wakeup.set()
        return job.future

    def cancel_pending(self, predicate: Callable[[Job], bool], exc: BaseException) -> int:
        """Fail queued (not yet running) jobs that match ``predicate``. Returns how many were removed."""
        removed = 0
        for queue in self._lanes:
            keep = deque()
            while queue:
                job = queue.popleft()
                if predicate(job):
                    removed += 1
                    if not job.future.done():
                        job.future.set_exception(exc)
                else:
                    keep.append(job)
            queue.extend(keep)
        return removed

    def pending_jobs(self, predicate: Callable[[Job], bool]) -> bool:
        if self._current is not None and predicate(self._current):
            return True
        return any(predicate(job) for queue in self._lanes for job in queue)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("BESO worker for %s failed during shutdown", self.address)
        self._fail_all(BesoClosedError("BESO connector closed"))
        await self._drop_link(None, notify=False)

    # ----------------------------------------------------------------------------------- internals
    def _ensure_task(self) -> None:
        if self._task is None or self._task.done():
            if self._task is not None and not self._task.cancelled() and self._task.exception() is not None:
                logger.error("BESO worker for %s had died; restarting it", self.address)
            self._task = asyncio.get_running_loop().create_task(self._run(), name=f"beso-worker-{self.address}")
            self._task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("BESO worker for %s stopped unexpectedly: %r", self.address, exc, exc_info=exc)
            self._fail_all(BesoClosedError(f"BESO worker failed: {exc!r}"))

    def _fail_all(self, exc: BaseException) -> None:
        for queue in self._lanes:
            while queue:
                job = queue.popleft()
                if not job.future.done():
                    job.future.set_exception(exc)

    def _next_job(self) -> Optional[Job]:
        for queue in self._lanes:
            if queue:
                return queue.popleft()
        return None

    async def _run(self) -> None:
        while True:
            job = self._next_job()
            if job is None:
                self._wakeup.clear()
                await self._wakeup.wait()
                continue
            if job.future.done():  # the caller gave up before the job started
                continue
            if job.max_wait is not None and self._clock() - job.enqueued > job.max_wait:
                job.future.set_exception(BesoTimeoutError(
                    f"{job.label} waited more than {job.max_wait} s in the queue; not executed"))
                continue
            self._current = job
            try:
                await self._ensure_link()
                result = await job.run(self._executor)
            except asyncio.CancelledError:
                if not job.future.done():
                    job.future.set_exception(BesoClosedError("BESO connector closed"))
                raise
            except BesoLinkError as exc:
                if not isinstance(exc, BesoLinkDownError):
                    await self._drop_link(exc)
                if not job.future.done():
                    job.future.set_exception(exc)
            except Exception as exc:
                if not job.future.done():
                    job.future.set_exception(exc)
            else:
                if not job.future.done():
                    job.future.set_result(result)
            finally:
                self._current = None

    async def _ensure_link(self) -> None:
        if self._link is not None:
            if self._link.is_open:
                return
            await self._drop_link(BesoLinkError("connection closed by BESO"))
        now = self._clock()
        if now < self._next_attempt:
            raise BesoLinkDownError(f"BESO at {self.address} is not reachable")
        try:
            self._link = await self._link_factory(self.host, self.port, self._settings.framing,
                                                  self._settings.timeouts, self._settings.tcp_keepalive)
            self.connections_opened += 1
            if self._on_connected is not None:
                await self._on_connected(self._executor)
        except BesoLinkError as exc:
            await self._drop_link(exc, notify=False)
            self._schedule_retry(exc)
            raise BesoLinkDownError(f"BESO at {self.address} is not reachable: {exc}") from exc
        if self._outage:
            logger.warning("BESO at %s reachable again after %d failed attempts; %d requests were rejected meanwhile",
                           self.address, self._failed_attempts, self._rejected)
        else:
            logger.info("Connected to BESO at %s", self.address)
        self._outage = False
        self._failed_attempts = 0
        self._rejected = 0
        self._backoff = self._settings.backoff.initial
        self._next_attempt = 0.0

    def _schedule_retry(self, exc: BaseException) -> None:
        b = self._settings.backoff
        delay = self._backoff * (1.0 + random.uniform(-b.jitter, b.jitter))
        self._next_attempt = self._clock() + delay
        self._backoff = min(self._backoff * b.factor, b.maximum)
        self._failed_attempts += 1
        if not self._outage:
            self._outage = True
            logger.error("BESO at %s unreachable: %s. Retrying with back-off up to %.0f s; requests fail "
                         "immediately until it is back, repeated failures are not logged.",
                         self.address, exc, b.maximum)
        elif logger.isEnabledFor(logging.DEBUG):
            logger.debug("BESO at %s still unreachable (attempt %d): %s", self.address, self._failed_attempts, exc)

    async def _drop_link(self, exc: Optional[BaseException], notify: bool = True) -> None:
        link, self._link = self._link, None
        if link is not None:
            try:
                await link.close()
            except Exception:
                logger.exception("Error while closing BESO link to %s", self.address)
            if exc is not None:
                logger.warning("BESO link to %s dropped: %s", self.address, exc)
        if notify and link is not None and self._on_link_lost is not None:
            self._on_link_lost()

    async def _exchange(self, command: str, timeout: Optional[float]) -> str:
        if self._link is None:
            raise BesoLinkError("not connected")
        settings = self._settings
        for pattern in settings.forbidden_commands:
            if pattern.search(command):
                raise BesoArgumentError(f"command {command!r} is forbidden by {pattern.pattern!r}")
        if len(command.encode(settings.framing.encoding, errors='replace')) > settings.framing.max_command_bytes:
            raise BesoArgumentError(f"command longer than {settings.framing.max_command_bytes} bytes")
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("BESO %s <- %s", self.address, command)
        reply = await self._link.exchange(command, timeout if timeout is not None else settings.timeouts.command)
        self._last_activity = self._clock()
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("BESO %s -> %s", self.address, reply)
        return reply
