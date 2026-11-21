"""The link to one Pilar (:class:`PilarLink`): the pool of TPL2 connections, the watcher and its snapshot,
freezes and outages, the motion of the mount, background operations.

One link per Pilar address is shared by every component with that address (``PilarConnector._links``). How Pilar
freezes and what the link does about it: the docstring of ``pilar_connector.py`` and ``watcher`` in
``pilar_config.yml``.
"""
import asyncio
import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple

from obsrv.protocols.pilar.pilar_exceptions import (PilarBusy, PilarDeviceError, PilarReplyError, PilarTransient,
                                                    PilarUnavailable, PilarUnsupported)
from obsrv.protocols.pilar.pilar_settings import PilarSettings
from obsrv.protocols.pilar.pilar_tpl2 import (Tpl2Busy, Tpl2CommandError, Tpl2Item, Tpl2LinkError, Tpl2Pool,
                                              Tpl2ProtocolError, Tpl2Reply, Tpl2Timeout, format_value)

logger = logging.getLogger('pilar_connector')     # one logger for the whole Pilar connector


def sky_distance(az1: float, alt1: float, az2: float, alt2: float) -> float:
    """Angle [deg] between two horizontal positions (haversine)."""
    p1, p2 = math.radians(alt1), math.radians(alt2)
    dp, dl = p2 - p1, math.radians(az2 - az1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return math.degrees(2 * math.asin(min(1.0, math.sqrt(h))))


@dataclass
class _Motion:
    """A mount move the connector commanded: until it ends, the mount is slewing."""
    kind: str                                   # 'altaz', 'track', 'abort'
    since: float
    start_delay: float
    deadline: float
    target: Optional[Tuple[float, float]] = None


@dataclass
class _Operation:
    key: str                                    # 'mount', 'cover', 'focuser', 'rotator<n>'
    name: str
    task: asyncio.Task
    target: Any = None
    kind: str = ''                              # mount: 'power', 'abort' or 'queued' (waits for a freeze end)


class PilarLink:
    """Everything the connector keeps about one Pilar: connections, the watcher and its snapshot, freezes,
    mount motion, background operations."""

    def __init__(self, host: str, port: int, settings: PilarSettings):
        s = settings
        self.settings = s
        self.address = f'{host}:{port}'
        self.loop = asyncio.get_running_loop()
        self.pool = Tpl2Pool(host, port, size=s.pool_size, reserved=s.reserved_for_stop,
                             long_max=s.long_connections, id_range=s.command_ids, encoding=s.encoding,
                             connect_timeout=s.connect_timeout, greeting_timeout=s.greeting_timeout,
                             wait_timeout=s.pool_wait, cooldown=s.reconnect_cooldown,
                             stalled_limit=s.stalled_connection, on_event=self._on_event)
        self.closed = False
        self._watched: Dict[str, str] = {}
        for var in (s.state_ready, s.state_track, s.state_azimuth, s.state_altitude):
            self._watched[var.upper()] = var
        self._snapshot: Dict[str, Tuple[float, Tpl2Item]] = {}
        self._forgotten: Dict[str, float] = {}    # variable -> when its snapshot value became out of date
        self._samples: Deque[Tuple[float, float, float]] = deque(maxlen=100)
        self._last_fast = -math.inf
        self._good: Dict[str, Tuple[float, Tpl2Item]] = {}   # last values read outside freezes and guards
        self.polled_at: Optional[float] = None
        self.first_poll = asyncio.Event()
        self._unanswered_since: Optional[float] = None    # the watcher waits for Pilar since then
        self._freeze_logged = False
        self._guard_until = 0.0
        self.outage = False
        self.last_freeze_end = -math.inf
        self.freezes: Deque[Tuple[float, float]] = deque(maxlen=50)
        self.tracking_wanted = False
        self.equatorial_target = False          # an equatorial target was set by this connector
        self._track_command_at = -math.inf
        self.motion: Optional[_Motion] = None
        self.operations: Dict[str, _Operation] = {}
        self._limits: Dict[str, float] = {}
        self.focus_syncmode_saved: Optional[int] = None
        self.derotator_saved: Optional[Tuple[Any, Any]] = None   # (SYNCMODE, OFFSET) before a move in tracking
        self.rotator_targets: Dict[int, float] = {}
        self.focus_target: Optional[float] = None   # [mm] where the last focuser move of the connector ended
        self._retrack_task: Optional[asyncio.Task] = None
        self._watch_task = self.loop.create_task(self._watch_loop(), name=f'pilar-watcher-{self.address}')

    # ------------------------------------------------------------------ state of the link

    def _unanswered(self) -> float:
        """How long the watcher has been waiting for Pilar (0 when it is not waiting)."""
        since = self._unanswered_since
        return time.monotonic() - since if since is not None else 0.0

    def frozen(self) -> bool:
        """Pilar has not answered the watcher for ``freeze_after``."""
        return self._unanswered() > self.settings.freeze_after

    def in_guard(self) -> bool:
        return time.monotonic() < self._guard_until

    def quiet(self) -> bool:
        """A freeze (also one only suspected: no answer for ``freeze_suspect_after``) or the guard after it: reads
        are answered with the last good values, commands wait for the end."""
        return self._unanswered() > self.settings.freeze_suspect_after or self.in_guard()

    def unavailable(self) -> bool:
        """Pilar cannot be reached: an outage, or new connections refused while the watcher gets no answers."""
        if self.outage:
            return True
        if not self.pool.down:
            return False
        recent = self.polled_at is not None and \
            time.monotonic() - self.polled_at <= self.settings.freeze_after + self.settings.poll_interval
        return not recent

    def settled(self) -> bool:
        return not self.quiet() and not self.unavailable()

    def raise_unavailable(self) -> None:
        if self.outage:
            raise PilarUnavailable(f"Pilar at {self.address} has not answered for over "
                                   f"{self.settings.outage_after:.0f} s")
        raise PilarUnavailable(f"Pilar at {self.address} not reachable")

    def check_readable(self) -> None:
        if self.unavailable():
            self.raise_unavailable()
        if self.frozen():
            raise PilarTransient(f"Pilar at {self.address} frozen for {self._unanswered():.0f} s")

    async def wait_settled(self, timeout: Optional[float] = None) -> None:
        """Wait for the end of a freeze and of the guard after it; 4005 TEMPORARY after ``timeout``."""
        timeout = self.settings.command_freeze_wait if timeout is None else timeout
        deadline = time.monotonic() + timeout
        while not self.settled():
            if self.unavailable():
                self.raise_unavailable()
            if time.monotonic() >= deadline:
                raise PilarTransient(f"Pilar at {self.address} frozen, no answer in {timeout:.0f} s")
            await asyncio.sleep(0.05)

    # ------------------------------------------------------------------ commands

    async def command(self, body: str, *, purpose: str = 'general', timeout: Optional[float] = None,
                      through_freeze: bool = True) -> Tpl2Reply:
        """Send one command and return its reply. A command (``through_freeze``) waits for the end of a freeze
        before it is sent and keeps waiting for its reply while Pilar is frozen; a read fails at once."""
        if self.unavailable():
            self.raise_unavailable()
        if self.quiet():
            if not through_freeze:
                self.check_readable()
            await self.wait_settled()
        timeout = self.settings.command_timeout if timeout is None else timeout
        async with self.pool.lease(purpose) as conn:
            request = conn.send(body)
            try:
                return await self._await_reply(request, timeout, through_freeze)
            except BaseException:
                conn.abandon(request)
                raise

    async def _await_reply(self, request, timeout: float, through_freeze: bool) -> Tpl2Reply:
        deadline = time.monotonic() + timeout
        while True:
            now = time.monotonic()
            if through_freeze and self.frozen():
                deadline = max(deadline, now + timeout)
            remaining = deadline - now
            if remaining <= 0:
                raise Tpl2Timeout(f"no reply from Pilar at {self.address} to {request.body!r} in {timeout:.1f} s")
            done, _ = await asyncio.wait({request.future}, timeout=min(remaining, 0.1))
            if done:
                return request.future.result()
            if self.outage:
                self.raise_unavailable()
            if not through_freeze and self.frozen():
                self.check_readable()

    def check_reply(self, reply: Tpl2Reply) -> None:
        if reply.command_error:
            raise Tpl2CommandError(reply.body, reply.command_error)

    def forget(self, *variables: str) -> None:
        """The snapshot values of ``variables`` are out of date (a SET changed them): the next read asks Pilar."""
        now = time.monotonic()
        for var in variables:
            self._snapshot.pop(var.upper(), None)
            self._good.pop(var.upper(), None)
            self._forgotten[var.upper()] = now

    async def set(self, var: str, value: Any, *, purpose: str = 'general', timeout: Optional[float] = None) -> None:
        self.forget(var)
        try:
            reply = await self.command(f'SET {var}={format_value(value)}', purpose=purpose, timeout=timeout)
        finally:
            self.forget(var)
        self.check_reply(reply)
        item = reply.items.get(var.upper())
        if item is None:
            if reply.failed:
                raise PilarDeviceError(f"Pilar refused SET {var}")
            raise Tpl2ProtocolError(f"reply to SET {var} has no line for {var}")
        if not item.ok:
            self.value(item, var)

    async def read(self, var: str, *, fresh: bool = False, watch: bool = True) -> Tpl2Item:
        """The value of one variable: from the snapshot when fresh enough, otherwise one GET (and from then on
        the watcher reads it too). During a freeze and the guard after it: the last value read before the
        freeze, if not older than ``freeze_snapshot_max_age``."""
        key = var.upper()
        if watch:
            self._watched.setdefault(key, var)
        if not fresh:
            now = time.monotonic()
            if self.quiet():
                if self.unavailable():
                    self.raise_unavailable()
                good = self._good.get(key)
                if good is not None and now - good[0] <= self.settings.freeze_snapshot_max_age:
                    return good[1]
                raise PilarTransient(f"Pilar at {self.address} frozen, no recent value of {var}")
            snap = self._snapshot.get(key)
            if snap is not None and now - snap[0] <= self.settings.snapshot_max_age:
                return snap[1]
        items = await self.read_many([var], fresh=True)
        return items[key]

    async def read_many(self, variables: List[str], *, fresh: bool = True, purpose: str = 'general') \
            -> Dict[str, Tpl2Item]:
        self.check_readable()
        reply = await self.command('GET ' + ';'.join(variables), purpose=purpose, through_freeze=False)
        self.check_reply(reply)
        now = time.monotonic()
        out = {}
        for var in variables:
            item = reply.item(var)
            out[var.upper()] = item
            if var.upper() in self._watched:
                self._snapshot[var.upper()] = (now, item)
                if not self.in_guard():
                    self._good[var.upper()] = (now, item)
        return out

    def value(self, item: Tpl2Item, var: str) -> Any:
        """The value of an item, or the error its special value means."""
        if item.ok:
            return item.value
        special = item.special
        detail = f" ({item.message})" if item.message else ''
        if special.kind in ('UNKNOWN', 'INVALID', 'DIMENSION'):
            raise PilarUnsupported(f"Pilar has no variable {var} ({special.kind})")
        if special.kind == 'LOCKEDBY':
            raise PilarBusy(f"{var} is locked by Pilar command {special.detail}")
        if special.kind == 'FAILED' and self.quiet():
            raise PilarTransient(f"{var}: FAILED just after a Pilar freeze{detail}")
        raise PilarDeviceError(f"Pilar: {var} {special}{detail}")

    def number(self, item: Tpl2Item, var: str) -> float:
        value = self.value(item, var)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise PilarReplyError(f"Pilar: {var} = {value!r}, a number was expected")
        return float(value)

    async def read_number(self, var: str, *, fresh: bool = False, watch: bool = True) -> float:
        return self.number(await self.read(var, fresh=fresh, watch=watch), var)

    def cached_limit(self, var: str) -> Optional[float]:
        return self._limits.get(var)

    async def limit(self, var: str) -> float:
        """A value which does not change (a limit), read once."""
        if var not in self._limits:
            self._limits[var] = await self.read_number(var, fresh=True, watch=False)
        return self._limits[var]

    async def require_ready(self) -> None:
        """Wait for a settled link and check that the telescope is on (4009 when it is not)."""
        await self.wait_settled()
        ready = await self.read_number(self.settings.state_ready, fresh=True)
        if ready != self.settings.ready_value:
            raise PilarDeviceError(f"telescope is not ready (READY_STATE={ready:g})")

    # ------------------------------------------------------------------ watcher

    async def _watch_loop(self) -> None:
        s = self.settings
        while not self.closed:
            started = time.monotonic()
            try:
                await self._poll()
            except asyncio.CancelledError:
                raise
            except (Tpl2LinkError, Tpl2Busy, Tpl2Timeout, PilarUnavailable) as e:
                logger.debug(f"Pilar {self.address}: watcher: {e}")
                self.first_poll.set()
                self._check_outage()
                await asyncio.sleep(s.poll_interval)
                continue
            except Exception as e:
                logger.warning(f"Pilar {self.address}: watcher: {type(e).__name__}: {e}")
            self.first_poll.set()
            await asyncio.sleep(max(0.0, started + s.poll_interval - time.monotonic()))

    async def _poll(self) -> None:
        """One GET of the watched variables. The time without an answer counts from the first unanswered attempt
        (also a connect waiting for the greeting of a frozen Pilar)."""
        s = self.settings
        if self._unanswered_since is None:
            self._unanswered_since = time.monotonic()
        variables = list(self._watched.values())
        async with self.pool.lease('watcher') as conn:
            request = conn.send('GET ' + ';'.join(variables))
            sent = time.monotonic()
            try:
                while not request.future.done():
                    if self.frozen() and not self._freeze_logged:
                        self._freeze_logged = True
                        logger.debug(f"Pilar {self.address}: no answer for {s.freeze_after:g} s, frozen")
                    if self._check_outage():          # the outage begins now: drop the hanging connection
                        conn.close()
                        raise PilarUnavailable("outage")
                    await asyncio.wait({request.future}, timeout=s.poll_interval / 2)
                reply = request.future.result()
            except BaseException:
                conn.abandon(request)
                raise
        now = time.monotonic()
        since = self._unanswered_since
        self._unanswered_since = None
        self._freeze_logged = False
        if since is not None and (now - since > s.freeze_after or self.outage):
            self._freeze_ends(since, now)
        self.polled_at = now
        self._store(reply, now, sent)

    def _check_outage(self) -> bool:
        """True when an outage begins now (no answer for ``outage_after``)."""
        if not self.outage and self._unanswered() > self.settings.outage_after:
            self.outage = True
            logger.error(f"Pilar {self.address}: no answer for {self.settings.outage_after:g} s, outage; "
                         f"tracking will not be resumed")
            self.tracking_wanted = False
            return True
        return False

    def _freeze_ends(self, since: float, now: float) -> None:
        self._guard_until = now + self.settings.post_freeze_guard
        self.last_freeze_end = now
        self.freezes.append((since, now))
        if self.outage:
            self.outage = False
            logger.warning(f"Pilar {self.address} answers again after an outage of {now - since:.0f} s")
            return
        logger.info(f"Pilar {self.address} answers again after a freeze of {now - since:.1f} s")
        if self.tracking_wanted and (self._retrack_task is None or self._retrack_task.done()):
            self._retrack_task = self.loop.create_task(self._retrack(now - since), name='pilar-retrack')

    def _store(self, reply: Tpl2Reply, now: float, sent: float) -> None:
        s = self.settings
        good = not self.in_guard()
        for key in list(self._watched):
            item = reply.items.get(key)
            if item is not None and self._forgotten.get(key, -math.inf) < sent:
                self._snapshot[key] = (now, item)
                if good:
                    self._good[key] = (now, item)
        az = reply.items.get(s.state_azimuth.upper())
        alt = reply.items.get(s.state_altitude.upper())
        if az is not None and alt is not None and az.ok and alt.ok \
                and isinstance(az.value, (int, float)) and isinstance(alt.value, (int, float)):
            self._add_sample(now, float(az.value), float(alt.value))
        track = reply.items.get(s.state_track.upper())
        if (self.tracking_wanted and track is not None and track.ok and track.value != s.tracking_value
                and self.settled()
                and now - self.last_freeze_end > s.retrack_window
                and now - self._track_command_at > s.start_delay['track'] + s.settle_time
                and (self._retrack_task is None or self._retrack_task.done())):
            self.tracking_wanted = False
            logger.warning(f"Pilar {self.address}: tracking stopped by Pilar (TRACK={track.value}) without a "
                           f"freeze; not resumed")

    def _add_sample(self, now: float, az: float, alt: float) -> None:
        self._samples.append((now, az, alt))
        older = [smp for smp in self._samples if now - smp[0] >= self.settings.speed_window]
        base = older[-1] if older else self._samples[0]
        dt = now - base[0]
        if dt >= self.settings.speed_window / 2 and sky_distance(base[1], base[2], az, alt) / dt \
                > self.settings.slew_speed:
            self._last_fast = now

    async def _retrack(self, freeze_length: float) -> None:
        s = self.settings
        await asyncio.sleep(s.post_freeze_guard)
        for attempt in range(1, s.retrack_attempts + 1):
            if not self.tracking_wanted or self.closed:
                return
            if self.operation_active('mount'):
                return
            try:
                await self.wait_settled()
                ready = await self.read_number(s.state_ready, fresh=True)
                track = await self.read(s.state_track, fresh=True)
                if ready == s.ready_value and track.ok and track.value == s.tracking_value:
                    return
                if ready == s.ready_value:
                    await self.set(s.state_track, s.tracking_value)
                    self.note_motion('track')
                    logger.info(f"Pilar {self.address}: tracking resumed after a freeze of {freeze_length:.1f} s")
                    return
                logger.info(f"Pilar {self.address}: tracking not resumed yet, READY_STATE={ready:g} "
                            f"(attempt {attempt})")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Pilar {self.address}: resuming tracking failed (attempt {attempt}): {e}")
            await asyncio.sleep(s.retrack_retry)
        logger.warning(f"Pilar {self.address}: tracking not resumed after the freeze")

    def _on_event(self, cmd_id: int, text: str) -> None:
        # e.g. 'ERROR TELESCOPE:0 Disconnected from telescope' at the end of some freezes, 'INFO POINTING.TRACK:0
        # Tracking started'; the freezes themselves are logged by the watcher
        logger.debug(f"Pilar {self.address} event ({cmd_id}): {text}")

    # ------------------------------------------------------------------ mount motion

    def note_motion(self, kind: str, target: Optional[Tuple[float, float]] = None) -> None:
        now = time.monotonic()
        if kind == 'track':
            self._track_command_at = now
        self.motion = _Motion(kind, now, self.settings.start_delay[kind], now + self.settings.max_slew_time,
                              target)

    def position(self) -> Optional[Tuple[float, float]]:
        return (self._samples[-1][1], self._samples[-1][2]) if self._samples else None

    def motion_active(self) -> bool:
        """A slew the connector commanded has not ended yet."""
        m = self.motion
        if m is None:
            return False
        s = self.settings
        now = time.monotonic()
        if now > m.deadline:
            logger.warning(f"Pilar {self.address}: {m.kind} slew not ended after {s.max_slew_time:.0f} s; "
                           f"taken as ended")
            self.motion = None
            return False
        if now < m.since + m.start_delay:
            return True
        if self.quiet():
            return True
        if m.target is not None:
            position = self.position()
            if position is None or sky_distance(*position, *m.target) > s.arrival_tolerance:
                return True
        if now - self._last_fast < s.settle_time:
            return True
        self.motion = None
        return False

    def slewing(self) -> bool:
        op = self.operations.get('mount')
        if op is not None and not op.task.done():
            return True
        if self.motion_active():
            return True
        return time.monotonic() - self._last_fast < self.settings.settle_time

    def mount_commanded_busy(self) -> bool:
        """The mount is doing what the connector asked: power, abort, a slew."""
        op = self.operations.get('mount')
        return (op is not None and not op.task.done()) or self.motion_active()

    async def wait_mount_idle(self, timeout: float) -> None:
        """Wait until the mount stands (or tracks) and the link is settled."""
        deadline = time.monotonic() + timeout
        while self.slewing() or not self.settled():
            if self.unavailable():
                self.raise_unavailable()
            if time.monotonic() >= deadline:
                raise PilarBusy(f"the mount did not stop in {timeout:.0f} s")
            await asyncio.sleep(self.settings.poll_interval / 2)

    # ------------------------------------------------------------------ background operations

    def operation_active(self, key: str) -> Optional[_Operation]:
        op = self.operations.get(key)
        return op if op is not None and not op.task.done() else None

    def start_operation(self, key: str, name: str, coro, target: Any = None, kind: str = '') -> _Operation:
        task = self.loop.create_task(coro, name=f'pilar-{name}')
        op = _Operation(key, name, task, target, kind)
        self.operations[key] = op

        def done(t: asyncio.Task) -> None:
            if t.cancelled():
                logger.info(f"Pilar {self.address}: {name} cancelled")
                return
            error = t.exception()
            if error is not None:
                logger.warning(f"Pilar {self.address}: {name} failed: {type(error).__name__}: {error}")
            else:
                logger.info(f"Pilar {self.address}: {name} done")

        task.add_done_callback(done)
        return op

    async def cancel_operation(self, key: str) -> None:
        op = self.operation_active(key)
        if op is not None:
            op.task.cancel()
            try:
                await op.task
            except BaseException:
                pass

    # ------------------------------------------------------------------ derotator restore

    async def restore_derotator(self) -> None:
        """Give the derotator back its SYNCMODE and OFFSET from before a move in tracking."""
        saved = self.derotator_saved
        if saved is None:
            return
        d = self.settings.derotator
        await self.set(d.str('syncmode'), saved[0])
        await self.set(d.str('offset'), saved[1])
        self.derotator_saved = None

    # ------------------------------------------------------------------ closing

    async def aclose(self) -> None:
        self.closed = True
        tasks = [op.task for op in self.operations.values() if not op.task.done()]
        if self._retrack_task is not None and not self._retrack_task.done():
            tasks.append(self._retrack_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=self.settings.command_timeout * 2)
        watcher = self._watch_task
        self.close()
        try:
            await asyncio.wait({watcher}, timeout=self.settings.command_timeout)
        except Exception:
            pass
        await self.pool.aclose()

    def close(self) -> None:
        self.closed = True
        tasks = [op.task for op in self.operations.values()] + [self._retrack_task, self._watch_task]
        for task in tasks:
            if task is not None and not task.done():
                try:
                    task.cancel()
                except RuntimeError:       # the loop of the task is closed (tests, server restart)
                    pass
        self.pool.close()
