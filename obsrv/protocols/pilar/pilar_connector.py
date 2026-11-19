"""Pilar connector: mount, mirror covers, focuser and derotator of IRIS through Pilar 3.3 (TPL2 / OpenTSI).

What the connector does about the ways of Pilar (measured on the instrument 18-19.11.2026; numbers and the
mapping of Ocabox API methods are in ``pilar_config.yml``):

* one :class:`PilarLink` per Pilar address is shared by every component with that address (mount, covers,
  focuser, the rotator below the mount): one pool of connections (Pilar takes at most 10 clients) and one
  watcher;
* the watcher reads the state (``READY_STATE``, ``TRACK``, the mount position and every variable a client
  reads) in one GET every ``poll_interval``; client reads are answered from this snapshot;
* freezes: every 58-82 s Pilar answers nothing for 16-18 s, motion goes on, and at the end ``TRACK`` drops to 0.
  During a freeze a read fails at once with 4005 TEMPORARY and a command waits for its end; for
  ``post_freeze_guard`` after it ``FAILED`` and ``READY_STATE=0`` are taken as transient; tracking which the
  client asked for is resumed; a freeze longer than ``outage_after`` is an outage (4005 NORMAL, no resuming);
* ``TELESCOPE.STOP``, ``ABORT`` and ``TRACK=0`` do not stop a slew: ``abortslew`` sends a new target, the
  current position;
* the focuser moves only via the pointing: a ``SYNCMODE`` bit for the time of the move + ``POSITION`` +
  ``TRACK=4``;
* the mount has priority: a focuser or rotator move asked during a slew waits for its end; a slew asked during
  such a move goes at once, and the move goes on after it;
* long operations (park, unpark, covers, focuser, rotator, abortslew) run in the background: the request
  returns at once (``slewtoaltaz`` / ``slewtocoordinates`` wait for the end of the slew) and a second command for
  a busy device gets 4008.
"""
import asyncio
import logging
import math
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

import confuse
from obcom.data_colection.address import AddressError
from obcom.data_colection.coded_error import BaseCodedError, TreeOtherError, TreeStructureError
from obcom.data_colection.value import TreeValueError

from obsrv.protocols.alpaca.alpaca_connector import Connector
from obsrv.protocols.pilar.pilar_tpl2 import (Tpl2Busy, Tpl2CommandError, Tpl2Item, Tpl2LinkError, Tpl2Pool,
                                               Tpl2ProtocolError, Tpl2Reply, Tpl2Timeout, format_value)

logger = logging.getLogger(__name__.rsplit('.')[-1])

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'pilar_config.yml')

_DEVICE_NUMBER = '{device_number}'


# ----------------------------------------------------------------------------------------------- exceptions

class PilarError(Exception):
    """Base of the Pilar connector errors (translated to Ocabox codes in :meth:`PilarConnector._serve`)."""


class PilarConfigError(PilarError):
    """The protocol or the component configuration is wrong (3001)."""


class PilarUnsupported(PilarError):
    """Pilar does not have the variable (``UNKNOWN``, ``INVALID``, ``DIMENSION``) (3002)."""


class PilarTransient(PilarError):
    """A blip: a freeze, the time just after it, a single missed reply (4005 TEMPORARY)."""


class PilarUnavailable(PilarError):
    """Pilar cannot be reached for longer: refused connection, outage (4005 NORMAL)."""


class PilarBusy(PilarError):
    """The device is busy with another operation, or a variable is locked (4008 TEMPORARY)."""


class PilarDeviceError(PilarError):
    """Pilar answered but said no: ``FAILED``, ``DENIED``, telescope not ready (4009)."""


class PilarReplyError(PilarError):
    """A reply the connector cannot use, e.g. text where a number is needed (2002)."""


# ----------------------------------------------------------------------------------------------- settings

def load_protocol_config(path: str = CONFIG_PATH) -> dict:
    config = confuse.Configuration('PilarConnector', __name__, read=False)
    config.set_file(path)
    return config.get()


class _Section:
    """A part of the configuration with typed, checked access; errors name the path of the key."""

    def __init__(self, data: Any, path: str):
        if not isinstance(data, dict):
            raise PilarConfigError(f"{path}: must be a mapping")
        self.data = data
        self.path = path

    def _get(self, key: str):
        if key not in self.data or self.data[key] is None:
            raise PilarConfigError(f"{self.path}.{key}: missing")
        return self.data[key]

    def num(self, key: str) -> float:
        value = self._get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PilarConfigError(f"{self.path}.{key}: must be a number, got {value!r}")
        return float(value)

    def int(self, key: str) -> int:
        value = self._get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise PilarConfigError(f"{self.path}.{key}: must be an integer, got {value!r}")
        return value

    def str(self, key: str) -> str:
        value = self._get(key)
        if not isinstance(value, str) or not value.strip():
            raise PilarConfigError(f"{self.path}.{key}: must be a TPL2 variable name, got {value!r}")
        return value.strip()

    def value(self, key: str):
        return self._get(key)

    def section(self, key: str) -> '_Section':
        return _Section(self._get(key), f'{self.path}.{key}')

    def optional_section(self, key: str) -> '_Section':
        data = self.data.get(key)
        return _Section(data if data is not None else {}, f'{self.path}.{key}')


@dataclass
class MappingEntry:
    """One method of the mapping: a TPL2 variable or a procedure of the connector with its options."""
    variable: Optional[str] = None
    procedure: Optional[str] = None
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ComponentMapping:
    get: Dict[str, MappingEntry]
    put: Dict[str, MappingEntry]
    actions: Dict[str, MappingEntry]
    fixed: Dict[str, Any]


