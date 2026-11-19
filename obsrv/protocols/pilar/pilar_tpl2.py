"""TPL2 client (OpenTSI telescope protocol) for the Pilar connector.

How Pilar 3.3-3r21 (IRIS) talks TPL2, verified against the instrument (18-19.11.2026):

* after the TCP connect the server sends ``TPL2 2.1 CONN <n> AUTH ENC MESSAGE ...`` and ``AUTH OK 0 0``: no
  authentication and no encryption are needed; the 11th client gets ``TOO MANY CLIENTS CONNECTED`` instead;
* a request is ``<id> GET A;B;C`` or ``<id> SET X=v``; a line without an id is a syntax error;
* a reply is ``<id> COMMAND OK``, one ``DATA INLINE X=v`` (GET) / ``DATA OK X`` / ``DATA ERROR X <reason>`` line per
  variable, then ``<id> COMMAND COMPLETE``; a rejected command gives ``COMMAND ERROR <reason>`` + ``COMMAND FAILED``;
* a SET of a variable that moves something (covers, derotator, ``TELESCOPE.READY``) answers ``DATA OK`` only when
  the move ends;
* a value can be special: ``FAILED`` (often with ``EVENT WARN X:0 <text>``), ``UNKNOWN`` (no such variable),
  ``INVALID`` (not a leaf), ``DIMENSION`` (index out of range), ``NULL``, ``LOCKEDBY <id>``;
* events (``EVENT INFO|WARN|ERROR X:<n> <text>``) come with the id of a command, with 0 or with an id of nobody;
* strings are quoted, a backslash escapes the next character, the encoding is latin-1;
* one connection answers its commands in order, so the pool gives each command a connection of its own.

The module knows nothing of Pilar's variables: it moves lines, the Pilar connector gives them meaning.
"""
import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__.rsplit('.')[-1])


# ----------------------------------------------------------------------------------------------- exceptions

class Tpl2Error(Exception):
    """Base of the TPL2 client errors."""


class Tpl2LinkError(ConnectionError):
    """The server cannot be reached or the connection broke (refused, closed, too many clients)."""


class Tpl2Timeout(TimeoutError):
    """No complete reply in time."""


class Tpl2Busy(Tpl2Error):
    """No free connection in the pool in time."""


class Tpl2ProtocolError(Tpl2Error):
    """A greeting or a reply that does not follow TPL2."""


class Tpl2CommandError(Tpl2Error):
    """The server rejected the whole command (``COMMAND ERROR <reason>``)."""

    def __init__(self, body: str, reason: str):
        self.body = body
        self.reason = reason
        super().__init__(f"command {body!r} rejected: {reason}")


# ----------------------------------------------------------------------------------------------- values

#: Special values of a variable (``DATA INLINE X=<special>`` or the reason of ``DATA ERROR X <reason>``).
SPECIAL_VALUES = frozenset({'FAILED', 'UNKNOWN', 'INVALID', 'NULL', 'DIMENSION', 'DENIED', 'LOCKEDBY'})

_INT_RE = re.compile(r'^[+-]?\d+$')


@dataclass(frozen=True)
class Tpl2Special:
    """A special value instead of data, e.g. ``FAILED`` or ``LOCKEDBY 3199750636807``."""
    kind: str
    detail: str = ''

    def __str__(self):
        return f'{self.kind} {self.detail}'.strip()


def unescape(text: str) -> str:
    """Contents of a quoted TPL2 string: a backslash escapes the next character."""
    out = []
    escaped = False
    for ch in text:
        if escaped:
            out.append(ch)
            escaped = False
        elif ch == '\\':
            escaped = True
        else:
            out.append(ch)
    if escaped:
        out.append('\\')
    return ''.join(out)


def parse_value(text: str) -> Any:
    """A TPL2 value: quoted string, int, float, :class:`Tpl2Special` or (anything else) the raw text."""
    t = text.strip()
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        return unescape(t[1:-1])
    word, _, rest = t.partition(' ')
    if word in SPECIAL_VALUES:
        return Tpl2Special(word, rest.strip())
    if _INT_RE.match(t):
        return int(t)
    try:
        return float(t)
    except ValueError:
        return t


def format_value(value: Any) -> str:
    """A value for ``SET X=<value>``: numbers as numbers, text quoted."""
    if isinstance(value, bool):
        return '1' if value else '0'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format(value, '.12g')
    text = str(value).replace('\\', '\\\\').replace('"', '\\"')
    return f'"{text}"'


