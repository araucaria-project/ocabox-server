"""One session per BESO address, shared by every connector that points at it.

BESO accepts a single client, while the device tree creates a separate connector for each component that
declares ``protocol: beso``. The session therefore owns everything that must exist once per device: the
worker (and with it the only connection), the read cache, the exposure tracker and its monitor task.
"""
import asyncio
import copy
import functools
import logging
import time
from typing import Any, Dict, Optional, Tuple

from obsrv.protocols.beso.beso_config import (BesoConfig, CommandStep, DelayStep, Query, ReadSpec, Source, WaitStep,
                                              WriteSpec)
from obsrv.protocols.beso.beso_errors import (SEVERITY_NORMAL, BesoArgumentError, BesoBusyError, BesoClosedError,
                                              BesoConfigError, BesoConversionError, BesoDeviceError, BesoError,
                                              BesoLinkError, BesoNoDataError, BesoUnsupportedError)
from obsrv.protocols.beso.beso_exposure import ExposureTracker, Phase
from obsrv.protocols.beso.beso_logging import RepeatLimiter
from obsrv.protocols.beso.beso_worker import BACKGROUND, CONTROL, MONITOR, READ, WRITE, CommandWorker, Executor

logger = logging.getLogger(__name__.rsplit('.')[-1])

DEFAULT_PORT = 30020  # 'besocam 30020/tcp' in /etc/services on the observatory machines
START_TAG = 'exposure_start'
_FORBIDDEN_VALUE_CHARS = frozenset(chr(c) for c in range(0x20)) | {'\x7f', "'"}


def parse_address(address: str) -> Tuple[str, int]:
    if not isinstance(address, str) or not address.strip():
        raise BesoConfigError(f"invalid BESO address {address!r}")
    if '://' in address:
        raise BesoConfigError(f"BESO address must be 'host:port', not a URL: {address!r}")
    host, sep, port = address.strip().rpartition(':')
    if not sep:
        return address.strip(), DEFAULT_PORT
    try:
        number = int(port)
    except ValueError:
        raise BesoConfigError(f"invalid port in BESO address {address!r}") from None
    if not host or not 0 < number < 65536:
        raise BesoConfigError(f"invalid BESO address {address!r}")
    return host, number


def _same_value(a, b) -> bool:
    # True == 1 in Python; a constant put must not accept a boolean for an integer and vice versa.
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return type(a) is type(b) and a == b