class PilarSettings:
    """``pilar_config.yml`` read and checked once, when the connector is created."""

    def __init__(self, cfg: dict, procedures: Dict[str, Tuple[str, str]]):
        root = _Section(cfg, 'pilar_config')
        link = root.section('link')
        self.encoding = link.str('encoding')
        self.pool_size = link.int('pool_size')
        self.reserved_for_stop = link.int('reserved_for_stop')
        self.long_connections = link.int('long_connections')
        ids = link.value('command_ids')
        if not (isinstance(ids, list) and len(ids) == 2 and all(isinstance(i, int) for i in ids)
                and 0 < ids[0] < ids[1]):
            raise PilarConfigError("pilar_config.link.command_ids: must be [first, last], 0 < first < last")
        self.command_ids = (ids[0], ids[1])
        self.reconnect_cooldown = link.num('reconnect_cooldown')
        if self.reserved_for_stop < 0 or self.pool_size < self.reserved_for_stop + 2:
            raise PilarConfigError("pilar_config.link.pool_size: must be at least reserved_for_stop + 2 "
                                   "(one connection of the watcher and one for everything else)")

        t = root.section('timeouts')
        self.connect_timeout = t.num('connect')
        self.greeting_timeout = t.num('greeting')
        self.command_timeout = t.num('command')
        self.pool_wait = t.num('pool_wait')
        self.stalled_connection = t.num('stalled_connection')

        w = root.section('watcher')
        self.poll_interval = w.num('poll_interval')
        self.snapshot_max_age = w.num('snapshot_max_age')
        self.freeze_after = w.num('freeze_after')
        self.post_freeze_guard = w.num('post_freeze_guard')
        self.outage_after = w.num('outage_after')
        self.command_freeze_wait = w.num('command_freeze_wait')
        self.retrack_attempts = w.int('retrack_attempts')
        self.retrack_retry = w.num('retrack_retry')
        self.retrack_window = w.num('retrack_window')

        s = root.section('state')
        self.state_ready = s.str('ready_state')
        self.ready_value = s.num('ready_value')
        self.off_value = s.num('off_value')
        self.state_track = s.str('track')
        self.tracking_value = s.num('tracking_value')
        self.state_azimuth = s.str('azimuth')
        self.state_altitude = s.str('altitude')

        m = root.section('mount')
        self.slew_speed = m.num('slew_speed')
        self.speed_window = m.num('speed_window')
        self.settle_time = m.num('settle_time')
        self.arrival_tolerance = m.num('arrival_tolerance')
        self.start_delay = {'altaz': m.num('start_delay_altaz'), 'track': m.num('start_delay_track'),
                            'abort': m.num('start_delay_abort')}
        self.max_slew_time = m.num('max_slew_time')
        self.sync_slew_timeout = m.num('sync_slew_timeout')
        self.queue_wait = m.num('queue_wait')

        p = root.section('procedures')
        self.power = p.section('power')
        self.slew_altaz = p.section('slew_altaz')
        self.slew_equatorial = p.section('slew_equatorial')
        self.tracking = p.section('tracking')
        self.abort_slew = p.section('abort_slew')
        self.cover = p.section('cover')
        self.focuser = p.section('focuser')
        self.derotator = p.section('derotator')
        self._check_procedures()

        self.components: Dict[str, ComponentMapping] = {}
        comps = root.section('components')
        for kind, data in comps.data.items():
            c = _Section(data, f'pilar_config.components.{kind}')
            self.components[kind] = ComponentMapping(
                get=self._entries(c.optional_section('get'), procedures, 'get'),
                put=self._entries(c.optional_section('put'), procedures, 'put'),
                actions=self._entries(c.optional_section('actions'), procedures, None),
                fixed={str(k).lower(): v for k, v in c.optional_section('fixed').data.items()},
            )

    def _check_procedures(self) -> None:
        """Read every key the procedures use, so a typo shows when the connector is created."""
        for key in ('ready',):
            self.power.str(key)
        for key in ('timeout_on', 'timeout_off'):
            self.power.num(key)
        for section in (self.slew_altaz, self.abort_slew):
            for key in ('azimuth', 'altitude', 'track'):
                section.str(key)
            section.num('track_value')
        self.slew_altaz.str('zd_limit')
        epoch = self.slew_equatorial.value('epoch')
        if not (isinstance(epoch, list) and epoch and all(isinstance(v, str) for v in epoch)):
            raise PilarConfigError("pilar_config.procedures.slew_equatorial.epoch: must be a list of variables")
        before = self.slew_equatorial.data.get('set_before') or {}
        if not isinstance(before, dict):
            raise PilarConfigError("pilar_config.procedures.slew_equatorial.set_before: must be a mapping")
        for key in ('ra', 'dec', 'track'):
            self.slew_equatorial.str(key)
        self.slew_equatorial.num('track_value')
        for key in ('track', 'current_ra', 'current_dec'):
            self.tracking.str(key)
        for key in ('on_value', 'off_value'):
            self.tracking.num(key)
        for key in ('target', 'position'):
            self.cover.str(key)
        for key in ('open_value', 'closed_value', 'tolerance', 'timeout'):
            self.cover.num(key)
        states = self.cover.section('states')
        for key in ('closed', 'moving', 'open', 'unknown'):
            states.int(key)
        for key in ('position', 'minimum', 'maximum', 'syncmode', 'target', 'track'):
            self.focuser.str(key)
        self.focuser.int('syncmode_bit')
        for key in ('track_value', 'steps_per_mm', 'tolerance', 'still_time', 'speed', 'timeout_margin'):
            self.focuser.num(key)
        for section in (self.focuser, self.derotator):
            if section.int('attempts') < 1:
                raise PilarConfigError(f"{section.path}.attempts: must be at least 1")
        if self.focuser.num('steps_per_mm') <= 0 or self.focuser.num('speed') <= 0:
            raise PilarConfigError("pilar_config.procedures.focuser: steps_per_mm and speed must be > 0")
        for key in ('position', 'target', 'minimum', 'maximum', 'syncmode', 'offset', 'track'):
            self.derotator.str(key)
        for key in ('tracking_syncmode', 'track_value', 'tolerance', 'tracking_tolerance', 'still_speed',
                    'still_time', 'timeout'):
            self.derotator.num(key)

    @staticmethod
    def _entries(section: _Section, procedures: Dict[str, Tuple[str, str]], mode: Optional[str]) \
            -> Dict[str, MappingEntry]:
        entries = {}
        for method, spec in section.data.items():
            path = f'{section.path}.{method}'
            if isinstance(spec, str) and spec.strip():
                entries[str(method).lower()] = MappingEntry(variable=spec.strip())
                continue
            if not isinstance(spec, dict) or not isinstance(spec.get('procedure'), str):
                raise PilarConfigError(f"{path}: must be a TPL2 variable or {{procedure: <name>, ...}}")
            name = spec['procedure']
            if name not in procedures:
                raise PilarConfigError(f"{path}: unknown procedure {name!r} (known: {', '.join(sorted(procedures))})")
            if mode is not None and procedures[name][1] != mode:
                raise PilarConfigError(f"{path}: procedure {name!r} is a {procedures[name][1]} procedure")
            options = {k: v for k, v in spec.items() if k != 'procedure'}
            if not all(isinstance(k, str) for k in options):
                raise PilarConfigError(f"{path}: option names must be text (YAML reads on/off/yes/no as true/false)")
            entries[str(method).lower()] = MappingEntry(procedure=name, options=options)
        return entries