@dataclass
class Tpl2Item:
    """One variable of a reply: its value or the special value / error reason that came instead."""
    name: str
    value: Any = None
    special: Optional[Tpl2Special] = None
    message: str = ''          # texts of the events about this variable that came with the reply

    @property
    def ok(self) -> bool:
        return self.special is None


@dataclass
class Tpl2Reply:
    """The complete reply to one command."""
    cmd_id: int
    body: str
    items: Dict[str, Tpl2Item] = field(default_factory=dict)  # upper-case name -> item
    events: List[str] = field(default_factory=list)             # 'WARN X:0 text' lines with this command's id
    accepted: bool = False                                      # COMMAND OK seen
    command_error: Optional[str] = None                         # reason of COMMAND ERROR
    failed: bool = False                                        # ended with COMMAND FAILED

    def item(self, name: str) -> Tpl2Item:
        try:
            return self.items[name.upper()]
        except KeyError:
            raise Tpl2ProtocolError(f"reply to {self.body!r} has no line for {name}") from None

    def messages(self, name: Optional[str] = None) -> List[str]:
        """Texts of the events of this command (of one variable when ``name`` is given)."""
        out = []
        for event in self.events:
            _level, _, rest = event.partition(' ')
            var, _, text = rest.partition(' ')
            if name is None or var.split(':')[0].upper() == name.upper():
                out.append(text.strip() or event)
        return out


# ----------------------------------------------------------------------------------------------- connection

_LINE_RE = re.compile(r'^(\d+)\s+(.*)$')
_COMMAND_RE = re.compile(r'^COMMAND\s+(OK|COMPLETE|FAILED)\s*$')
_COMMAND_ERROR_RE = re.compile(r'^COMMAND\s+ERROR\s+(\S+)(?:\s+(.*))?$')
_DATA_INLINE_RE = re.compile(r'^DATA\s+INLINE\s+([^=\s]+)=(.*)$')
_DATA_OK_RE = re.compile(r'^DATA\s+OK\s+(\S+)')
_DATA_ERROR_RE = re.compile(r'^DATA\s+ERROR\s+(\S+)\s+(\S+)(?:\s+(.*))?$')
_EVENT_RE = re.compile(r'^EVENT\s+(\S+)\s+(.*)$')


@dataclass
class Tpl2Request:
    """A command sent on a connection."""
    cmd_id: int
    body: str
    future: 'asyncio.Future[Tpl2Reply]'


class _Pending:
    __slots__ = ('reply', 'future', 'abandoned', 'since')

    def __init__(self, reply: Tpl2Reply, future: asyncio.Future):
        self.reply = reply
        self.future = future
        self.abandoned = False
        self.since = time.monotonic()