class BesoSession:
    def __init__(self, address: str, config: BesoConfig, clock=time.monotonic):
        host, port = parse_address(address)
        self.address = f"{host}:{port}"
        self.config = config
        self.settings = config.settings
        self.loop = asyncio.get_running_loop()
        self._clock = clock
        self.worker = CommandWorker(host, port, self.settings, on_connected=self._on_connected,
                                    on_link_lost=self._on_link_lost, clock=clock)
        self.tracker = ExposureTracker(self.settings.exposure, self.settings.error_pattern, clock=clock)
        self._status_query: Query = config.queries[self.settings.exposure.status_query]
        self._health_query: Optional[Query] = (config.queries[self.settings.health.query]
                                               if self.settings.health.query else None)
        # Fixed-size state: at most one entry per configured query / parameter source / marker.
        self._raw: Dict[str, Tuple[str, float]] = {}
        self._converted: Dict[Tuple[str, int], Tuple[float, Any, Optional[tuple]]] = {}
        self._inflight: Dict[str, asyncio.Future] = {}
        self._markers: Dict[str, float] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._monitor_wakeup = asyncio.Event()
        self._log = RepeatLimiter(self.settings.log_repeat_interval, clock=clock)
        self._uptime: Optional[float] = None
        self.refs = 0
        self.closed = False

    # ---------------------------------------------------------------------------------- lifecycle
    def _ensure_tasks(self) -> None:
        if self.closed:
            raise BesoClosedError("BESO session is closed")
        wanted = {'monitor': self._monitor_loop}
        if self.settings.keepalive_interval > 0 and self._health_query is not None:
            wanted['keepalive'] = self._keepalive_loop
        for name, factory in wanted.items():
            task = self._tasks.get(name)
            if task is None or task.done():
                task = self.loop.create_task(factory(), name=f"beso-{name}-{self.address}")
                task.add_done_callback(functools.partial(self._on_task_done, name))
                self._tasks[name] = task

    def _on_task_done(self, name: str, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("BESO %s task for %s failed (restarted on next request): %r", name, self.address, exc,
                         exc_info=exc)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.worker.close()
        self._inflight.clear()
        self._raw.clear()
        self._converted.clear()

    # --------------------------------------------------------------------------------------- reads
    async def read(self, spec: ReadSpec) -> Any:
        if spec.unsupported:
            raise BesoUnsupportedError(spec.unsupported)
        self._ensure_tasks()
        expiry = spec.expire_marker
        if expiry is not None:
            since = self._markers.get(expiry.marker)
            if since is not None and self._clock() - since >= expiry.after:
                return expiry.value
        values = []
        for index, source in enumerate(spec.sources):
            values.append(await self._source_value(spec, index, source))
        if spec.combine is None:
            value = values[0]
        else:
            try:
                value = spec.combine(tuple(values))
            except BesoConversionError as exc:
                self._log_conversion(spec.key, exc)
                raise
        return copy.deepcopy(value) if isinstance(value, (list, dict)) else value

    async def _source_value(self, spec: ReadSpec, index: int, source: Source):
        if source.kind == 'constant':
            return source.ref
        if source.optional:
            try:
                return await self._required_source_value(spec, index, source)
            except (BesoDeviceError, BesoConversionError, BesoNoDataError) as exc:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("%s: optional source #%d unavailable: %s", spec.key, index + 1, exc)
                return None
        return await self._required_source_value(spec, index, source)

    async def _required_source_value(self, spec: ReadSpec, index: int, source: Source):
        if source.kind == 'exposure':
            await self._sync_exposure()
            value = self.tracker.field(source.ref)
            if source.parse:
                try:
                    value = source.parse(value)
                except BesoConversionError as exc:
                    self._log_conversion(spec.key, exc)
                    raise
            return value
        reply, at = await self._fresh(source.ref, spec.interval, READ)
        return self._convert(spec, index, source, reply, at)

    def _convert(self, spec: ReadSpec, index: int, source: Source, reply: str, at: float):
        key = (spec.key, index)
        cached = self._converted.get(key)
        if cached is None or cached[0] != at:
            try:
                cached = (at, source.parse(reply) if source.parse else reply, None)
            except BesoConversionError as exc:
                self._log_conversion(spec.key, exc)
                cached = (at, None, (exc.where, exc.step, exc.raw, exc.value, exc.reason))
            self._converted[key] = cached
        if cached[2] is not None:
            # A fresh instance each time: re-raising one stored exception would grow its traceback forever.
            raise BesoConversionError(*cached[2])
        return cached[1]

    async def _fresh(self, name: str, max_age: float, lane: int) -> Tuple[str, float]:
        """Reply of a query not older than ``max_age``; concurrent requests share one BESO round trip."""
        entry = self._raw.get(name)
        if entry is not None and self._clock() - entry[1] < max_age:
            return entry
        future = self._inflight.get(name)
        if future is None:
            query = self.config.queries[name]
            future = self.worker.submit(lane, functools.partial(self._run_query, query), label=f"query {name}")
            self._inflight[name] = future
            future.add_done_callback(functools.partial(self._query_done, name))
        # shield: one caller giving up must not cancel the round trip the others wait for
        return await asyncio.shield(future)

    async def _run_query(self, query: Query, executor: Executor) -> Tuple[str, float]:
        reply = await executor.exchange(query.command, query.timeout)
        self._observe_reply(query.command, reply)
        if self.settings.error_pattern.search(reply):
            raise BesoDeviceError(query.command, reply)
        if query.expect is not None and not query.expect.search(reply):
            raise BesoConversionError(f"query {query.name}", 'expect', reply, reply,
                                      f"reply does not match {query.expect.pattern!r}")
        return reply, self._clock()

    def _query_done(self, name: str, future: asyncio.Future) -> None:
        if self._inflight.get(name) is future:
            del self._inflight[name]
        if future.cancelled():
            return
        exc = future.exception()
        if exc is None:
            self._raw[name] = future.result()
        elif isinstance(exc, BesoConversionError):
            self._log_conversion(f"query {name}", exc)

    def _observe_reply(self, command: str, reply: str) -> None:
        """Every reply to the status/health command is seen here, whoever sent it."""
        if command == self._status_query.command:
            try:
                self.tracker.on_status_reply(reply)
            except BesoConversionError as exc:
                self._log_conversion('exposure status', exc)
            self._monitor_wakeup.set()
        elif self._health_query is not None and command == self._health_query.command:
            self._check_uptime(reply)

    def _check_uptime(self, reply: str) -> None:
        if self.settings.error_pattern.search(reply):
            return
        try:
            uptime = float(self.settings.health.parse(reply) if self.settings.health.parse else reply)
        except (BesoConversionError, TypeError, ValueError) as exc:
            should, _ = self._log.check('uptime-parse')
            if should:
                logger.warning("Cannot read BESO uptime from %r: %s", reply, exc)
            return
        if self._uptime is not None and uptime + 1.0 < self._uptime:
            logger.warning("BESO server at %s was restarted (uptime %.0f s < %.0f s); server-side settings "
                           "(FITS keys, shutter mode) are back to defaults", self.address, uptime, self._uptime)
            self._invalidate_all()
            self.tracker.mark_unknown()
        self._uptime = uptime

    async def _sync_exposure(self) -> None:
        tracker = self.tracker
        if tracker.phase == Phase.UNKNOWN:
            max_age = 0.0
        elif tracker.phase in (Phase.IDLE, Phase.ERROR):
            max_age = self.settings.exposure.idle_interval
        else:
            return  # active exposure: the monitor polls, readers use the extrapolation
        if tracker.status_age() < max_age:
            return
        try:
            await self._fresh(self._status_query.name, 0.0, READ)  # the tracker's age decided it is stale
        except BesoDeviceError:
            pass  # an ERROR status is a state (camerastate 5), already recorded by the tracker

    # -------------------------------------------------------------------------------------- writes
    async def write(self, spec: WriteSpec, data: dict) -> None:
        if spec.unsupported:
            raise BesoUnsupportedError(spec.unsupported)
        self._ensure_tasks()
        if spec.accept is not None:
            return self._accept(spec, data)
        values = self._prepare_args(spec, data)
        tracker = self.tracker
        if spec.handler == 'exposure_start':
            if tracker.phase == Phase.UNKNOWN:
                await self._sync_exposure()
            if tracker.is_busy():
                raise BesoBusyError("an exposure is already in progress or not yet written to disk")
        elif spec.blocked_during_exposure and tracker.is_busy():
            raise BesoBusyError("BESO rejects commands while an exposure is in progress")
        if spec.handler == 'exposure_abort':
            removed = self.worker.cancel_pending(
                lambda job: job.tag == START_TAG,
                BesoBusyError("startexposure cancelled by abortexposure before it reached BESO",
                              severity=SEVERITY_NORMAL))
            if removed:
                logger.info("abortexposure cancelled %d queued startexposure request(s)", removed)
        duration = None
        if spec.handler == 'exposure_start':
            duration = float(data[spec.duration_arg])
        state = {'started': False}

        async def run(executor: Executor):
            state['started'] = True
            if spec.handler is None and spec.blocked_during_exposure and tracker.is_busy():
                raise BesoBusyError("BESO rejects commands while an exposure is in progress")
            try:
                await self._run_steps(executor, spec, spec.steps, values)
            except BesoDeviceError as exc:
                if spec.on_error:
                    try:
                        await self._run_steps(executor, spec, spec.on_error, values)
                    except BesoDeviceError as cleanup_exc:
                        logger.warning("%s: on_error steps failed too: %s", spec.key, cleanup_exc)
                raise exc
            finally:
                self._invalidate(spec.invalidates)
            for marker in spec.set_markers:
                self._markers[marker] = self._clock()
            for marker in spec.clear_markers:
                self._markers.pop(marker, None)
            if spec.handler == 'exposure_start':
                tracker.mark_started(duration)
                self._monitor_wakeup.set()
            elif spec.handler == 'exposure_abort':
                await self._run_query_logged(executor, self._status_query)
            return None

        lane = CONTROL if spec.priority == 'control' else WRITE
        if spec.handler == 'exposure_start':
            tracker.mark_starting()
        try:
            future = self.worker.submit(lane, run, label=spec.key, max_wait=spec.max_queue_wait,
                                        tag=START_TAG if spec.handler == 'exposure_start' else None)
        except BaseException:
            if spec.handler == 'exposure_start':
                tracker.mark_start_failed(outcome_unknown=False)
            raise
        if spec.handler == 'exposure_start':
            future.add_done_callback(functools.partial(self._start_done, state))
        return await future

    def _start_done(self, state: dict, future: asyncio.Future) -> None:
        if future.cancelled():
            exc = None
        else:
            exc = future.exception()
            if exc is None:
                return
        unknown = state['started'] and (isinstance(exc, BesoLinkError) or
                                        (isinstance(exc, BesoDeviceError) and exc.unexpected))
        self.tracker.mark_start_failed(outcome_unknown=unknown)
        self._monitor_wakeup.set()

    async def _run_query_logged(self, executor: Executor, query: Query) -> None:
        try:
            await self._run_query(query, executor)
        except (BesoDeviceError, BesoConversionError) as exc:
            logger.warning("%s after abort: %s", query.command, exc)

    def _accept(self, spec: WriteSpec, data: dict) -> None:
        accept = spec.accept
        if accept.arg not in data:
            raise BesoArgumentError(f"missing argument {accept.arg!r}")
        raw = data[accept.arg]
        try:
            value = accept.convert(raw) if accept.convert else raw
        except BesoConversionError as exc:
            raise BesoArgumentError(f"invalid {accept.arg}={raw!r}: {exc.reason}") from None
        if not any(_same_value(value, allowed) for allowed in accept.values):
            raise BesoArgumentError(f"{accept.arg}={raw!r} is not supported by BESO; allowed: {list(accept.values)}")
        return None

    def _prepare_args(self, spec: WriteSpec, data: dict) -> Dict[str, str]:
        values = {}
        for arg in spec.args:
            if arg.source in data:
                raw = data[arg.source]
            elif arg.has_default:
                raw = arg.default
            else:
                raise BesoArgumentError(f"missing argument {arg.source!r}")
            try:
                value = arg.convert(raw) if arg.convert else raw
            except BesoConversionError as exc:
                raise BesoArgumentError(f"invalid {arg.source}={raw!r}: {exc.reason}") from None
            text = value if isinstance(value, str) else str(value)
            if any(ch in _FORBIDDEN_VALUE_CHARS for ch in text):
                raise BesoArgumentError(f"{arg.source}={raw!r} contains characters BESO does not accept")
            values[arg.name] = text
        return values

    async def _run_steps(self, executor: Executor, spec: WriteSpec, steps, values: Dict[str, str]) -> None:
        total = len(steps)
        for index, step in enumerate(steps):
            if isinstance(step, DelayStep):
                await asyncio.sleep(step.seconds)
            elif isinstance(step, WaitStep):
                await self._wait_until(executor, step)
            elif isinstance(step, CommandStep):
                command = step.template.format_map(values) if step.fields else step.template
                reply = await executor.exchange(command, step.timeout)
                self._observe_reply(command, reply)
                if self.settings.error_pattern.search(reply):
                    raise BesoDeviceError(command, reply, index, total)
                if step.expect is not None and not step.expect.search(reply):
                    raise BesoDeviceError(command, reply, index, total, unexpected=True)

    async def _wait_until(self, executor: Executor, step: WaitStep) -> None:
        query = self.config.queries[step.query]
        deadline = self._clock() + step.timeout
        while True:
            reply = await executor.exchange(query.command, query.timeout)
            self._observe_reply(query.command, reply)
            if self.settings.error_pattern.search(reply):
                raise BesoDeviceError(query.command, reply)
            try:
                if step.condition(reply):
                    return
            except BesoConversionError as exc:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("wait_until %s: %s", query.command, exc)
            if self._clock() + step.interval > deadline:
                raise BesoDeviceError(query.command, reply, unexpected=True)
            await asyncio.sleep(step.interval)

    # ------------------------------------------------------------------------------ cache handling
    def _invalidate(self, queries) -> None:
        for name in queries:
            self._raw.pop(name, None)

    def _invalidate_all(self) -> None:
        self._raw.clear()
        self._converted.clear()

    async def _on_connected(self, executor: Executor) -> None:
        """Resynchronise after (re)connecting: the device may have changed while we were away."""
        self._invalidate_all()
        for query in (self._health_query, self._status_query):
            if query is None:
                continue
            try:
                reply, at = await self._run_query(query, executor)
                self._raw[query.name] = (reply, at)
            except (BesoDeviceError, BesoConversionError) as exc:
                logger.warning("BESO resync query %s failed: %s", query.command, exc)

    def _on_link_lost(self) -> None:
        self._invalidate_all()
        self.tracker.mark_unknown()
        self._monitor_wakeup.set()

    def _log_conversion(self, key: str, exc: BesoConversionError) -> None:
        should, suppressed = self._log.check(('conversion', key))
        if should:
            logger.warning("BESO value conversion failed for %s: %s%s", key, exc,
                           f" ({suppressed} similar failures suppressed)" if suppressed else "")

    # ----------------------------------------------------------------------------- background tasks
    async def _monitor_loop(self) -> None:
        tracker = self.tracker
        while True:
            if not tracker.needs_monitoring():
                self._monitor_wakeup.clear()
                await self._monitor_wakeup.wait()
                continue
            self._monitor_wakeup.clear()
            try:
                await asyncio.wait_for(self._monitor_wakeup.wait(), tracker.next_poll_delay())
                continue  # state changed (start, abort, status from elsewhere): re-evaluate the schedule
            except asyncio.TimeoutError:
                pass
            if not tracker.needs_monitoring():
                continue
            try:
                await self._fresh(self._status_query.name, 0.0, MONITOR)
            except BesoDeviceError:
                pass
            except BesoError as exc:
                should, suppressed = self._log.check(('monitor', type(exc)))
                if should:
                    logger.warning("BESO exposure monitor could not read status: %s%s", exc,
                                   f" ({suppressed} similar suppressed)" if suppressed else "")

    async def _keepalive_loop(self) -> None:
        interval = self.settings.keepalive_interval
        while True:
            await asyncio.sleep(interval)
            if self.worker.connected and self.worker.idle_for < interval:
                continue
            try:
                await self._fresh(self._health_query.name, 0.0, BACKGROUND)
            except BesoError:
                pass  # outages are logged once by the worker


# ------------------------------------------------------------------------------------------ registry

_SESSIONS: Dict[str, BesoSession] = {}


def acquire_session(address: str, config: BesoConfig) -> BesoSession:
    host, port = parse_address(address)
    key = f"{host}:{port}"
    loop = asyncio.get_running_loop()
    session = _SESSIONS.get(key)
    if session is None or session.closed or session.loop is not loop:
        session = BesoSession(key, config)
        _SESSIONS[key] = session
    elif session.config is not config:
        raise BesoConfigError(f"BESO at {key} is used with two different mapping configurations")
    session.refs += 1
    return session


async def release_session(session: BesoSession) -> None:
    session.refs -= 1
    if session.refs > 0:
        return
    if _SESSIONS.get(session.address) is session:
        del _SESSIONS[session.address]
    await session.close()


def active_sessions() -> Dict[str, BesoSession]:
    return dict(_SESSIONS)