# ----------------------------------------------------------------------------------------------- the link

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
        self.polled_at: Optional[float] = None
        self.first_poll = asyncio.Event()
        self._frozen_since: Optional[float] = None
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
        self._retrack_task: Optional[asyncio.Task] = None
        self._watch_task = self.loop.create_task(self._watch_loop(), name=f'pilar-watcher-{self.address}')

    # ------------------------------------------------------------------ state of the link

    def frozen(self) -> bool:
        return self._frozen_since is not None

    def in_guard(self) -> bool:
        return time.monotonic() < self._guard_until

    def unavailable(self) -> bool:
        return self.outage or self.pool.down

    def settled(self) -> bool:
        return not self.frozen() and not self.in_guard() and not self.unavailable()

    def _raise_unavailable(self) -> None:
        if self.outage:
            raise PilarUnavailable(f"Pilar at {self.address} has not answered for over "
                                   f"{self.settings.outage_after:.0f} s")
        raise PilarUnavailable(f"Pilar at {self.address} not reachable")

    def check_readable(self) -> None:
        if self.unavailable():
            self._raise_unavailable()
        if self.frozen():
            raise PilarTransient(f"Pilar at {self.address} frozen for "
                                 f"{time.monotonic() - self._frozen_since:.0f} s")

    async def wait_settled(self, timeout: Optional[float] = None) -> None:
        """Wait for the end of a freeze and of the guard after it; 4005 TEMPORARY after ``timeout``."""
        timeout = self.settings.command_freeze_wait if timeout is None else timeout
        deadline = time.monotonic() + timeout
        while not self.settled():
            if self.unavailable():
                self._raise_unavailable()
            if time.monotonic() >= deadline:
                raise PilarTransient(f"Pilar at {self.address} frozen, no answer in {timeout:.0f} s")
            await asyncio.sleep(0.05)

    # ------------------------------------------------------------------ commands

    async def command(self, body: str, *, purpose: str = 'general', timeout: Optional[float] = None,
                      through_freeze: bool = True) -> Tpl2Reply:
        """Send one command and return its reply. A command (``through_freeze``) waits for the end of a freeze
        before it is sent and keeps waiting for its reply while Pilar is frozen; a read fails at once."""
        if self.unavailable():
            self._raise_unavailable()
        if self.frozen():
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
                self._raise_unavailable()
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
        the watcher reads it too)."""
        key = var.upper()
        if watch:
            self._watched.setdefault(key, var)
        if not fresh:
            snap = self._snapshot.get(key)
            if snap is not None and time.monotonic() - snap[0] <= self.settings.snapshot_max_age:
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
        if special.kind == 'FAILED' and (self.frozen() or self.in_guard()):
            raise PilarTransient(f"{var}: FAILED just after a Pilar freeze{detail}")
        raise PilarDeviceError(f"Pilar: {var} {special}{detail}")

    def number(self, item: Tpl2Item, var: str) -> float:
        value = self.value(item, var)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise PilarReplyError(f"Pilar: {var} = {value!r}, a number was expected")
        return float(value)

    async def read_number(self, var: str, *, fresh: bool = False, watch: bool = True) -> float:
        return self.number(await self.read(var, fresh=fresh, watch=watch), var)

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
            except (Tpl2LinkError, Tpl2Busy, PilarUnavailable) as e:
                logger.debug(f"Pilar {self.address}: watcher: {e}")
                self.first_poll.set()
                await asyncio.sleep(s.poll_interval)
                continue
            except Exception as e:
                logger.warning(f"Pilar {self.address}: watcher: {type(e).__name__}: {e}")
            self.first_poll.set()
            await asyncio.sleep(max(0.0, started + s.poll_interval - time.monotonic()))

    async def _poll(self) -> None:
        s = self.settings
        variables = list(self._watched.values())
        async with self.pool.lease('watcher') as conn:
            request = conn.send('GET ' + ';'.join(variables))
            sent = time.monotonic()
            try:
                done, _ = await asyncio.wait({request.future}, timeout=s.freeze_after)
                if not done:
                    self._freeze_begins(sent)
                    while not request.future.done():
                        remaining = sent + s.outage_after - time.monotonic()
                        if remaining <= 0:
                            self._outage_begins(sent)
                            conn.close()
                            raise PilarUnavailable("outage")
                        await asyncio.wait({request.future}, timeout=min(remaining, 1.0))
                reply = request.future.result()
            except BaseException:
                conn.abandon(request)
                raise
        now = time.monotonic()
        if self._frozen_since is not None or self.outage:
            self._freeze_ends(now)
        self.polled_at = now
        self._store(reply, now, sent)

    def _freeze_begins(self, since: float) -> None:
        if self._frozen_since is None:
            self._frozen_since = since
            logger.debug(f"Pilar {self.address}: no answer for {self.settings.freeze_after:g} s, frozen")

    def _outage_begins(self, since: float) -> None:
        if not self.outage:
            self.outage = True
            logger.error(f"Pilar {self.address}: no answer for {self.settings.outage_after:g} s, outage; "
                         f"tracking will not be resumed")
            self.tracking_wanted = False

    def _freeze_ends(self, now: float) -> None:
        since = self._frozen_since if self._frozen_since is not None else now
        self._frozen_since = None
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
        for key in list(self._watched):
            item = reply.items.get(key)
            if item is not None and self._forgotten.get(key, -math.inf) < sent:
                self._snapshot[key] = (now, item)
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
        if self.frozen() or self.in_guard():
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
                self._raise_unavailable()
            if time.monotonic() >= deadline:
                raise PilarBusy(f"the mount did not stop in {timeout:.0f} s")
            await asyncio.sleep(self.settings.poll_interval / 2)

    # ------------------------------------------------------------------ background operations

    def operation_active(self, key: str) -> Optional[_Operation]:
        op = self.operations.get(key)
        return op if op is not None and not op.task.done() else None

    def start_operation(self, key: str, name: str, coro, target: Any = None) -> _Operation:
        task = self.loop.create_task(coro, name=f'pilar-{name}')
        op = _Operation(key, name, task, target)
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
        self.close()

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


# ----------------------------------------------------------------------------------------------- connector

#: procedure name -> (method of PilarConnector, 'get' or 'put')
_PROCEDURES: Dict[str, Tuple[str, str]] = {
    'connected': ('_get_connected', 'get'),
    'tracking': ('_get_tracking', 'get'),
    'slewing': ('_get_slewing', 'get'),
    'at_park': ('_get_at_park', 'get'),
    'cover_state': ('_get_cover_state', 'get'),
    'focuser_position': ('_get_focuser_position', 'get'),
    'focuser_moving': ('_get_focuser_moving', 'get'),
    'focuser_maxstep': ('_get_focuser_maxstep', 'get'),
    'rotator_target': ('_get_rotator_target', 'get'),
    'rotator_moving': ('_get_rotator_moving', 'get'),
    'connect': ('_put_connect', 'put'),
    'power': ('_put_power', 'put'),
    'slew_altaz': ('_put_slew_altaz', 'put'),
    'slew_equatorial': ('_put_slew_equatorial', 'put'),
    'abort_slew': ('_put_abort_slew', 'put'),
    'set_tracking': ('_put_tracking', 'put'),
    'cover': ('_put_cover', 'put'),
    'focuser_move': ('_put_focuser_move', 'put'),
    'rotator_move': ('_put_rotator_move', 'put'),
}


#: put procedures which do not wait for the end of a freeze before they start
_NO_FREEZE_WAIT = frozenset({'abort_slew', 'connect'})


class PilarConnector(Connector):
    """Connector of components with ``protocol: pilar`` (kinds ``telescope``, ``covercalibrator``, ``focuser``,
    ``rotator``). What a method does is given by the mapping in ``pilar_config.yml``."""

    #: Pilar address -> link, shared by every connector (mount, covers and focuser are separate components with
    #: separate connectors, but one Pilar).
    _links: Dict[str, PilarLink] = {}

    def __init__(self, settings: Optional[dict] = None, **kwargs):
        super().__init__(**kwargs)
        cfg = settings if settings is not None else load_protocol_config()
        try:
            self.settings = PilarSettings(cfg, _PROCEDURES)
        except PilarConfigError as e:
            raise RuntimeError(f"Pilar connector configuration: {e}") from e
        logger.info('PilarConnector created')

    # ------------------------------------------------------------------ Connector interface

    async def get(self, component: 'Component', variable: str, kind=None, **data):
        return await self._serve('GET', component, variable, data)

    async def put(self, component: 'Component', variable: str, kind=None, **data):
        return await self._serve('PUT', component, variable, data)

    async def call(self, component: 'Component', function: str, **data):
        return await self.put(component, function, **data)

    async def subscribe(self, variables: Iterable[Tuple[str, str]], callback: Callable):
        raise NotImplementedError("Pilar protocol does not support subscriptions")

    def _not_implemented(self, operation: str, component, variable: str) -> TreeStructureError:
        return TreeStructureError(code=3002,
                                  message=f"{operation} {variable!r} is not implemented on {component.kind} "
                                          f"({component.sys_id}) by the Pilar protocol",
                                  severity=TreeStructureError.SEVERITY_CRITICAL)

    async def _serve(self, operation: str, component, variable: str, data: dict):
        where = f"{operation} {component.kind}.{variable}"
        try:
            mapping = self.settings.components.get(component.kind)
            if mapping is None:
                raise self._not_implemented(operation, component, variable)
            name = variable.lower()
            mode = operation.lower()
            if operation == 'GET':
                if name in mapping.fixed:
                    return mapping.fixed[name]
                entry = mapping.get.get(name)
            elif name == 'action':
                action = str(self._param(data, 'Action'))
                entry = mapping.actions.get(action.lower())
                if entry is None:
                    raise self._not_implemented(operation, component, f'action {action}')
                mode = 'get' if entry.variable is not None else _PROCEDURES[entry.procedure][1]
                data = {}
            else:
                entry = mapping.put.get(name)
            if entry is None:
                raise self._not_implemented(operation, component, variable)
            return await self._run(entry, mode, component, data)
        except (BaseCodedError, asyncio.CancelledError):
            raise
        except (PilarBusy, Tpl2Busy) as e:
            raise TreeOtherError(code=4008, severity=TreeOtherError.SEVERITY_TEMPORARY,
                                 message=f"Pilar busy, {where} refused: {e}")
        except PilarDeviceError as e:
            logger.warning(f"Pilar device error on {where}: {e}")
            raise TreeOtherError(code=4009, severity=TreeOtherError.SEVERITY_NORMAL,
                                 message=f"Pilar error on {where}: {e}")
        except PilarUnsupported as e:
            raise TreeStructureError(code=3002, severity=TreeStructureError.SEVERITY_CRITICAL,
                                     message=f"{where}: {e}")
        except PilarConfigError as e:
            logger.error(f"Pilar configuration error on {where}: {e}")
            raise TreeStructureError(code=3001, message=str(e), severity=TreeStructureError.SEVERITY_CRITICAL)
        except (PilarTransient, Tpl2Timeout) as e:
            logger.debug(f"Pilar transient failure on {where}: {e}")
            raise TreeOtherError(code=4005, severity=TreeOtherError.SEVERITY_TEMPORARY,
                                 message=f"Pilar did not answer on {where}: {e}")
        except (PilarUnavailable, Tpl2LinkError, OSError) as e:
            raise TreeOtherError(code=4005, severity=TreeOtherError.SEVERITY_NORMAL,
                                 message=f"Pilar unreachable on {where}: {e}")
        except (Tpl2ProtocolError, Tpl2CommandError, PilarReplyError) as e:
            logger.error(f"Pilar reply not usable on {where}: {e}")
            raise TreeValueError(code=2002, message=f"Pilar reply not usable on {where}: {e}")
        except (TypeError, ValueError) as e:
            raise AddressError(code=1003, message=f"Wrong parameters for {where}: {e}")

    async def _run(self, entry: MappingEntry, mode: str, component, data: dict):
        if entry.variable is not None:
            link = self._link(component)
            var = self._variable(entry.variable, component)
            if mode == 'get':
                return link.value(await link.read(var), var)
            if len(data) != 1:
                raise AddressError(code=1003, message=f"SET {var} needs exactly one parameter, got {len(data)}")
            await link.set(var, next(iter(data.values())))
            return None
        method_name, kind = _PROCEDURES[entry.procedure]
        if kind == 'put' and entry.procedure not in _NO_FREEZE_WAIT:
            await self._link(component).wait_settled()     # a command asked during a freeze waits for its end
        return await getattr(self, method_name)(component, data, **entry.options)

    # ------------------------------------------------------------------ links

    def _link(self, component) -> PilarLink:
        address = component.get_option_recursive('address')
        if not address:
            raise PilarConfigError(f"{component.sys_id}: no 'address' (host:port of Pilar) in the configuration")
        host, sep, port = str(address).rpartition(':')
        if not sep or not host or not port.isdigit():
            raise PilarConfigError(f"{component.sys_id}: address {address!r} is not host:port")
        key = f'{host}:{int(port)}'
        link = self._links.get(key)
        if link is None or link.closed or link.loop is not asyncio.get_running_loop():
            if link is not None:
                link.close()
            link = PilarLink(host, int(port), self.settings)
            self._links[key] = link
        return link

    @classmethod
    def close_all(cls) -> None:
        """Close every link (server stop, tests)."""
        for link in cls._links.values():
            link.close()
        cls._links.clear()

    @classmethod
    async def aclose_all(cls) -> None:
        links = list(cls._links.values())
        cls._links.clear()
        for link in links:
            try:
                await link.aclose()
            except Exception as e:
                logger.debug(f"Closing Pilar link {link.address} failed: {e}")

    @staticmethod
    def _variable(template: str, component) -> str:
        return template.replace(_DEVICE_NUMBER, str(component.device_nr))

    # ------------------------------------------------------------------ parameters

    @staticmethod
    def _param(data: dict, name: str, required: bool = True):
        for key, value in (data or {}).items():
            if str(key).lower() == name.lower():
                return value
        if required:
            raise AddressError(code=1003, message=f"Missing parameter {name!r}")
        return None

    @staticmethod
    def _as_float(value, name: str) -> float:
        try:
            if isinstance(value, bool):
                raise TypeError
            result = float(value)
        except (TypeError, ValueError):
            raise AddressError(code=1003, message=f"Parameter {name!r} must be a number, got {value!r}")
        if not math.isfinite(result):
            raise AddressError(code=1003, message=f"Parameter {name!r} must be finite, got {value!r}")
        return result

    @staticmethod
    def _as_bool(value, name: str) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in ('true', 'false'):
            return value.strip().lower() == 'true'
        raise AddressError(code=1003, message=f"Parameter {name!r} must be true or false, got {value!r}")

    @staticmethod
    def _out_of_range(name: str, value: float, low: float, high: float, unit: str = '') -> AddressError:
        return AddressError(code=1003, message=f"Parameter {name!r} = {value:g}{unit} is outside "
                                               f"{low:g}..{high:g}{unit}")

    # ------------------------------------------------------------------ procedures: link and mount

    async def _get_connected(self, component, data) -> bool:
        link = self._link(component)
        s = self.settings
        try:
            await asyncio.wait_for(link.first_poll.wait(), timeout=s.connect_timeout + s.greeting_timeout)
        except asyncio.TimeoutError:
            pass
        return link.polled_at is not None and not link.unavailable()

    async def _put_connect(self, component, data) -> None:
        if not self._as_bool(self._param(data, 'Connected'), 'Connected'):
            return None
        if not await self._get_connected(component, data):
            raise PilarUnavailable(f"Pilar at {self._link(component).address} not reachable")
        return None

    async def _get_tracking(self, component, data) -> bool:
        link = self._link(component)
        s = self.settings
        return link.value(await link.read(s.state_track), s.state_track) == s.tracking_value

    async def _get_slewing(self, component, data) -> bool:
        link = self._link(component)
        await link.read(self.settings.state_azimuth)     # raises when frozen: the state is not known then
        return link.slewing()

    async def _get_at_park(self, component, data) -> bool:
        link = self._link(component)
        s = self.settings
        ready = await link.read_number(s.state_ready)
        if link.in_guard():
            raise PilarTransient("READY_STATE just after a Pilar freeze")
        op = link.operation_active('mount')
        if op is not None and op.name.startswith('power'):
            return False
        return ready == s.off_value

    def _check_mount_free(self, link: PilarLink) -> None:
        op = link.operation_active('mount')
        if op is not None and op.name.startswith('power'):
            raise PilarBusy(f"{op.name} in progress")

    async def _before_slew(self, link: PilarLink) -> None:
        self._check_mount_free(link)
        await link.require_ready()
        await link.cancel_operation('mount')   # a pending abortslew must not override the new target
        await link.restore_derotator()

    async def _put_slew_altaz(self, component, data, wait: bool = False) -> None:
        link = self._link(component)
        p = self.settings.slew_altaz
        az = self._as_float(self._param(data, 'Azimuth'), 'Azimuth')
        alt = self._as_float(self._param(data, 'Altitude'), 'Altitude')
        try:
            min_alt = 90.0 - await link.limit(p.str('zd_limit'))
        except (PilarUnsupported, PilarDeviceError, PilarReplyError) as e:
            logger.warning(f"Pilar: no zenith distance limit ({e}); altitude not checked")
            min_alt = -90.0
        if not min_alt <= alt <= 90.0:
            raise self._out_of_range('Altitude', alt, min_alt, 90.0, ' deg')
        az %= 360.0
        await self._before_slew(link)
        link.tracking_wanted = False
        await link.set(p.str('azimuth'), az)
        await link.set(p.str('altitude'), alt)
        await link.set(p.str('track'), p.num('track_value'))
        link.note_motion('altaz', (az, alt))
        if wait:
            await self._wait_slew(link)
        return None

    async def _put_slew_equatorial(self, component, data, wait: bool = False) -> None:
        link = self._link(component)
        ra = self._as_float(self._param(data, 'RightAscension'), 'RightAscension')
        dec = self._as_float(self._param(data, 'Declination'), 'Declination')
        if not 0.0 <= ra <= 24.0:
            raise self._out_of_range('RightAscension', ra, 0.0, 24.0, ' h')
        if not -90.0 <= dec <= 90.0:
            raise self._out_of_range('Declination', dec, -90.0, 90.0, ' deg')
        epoch = self._epoch(component)
        await self._before_slew(link)
        await self._set_equatorial_target(link, ra % 24.0, dec, epoch)
        p = self.settings.slew_equatorial
        await link.set(p.str('track'), p.num('track_value'))
        link.tracking_wanted = True
        link.note_motion('track')
        if wait:
            await self._wait_slew(link)
        return None

    def _epoch(self, component) -> float:
        epoch = component.get_option_recursive('epoch')
        try:
            return float(epoch)
        except (TypeError, ValueError):
            raise PilarConfigError(f"{component.sys_id}: no numeric 'epoch' in the observatory configuration "
                                   f"(got {epoch!r})") from None

    async def _set_equatorial_target(self, link: PilarLink, ra: float, dec: float, epoch: float) -> None:
        p = self.settings.slew_equatorial
        for var in p.value('epoch'):
            await link.set(var, epoch)
        for var, value in (p.data.get('set_before') or {}).items():
            await link.set(var, value)
        await link.set(p.str('ra'), ra)
        await link.set(p.str('dec'), dec)
        link.equatorial_target = True

    async def _wait_slew(self, link: PilarLink) -> None:
        deadline = time.monotonic() + self.settings.sync_slew_timeout
        while link.slewing() or link.frozen():
            if link.unavailable():
                link._raise_unavailable()
            if time.monotonic() > deadline:
                raise PilarDeviceError(f"slew not ended in {self.settings.sync_slew_timeout:.0f} s")
            await asyncio.sleep(self.settings.poll_interval / 2)

    async def _put_abort_slew(self, component, data) -> None:
        link = self._link(component)
        self._check_mount_free(link)
        if not link.slewing() and not link.frozen():
            return None                                   # nothing to stop (ALPACA: no-op)
        if link.operation_active('mount') is not None:
            return None                                   # an abortslew is already on its way
        s = self.settings
        p = s.abort_slew

        async def abort():
            await link.wait_settled(s.outage_after)       # the position is known only after a freeze
            items = await link.read_many([s.state_azimuth, s.state_altitude], purpose='priority')
            az = link.number(items[s.state_azimuth.upper()], s.state_azimuth)
            alt = link.number(items[s.state_altitude.upper()], s.state_altitude)
            await link.set(p.str('azimuth'), az % 360.0, purpose='priority')
            await link.set(p.str('altitude'), alt, purpose='priority')
            await link.set(p.str('track'), p.num('track_value'), purpose='priority')
            link.tracking_wanted = False
            link.note_motion('abort', (az % 360.0, alt))
            logger.info(f"Pilar {link.address}: slew aborted, new target = current position "
                        f"az {az:.3f} alt {alt:.3f}")
            while link.motion_active():
                await asyncio.sleep(s.poll_interval / 2)

        link.tracking_wanted = False
        link.start_operation('mount', 'abortslew', abort())
        return None

    async def _put_tracking(self, component, data) -> None:
        link = self._link(component)
        p = self.settings.tracking
        s = self.settings
        on = self._as_bool(self._param(data, 'Tracking'), 'Tracking')
        self._check_mount_free(link)
        if not on:
            link.tracking_wanted = False
            await link.set(p.str('track'), p.num('off_value'))
            await link.restore_derotator()
            return None
        await link.require_ready()
        track = await link.read(s.state_track, fresh=True)
        if track.ok and track.value == s.tracking_value:
            link.tracking_wanted = True
            return None
        if not link.equatorial_target:
            # after an az/alt slew Pilar's target is horizontal: track the current position
            items = await link.read_many([p.str('current_ra'), p.str('current_dec')])
            ra = link.number(items[p.str('current_ra').upper()], p.str('current_ra'))
            dec = link.number(items[p.str('current_dec').upper()], p.str('current_dec'))
            await self._set_equatorial_target(link, ra, dec, self._epoch(component))
        await link.set(p.str('track'), p.num('on_value'))
        link.tracking_wanted = True
        link.note_motion('track')
        return None

    async def _put_power(self, component, data, park: bool) -> None:
        link = self._link(component)
        s = self.settings
        on = not park
        if link.operation_active('mount') is not None or link.operation_active('cover') is not None:
            busy = link.operation_active('mount') or link.operation_active('cover')
            raise PilarBusy(f"{busy.name} in progress")
        await link.wait_settled()
        ready = await link.read_number(s.state_ready, fresh=True)
        if ready == (s.ready_value if on else s.off_value):
            return None
        name = 'power on (unpark)' if on else 'power off (park)'
        p = s.power

        async def power():
            if not on:
                link.tracking_wanted = False
                try:
                    await link.restore_derotator()
                except Exception as e:
                    logger.warning(f"Pilar {link.address}: derotator settings not restored before park: {e}")
            try:
                await link.set(p.str('ready'), 1 if on else 0, purpose='long',
                               timeout=p.num('timeout_on' if on else 'timeout_off'))
            finally:
                link.forget(s.state_ready, s.state_track, s.state_azimuth, s.state_altitude,
                            self.settings.cover.str('position'))
            link.motion = None

        link.start_operation('mount', name, power())
        return None

    # ------------------------------------------------------------------ procedures: covers

    async def _get_cover_state(self, component, data) -> int:
        link = self._link(component)
        p = self.settings.cover
        states = p.section('states')
        if link.operation_active('cover') is not None:
            return states.int('moving')
        item = await link.read(p.str('position'))
        if not item.ok and item.special.kind == 'FAILED':
            if link.in_guard() or link.frozen():
                raise PilarTransient("cover position FAILED just after a Pilar freeze")
            return states.int('unknown')
        position = link.number(item, p.str('position'))
        tolerance = p.num('tolerance')
        if abs(position - p.num('open_value')) <= tolerance:
            return states.int('open')
        if abs(position - p.num('closed_value')) <= tolerance:
            return states.int('closed')
        return states.int('moving')

    async def _put_cover(self, component, data, open: bool) -> None:
        link = self._link(component)
        p = self.settings.cover
        busy = link.operation_active('cover')
        mount = link.operation_active('mount')
        if busy is not None:
            raise PilarBusy(f"{busy.name} in progress")
        if mount is not None and mount.name.startswith('power'):
            raise PilarBusy(f"{mount.name} in progress")
        await link.require_ready()
        target = p.num('open_value' if open else 'closed_value')
        name = 'open cover' if open else 'close cover'

        async def move():
            try:
                await link.set(p.str('target'), target, purpose='long', timeout=p.num('timeout'))
            finally:
                link.forget(p.str('position'))

        link.start_operation('cover', name, move(), target)
        return None

    # ------------------------------------------------------------------ procedures: focuser

    async def _get_focuser_position(self, component, data) -> int:
        link = self._link(component)
        p = self.settings.focuser
        return int(round(await link.read_number(p.str('position')) * p.num('steps_per_mm')))

    async def _get_focuser_moving(self, component, data) -> bool:
        return self._link(component).operation_active('focuser') is not None

    async def _get_focuser_maxstep(self, component, data) -> int:
        link = self._link(component)
        p = self.settings.focuser
        return int(math.floor(await link.limit(p.str('maximum')) * p.num('steps_per_mm')))

    async def _put_focuser_move(self, component, data) -> None:
        link = self._link(component)
        p = self.settings.focuser
        steps = self._as_float(self._param(data, 'Position'), 'Position')
        target = round(steps) / p.num('steps_per_mm')
        low, high = await link.limit(p.str('minimum')), await link.limit(p.str('maximum'))
        if not low <= target <= high:
            k = p.num('steps_per_mm')
            raise self._out_of_range('Position', steps, math.ceil(low * k), math.floor(high * k), ' steps')
        await link.cancel_operation('focuser')        # a new target replaces the move in progress
        link.start_operation('focuser', f'focuser move to {target:.4f} mm', self._focuser_move(link, target),
                             target)
        return None

    async def _focuser_move(self, link: PilarLink, target: float) -> None:
        """Bit of SYNCMODE for the time of the move, POSITION, TRACK=4; the mount has priority: during its slew the
        move waits (Pilar switches the focuser motor off) and is sent again after it."""
        p = self.settings.focuser
        s = self.settings
        bit = p.int('syncmode_bit')
        try:
            attempts = p.int('attempts')
            for attempt in range(1, attempts + 1):
                await link.wait_mount_idle(s.queue_wait)
                await link.require_ready()
                if link.focus_syncmode_saved is None:
                    saved = int(await link.read_number(p.str('syncmode'), fresh=True))
                    if saved & bit:
                        logger.warning(f"Pilar {link.address}: focuser SYNCMODE {saved} already has bit {bit}; "
                                       f"it will be left as found")
                    link.focus_syncmode_saved = saved
                await link.set(p.str('syncmode'), link.focus_syncmode_saved | bit)
                await link.set(p.str('target'), target)
                await link.set(p.str('track'), p.num('track_value'))
                if await self._focuser_wait(link, target):
                    return
                logger.info(f"Pilar {link.address}: focuser move interrupted by the mount, sent again "
                            f"after the slew (attempt {attempt})")
            raise PilarDeviceError(f"focuser move to {target:.4f} mm interrupted by the mount {attempts} times")
        finally:
            saved = link.focus_syncmode_saved
            if saved is not None:
                try:
                    await asyncio.shield(link.set(p.str('syncmode'), saved))
                    link.focus_syncmode_saved = None
                except BaseException as e:
                    logger.warning(f"Pilar {link.address}: focuser SYNCMODE not restored to {saved}: {e}")

    async def _focuser_wait(self, link: PilarLink, target: float) -> bool:
        """True when the focuser is at ``target``; False when the mount started a slew meanwhile."""
        p = self.settings.focuser
        s = self.settings
        start = await link.read_number(p.str('position'), fresh=True)
        allowed = abs(target - start) / p.num('speed') + p.num('timeout_margin')
        deadline = time.monotonic() + allowed
        in_since = None
        while True:
            await asyncio.sleep(s.poll_interval)
            if link.mount_commanded_busy():
                return False
            try:
                position = await link.read_number(p.str('position'))
            except PilarTransient:
                deadline += s.poll_interval              # a freeze: the move goes on, the time is not counted
                in_since = None
                continue
            now = time.monotonic()
            if abs(position - target) <= p.num('tolerance'):
                in_since = in_since if in_since is not None else now
                if now - in_since >= p.num('still_time'):
                    return True
            else:
                in_since = None
            if now > deadline:
                raise PilarDeviceError(f"focuser not at {target:.4f} mm after {allowed:.0f} s "
                                       f"(at {position:.4f} mm)")

    # ------------------------------------------------------------------ procedures: rotator

    async def _get_rotator_target(self, component, data) -> float:
        link = self._link(component)
        target = link.rotator_targets.get(component.device_nr)
        if target is not None:
            return target
        var = self._variable(self.settings.derotator.str('position'), component)
        return await link.read_number(var)

    async def _get_rotator_moving(self, component, data) -> bool:
        return self._link(component).operation_active(f'rotator{component.device_nr}') is not None

    async def _put_rotator_move(self, component, data, relative: bool = False) -> None:
        link = self._link(component)
        d = self.settings.derotator
        position_var = self._variable(d.str('position'), component)
        value = self._as_float(self._param(data, 'Position'), 'Position')
        target = value + await link.read_number(position_var, fresh=True) if relative else value
        low = await link.limit(self._variable(d.str('minimum'), component))
        high = await link.limit(self._variable(d.str('maximum'), component))
        if not low <= target <= high:
            raise self._out_of_range('Position', target, low, high, ' deg')
        key = f'rotator{component.device_nr}'
        await link.cancel_operation(key)
        link.rotator_targets[component.device_nr] = target
        link.start_operation(key, f'rotator move to {target:.3f} deg', self._rotator_move(link, component, target),
                             target)
        return None

    async def _rotator_move(self, link: PilarLink, component, target: float) -> None:
        """Without tracking: TARGETPOS. In tracking (Pilar compensates the field rotation): SYNCMODE=3 + OFFSET +
        TRACK=1; SYNCMODE and OFFSET come back after tracking. The mount has priority, as for the focuser."""
        d = self.settings.derotator
        s = self.settings
        position_var = self._variable(d.str('position'), component)
        attempts = d.int('attempts')
        for attempt in range(1, attempts + 1):
            await link.wait_mount_idle(s.queue_wait)
            await link.require_ready()
            tracking = await link.read(s.state_track, fresh=True)
            if tracking.ok and tracking.value == s.tracking_value:
                rate = await self._rotator_rate(link, position_var)
                syncmode = await link.read_number(d.str('syncmode'), fresh=True, watch=False)
                offset = await link.read_number(d.str('offset'), fresh=True, watch=False)
                if link.derotator_saved is None:
                    link.derotator_saved = (int(syncmode), offset)
                read_at, current = time.monotonic(), await link.read_number(position_var, fresh=True)
                await link.set(d.str('syncmode'), d.num('tracking_syncmode'))
                await link.set(d.str('offset'), offset + (target - current))
                await link.set(d.str('track'), d.num('track_value'))
                if await self._rotator_settle(link, position_var, rate, target, read_at):
                    return
            else:
                await link.set(self._variable(d.str('target'), component), target, purpose='long',
                               timeout=d.num('timeout'))
                if await self._rotator_arrive(link, position_var, target):
                    return
            logger.info(f"Pilar {link.address}: rotator move interrupted by the mount, sent again after the "
                        f"slew (attempt {attempt})")
        raise PilarDeviceError(f"rotator move to {target:.3f} deg interrupted by the mount {attempts} times")

    async def _rotator_rate(self, link: PilarLink, var: str) -> float:
        """The field rotation [deg/s] seen on the derotator just before a move in tracking."""
        t0, p0 = time.monotonic(), await link.read_number(var, fresh=True)
        await asyncio.sleep(self.settings.derotator.num('still_time'))
        t1, p1 = time.monotonic(), await link.read_number(var, fresh=True)
        return (p1 - p0) / max(t1 - t0, 1e-3)

    async def _rotator_settle(self, link: PilarLink, var: str, rate: float, target: float, since: float) -> bool:
        """In tracking: wait until the derotator is at ``target`` carried on by the field rotation (TRACK=1 starts
        3-4 s after the command) and moves only with the field rotation again."""
        d = self.settings.derotator
        s = self.settings
        deadline = time.monotonic() + d.num('timeout')
        last = None
        still_since = None
        while True:
            await asyncio.sleep(s.poll_interval)
            if link.mount_commanded_busy():
                return False
            try:
                now, position = time.monotonic(), await link.read_number(var, fresh=True)
            except PilarTransient:
                deadline += s.poll_interval
                still_since, last = None, None
                continue
            expected = target + rate * (now - since)
            if last is not None and abs(position - expected) <= d.num('tracking_tolerance'):
                speed = (position - last[1]) / max(now - last[0], 1e-3)
                if abs(speed - rate) < d.num('still_speed'):
                    still_since = still_since if still_since is not None else now
                    if now - still_since >= d.num('still_time'):
                        return True
                else:
                    still_since = None
            else:
                still_since = None
            last = (now, position)
            if now > deadline:
                raise PilarDeviceError(f"rotator not at {expected:.3f} deg (target {target:.3f} + field rotation) "
                                       f"in {d.num('timeout'):.0f} s (at {position:.3f} deg)")

    async def _rotator_arrive(self, link: PilarLink, var: str, target: float) -> bool:
        """Without tracking: wait until the derotator is at ``target`` and stands."""
        d = self.settings.derotator
        s = self.settings
        deadline = time.monotonic() + d.num('timeout')
        in_since = None
        while True:
            if link.mount_commanded_busy():
                return False
            try:
                position = await link.read_number(var, fresh=True)
            except PilarTransient:
                deadline += s.poll_interval
                in_since = None
                await asyncio.sleep(s.poll_interval)
                continue
            now = time.monotonic()
            if abs(position - target) <= d.num('tolerance'):
                in_since = in_since if in_since is not None else now
                if now - in_since >= d.num('still_time'):
                    return True
            else:
                in_since = None
            if now > deadline:
                raise PilarDeviceError(f"rotator not at {target:.3f} deg after {d.num('timeout'):.0f} s "
                                       f"(at {position:.3f} deg)")
            await asyncio.sleep(s.poll_interval)