class Tpl2Connection:
    """One TCP connection to a TPL2 server. A reader task matches reply lines to commands by id, so a late reply
    (e.g. after a Pilar freeze) never mixes with the next command."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, name: str,
                 encoding: str, id_range, greeting: str = '',
                 on_event: Optional[Callable[[int, str], None]] = None,
                 on_settled: Optional[Callable[['Tpl2Connection'], None]] = None):
        self.name = name
        self.greeting = greeting
        self._reader = reader
        self._writer = writer
        self._encoding = encoding
        self._id_first, self._id_last = int(id_range[0]), int(id_range[1])
        self._next_id = self._id_first
        self._pending: Dict[int, _Pending] = {}
        self._on_event = on_event
        self._on_settled = on_settled
        self.closed = False
        self.last_line = time.monotonic()
        self._reader_task = asyncio.get_running_loop().create_task(self._read_loop(), name=f'tpl2-reader-{name}')

    @classmethod
    async def open(cls, host: str, port: int, *, name: str, encoding: str, id_range, connect_timeout: float,
                   greeting_timeout: float, on_event=None, on_settled=None) -> 'Tpl2Connection':
        """Connect and read the greeting. Raises :class:`Tpl2LinkError` when the server cannot be reached or
        refuses the client, :class:`Tpl2ProtocolError` when the greeting is not TPL2."""
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=connect_timeout)
        except (OSError, asyncio.TimeoutError) as e:
            raise Tpl2LinkError(f"cannot connect to {host}:{port}: {e or type(e).__name__}") from e
        try:
            greeting = await cls._read_greeting(reader, encoding, greeting_timeout)
        except BaseException:
            writer.close()
            raise
        return cls(reader, writer, name=name, encoding=encoding, id_range=id_range, greeting=greeting,
                   on_event=on_event, on_settled=on_settled)

    @staticmethod
    async def _read_greeting(reader: asyncio.StreamReader, encoding: str, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        greeting = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Tpl2LinkError("no TPL2 greeting in time")
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=remaining)
            except asyncio.TimeoutError:
                raise Tpl2LinkError("no TPL2 greeting in time") from None
            except OSError as e:
                raise Tpl2LinkError(f"connection lost during the greeting: {e}") from e
            line = raw.decode(encoding, errors='replace').strip()
            if not raw:
                raise Tpl2LinkError(f"server closed the connection during the greeting"
                                    f"{': ' + greeting if greeting else ''}")
            if 'TOO MANY CLIENTS' in line.upper():
                raise Tpl2LinkError(f"server refused the client: {line}")
            if greeting is None:
                if not line.startswith('TPL2'):
                    raise Tpl2ProtocolError(f"not a TPL2 greeting: {line!r}")
                greeting = line
                continue
            if line.startswith('AUTH OK'):
                return greeting
            if line.startswith('AUTH'):
                raise Tpl2ProtocolError(f"server requires authentication, not supported: {line!r}")
            # anything else before AUTH OK (e.g. an early event) is ignored

    # ------------------------------------------------------------------ commands

    @property
    def idle(self) -> bool:
        """Open and with no command in progress (also no abandoned one whose reply is still coming)."""
        return not self.closed and not self._pending

    @property
    def stalled_since(self) -> Optional[float]:
        """Since when an abandoned command still waits for its reply (None when nothing is abandoned)."""
        times = [p.since for p in self._pending.values() if p.abandoned]
        return min(times) if times else None

    def next_id(self) -> int:
        for _ in range(self._id_last - self._id_first + 1):
            cmd_id = self._next_id
            self._next_id = cmd_id + 1 if cmd_id < self._id_last else self._id_first
            if cmd_id not in self._pending:
                return cmd_id
        raise Tpl2Busy(f"{self.name}: no free command id")

    def send(self, body: str) -> 'Tpl2Request':
        """Send ``<id> <body>``; the reply comes in ``request.future``. The caller awaits it (with its own
        timeout) and calls :meth:`abandon` when it stops waiting."""
        if self.closed:
            raise Tpl2LinkError(f"{self.name}: connection closed")
        cmd_id = self.next_id()
        future = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = _Pending(Tpl2Reply(cmd_id, body), future)
        try:
            self._writer.write(f'{cmd_id} {body}\n'.encode(self._encoding, errors='replace'))
        except Exception as e:
            self._pending.pop(cmd_id, None)
            self._fail(Tpl2LinkError(f"{self.name}: cannot send: {e}"))
            raise Tpl2LinkError(f"{self.name}: cannot send: {e}") from e
        return Tpl2Request(cmd_id, body, future)

    def abandon(self, request: 'Tpl2Request') -> None:
        """The caller stopped waiting: the late reply will be dropped; until it comes the connection is not
        idle."""
        pending = self._pending.get(request.cmd_id)
        if pending is not None and pending.future is request.future:
            pending.abandoned = True
        if not request.future.done():
            request.future.cancel()

    def close(self) -> None:
        if not self.closed:
            self._fail(Tpl2LinkError(f"{self.name}: connection closed"))
        try:
            self._writer.close()
        except Exception:
            pass
        if not self._reader_task.done():
            try:
                self._reader_task.cancel()
            except RuntimeError:       # the loop of the task is closed (tests, server restart)
                pass

    def _fail(self, error: Exception) -> None:
        self.closed = True
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(error)
                pending.future.exception()  # mark retrieved: nobody may be waiting any more
        self._pending.clear()
        if self._on_settled is not None:
            self._on_settled(self)

    # ------------------------------------------------------------------ reading

    async def _read_loop(self) -> None:
        try:
            while True:
                raw = await self._reader.readline()
                if not raw:
                    self._fail(Tpl2LinkError(f"{self.name}: server closed the connection"))
                    return
                self.last_line = time.monotonic()
                line = raw.decode(self._encoding, errors='replace').strip()
                if line:
                    self._dispatch(line)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._fail(Tpl2LinkError(f"{self.name}: connection lost: {e}"))

    def _dispatch(self, line: str) -> None:
        m = _LINE_RE.match(line)
        if m is None:
            logger.debug(f"{self.name}: line without a command id ignored: {line!r}")
            return
        cmd_id, rest = int(m.group(1)), m.group(2)
        event = _EVENT_RE.match(rest)
        pending = self._pending.get(cmd_id)
        if event is not None:
            text = f'{event.group(1)} {event.group(2)}'
            if pending is not None:
                pending.reply.events.append(text)
            if self._on_event is not None:
                try:
                    self._on_event(cmd_id, text)
                except Exception as e:
                    logger.debug(f"{self.name}: event handler failed: {e}")
            return
        if pending is None:
            logger.debug(f"{self.name}: line of no waiting command ignored: {line!r}")
            return
        reply = pending.reply
        if (c := _COMMAND_RE.match(rest)) is not None:
            word = c.group(1)
            if word == 'OK':
                reply.accepted = True
                return
            reply.failed = word == 'FAILED'
            self._finish(cmd_id, pending)
            return
        if (c := _COMMAND_ERROR_RE.match(rest)) is not None:
            reply.command_error = c.group(1) + (f' {c.group(2)}' if c.group(2) else '')
            return
        if (c := _DATA_INLINE_RE.match(rest)) is not None:
            name, value = c.group(1), parse_value(c.group(2))
            if isinstance(value, Tpl2Special):
                reply.items[name.upper()] = Tpl2Item(name, None, value)
            else:
                reply.items[name.upper()] = Tpl2Item(name, value)
            return
        if (c := _DATA_ERROR_RE.match(rest)) is not None:
            reason = c.group(2).upper()
            detail = c.group(3) or ''
            reply.items[c.group(1).upper()] = Tpl2Item(c.group(1), None, Tpl2Special(reason, detail.strip()))
            return
        if (c := _DATA_OK_RE.match(rest)) is not None:
            reply.items[c.group(1).upper()] = Tpl2Item(c.group(1), None)
            return
        logger.debug(f"{self.name}: line not understood: {line!r}")

    def _finish(self, cmd_id: int, pending: _Pending) -> None:
        del self._pending[cmd_id]
        reply = pending.reply
        if reply.events:
            for item in reply.items.values():
                if item.special is not None:
                    item.message = '; '.join(reply.messages(item.name))
        if not pending.future.done():
            pending.future.set_result(pending.reply)
        if pending.abandoned and not self._pending and self._on_settled is not None:
            self._on_settled(self)


# ----------------------------------------------------------------------------------------------- pool

class _Slot:
    __slots__ = ('kind', 'index', 'conn', 'leased')

    def __init__(self, kind: str, index: int):
        self.kind = kind          # 'general', 'reserved' or 'long'
        self.index = index
        self.conn: Optional[Tpl2Connection] = None
        self.leased = False

    @property
    def name(self) -> str:
        return f'{self.kind}{self.index}'


class Tpl2Pool:
    """Connections to one TPL2 server, opened when needed:

    * ``size`` permanent connections: one only for the watcher (``purpose='watcher'``), ``reserved`` only for
      priority commands (stopping the mount, ``purpose='priority'``), the rest for everything else;
    * ``long_max`` extra connections for commands which hold a connection until a move ends; they are closed
      after use, so they occupy a seat on the server only while needed;
    * when a connect fails, no connect is tried for ``cooldown`` seconds (breaker) and every lease fails at once
      with :class:`Tpl2LinkError`; one ERROR log per outage, one WARNING when the server is back;
    * a connection whose abandoned command waits for its reply longer than ``stalled_limit`` is closed.
    """

    def __init__(self, host: str, port: int, *, size: int, reserved: int, long_max: int, id_range,
                 encoding: str, connect_timeout: float, greeting_timeout: float, wait_timeout: float,
                 cooldown: float, stalled_limit: float, on_event: Optional[Callable[[int, str], None]] = None):
        if reserved < 0 or size < reserved + 2 or long_max < 0:
            raise ValueError("pool: need size >= reserved + 2 (watcher + at least one general), long_max >= 0")
        self.host, self.port = host, port
        self.address = f'{host}:{port}'
        self._slots = ([_Slot('watcher', 0)]
                       + [_Slot('general', i) for i in range(size - reserved - 1)]
                       + [_Slot('reserved', i) for i in range(reserved)]
                       + [_Slot('long', i) for i in range(long_max)])
        self._id_range = id_range
        self._encoding = encoding
        self._connect_timeout = connect_timeout
        self._greeting_timeout = greeting_timeout
        self._wait_timeout = wait_timeout
        self._cooldown = cooldown
        self._stalled_limit = stalled_limit
        self._on_event = on_event
        self._cond = asyncio.Condition()
        self._down_until = 0.0
        self._outage = False
        self.closed = False

    # ------------------------------------------------------------------ state

    @property
    def down(self) -> bool:
        """The breaker is open: the last connect failed less than ``cooldown`` ago."""
        return time.monotonic() < self._down_until

    @property
    def outage(self) -> bool:
        """The last connect attempt failed (and none succeeded since)."""
        return self._outage

    def open_connections(self) -> int:
        return sum(1 for s in self._slots if s.conn is not None and not s.conn.closed)

    # ------------------------------------------------------------------ leasing

    _PURPOSES = {'general': ('general',), 'priority': ('reserved', 'general'), 'long': ('long',),
                 'watcher': ('watcher',)}

    @contextlib.asynccontextmanager
    async def lease(self, purpose: str = 'general'):
        """A connection for one command: ``async with pool.lease() as conn: request = conn.send(...)``.
        ``purpose``: 'general', 'priority' (reserved connections first), 'long' (extra connection, closed after
        use) or 'watcher'."""
        slot = await self._acquire(self._PURPOSES[purpose])
        try:
            yield slot.conn
        finally:
            await self._release(slot)

    def _prune(self) -> None:
        now = time.monotonic()
        for slot in self._slots:
            conn = slot.conn
            if conn is None:
                continue
            if conn.closed:
                slot.conn = None
                continue
            stalled = conn.stalled_since
            if not slot.leased and stalled is not None and now - stalled > self._stalled_limit:
                logger.warning(f"Pilar {self.address}: connection {slot.name} waited {now - stalled:.0f} s for a "
                               f"late reply; closed")
                conn.close()
                slot.conn = None

    async def _acquire(self, kinds) -> _Slot:
        if self.closed:
            raise Tpl2LinkError(f"{self.address}: pool closed")
        deadline = time.monotonic() + self._wait_timeout
        async with self._cond:
            while True:
                self._prune()
                free = [s for s in self._slots if s.kind in kinds and not s.leased]
                ready = [s for s in free if s.conn is not None and s.conn.idle]
                empty = [s for s in free if s.conn is None]
                choice = None
                for kind in kinds:   # preference order: reserved before general for priority commands
                    choice = next((s for s in ready if s.kind == kind), None) \
                        or next((s for s in empty if s.kind == kind), None)
                    if choice is not None:
                        break
                if choice is not None:
                    choice.leased = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Tpl2Busy(f"Pilar {self.address}: no free connection in {self._wait_timeout:.0f} s")
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=min(remaining, 1.0))
                except asyncio.TimeoutError:
                    pass
        if choice.conn is None:
            try:
                choice.conn = await self._connect(choice.name)
            except BaseException:
                choice.leased = False
                await self._notify()
                raise
        return choice

    async def _release(self, slot: _Slot) -> None:
        slot.leased = False
        if slot.kind == 'long' and slot.conn is not None and slot.conn.idle:
            slot.conn.close()
            slot.conn = None
        await self._notify()

    async def _notify(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    def _settled(self, _conn: Tpl2Connection) -> None:
        # a late reply arrived or a connection closed: a slot may be free
        if not self.closed:
            asyncio.get_running_loop().create_task(self._notify())

    async def _connect(self, name: str) -> Tpl2Connection:
        if self.down:
            raise Tpl2LinkError(f"Pilar {self.address} not reachable (next attempt in "
                                f"{self._down_until - time.monotonic():.0f} s)")
        try:
            conn = await Tpl2Connection.open(
                self.host, self.port, name=f'{self.address}/{name}', encoding=self._encoding,
                id_range=self._id_range, connect_timeout=self._connect_timeout,
                greeting_timeout=self._greeting_timeout, on_event=self._on_event, on_settled=self._settled)
        except (Tpl2LinkError, Tpl2ProtocolError) as e:
            self._down_until = time.monotonic() + self._cooldown
            if not self._outage:
                logger.error(f"Pilar {self.address} not reachable: {e}; next attempt in {self._cooldown:g} s")
            self._outage = True
            raise Tpl2LinkError(str(e)) from e
        if self._outage:
            logger.warning(f"Pilar {self.address} reachable again")
        elif name.startswith('long'):
            logger.debug(f"Pilar {self.address}: connection {name} open ({conn.greeting})")   # one per long command
        else:
            logger.info(f"Pilar {self.address}: connection {name} open ({conn.greeting})")
        self._outage = False
        return conn

    def close(self) -> None:
        self.closed = True
        for slot in self._slots:
            if slot.conn is not None:
                slot.conn.close()
                slot.conn = None
