"""IRIS-CCD connector: the IRIS camera and its filter wheel, both served by one ``iriscom`` process over UDP.

How iriscom talks (iriscom.c, version 3.5 AF4.43, verified against the instrument):

* a request is one UDP datagram of exactly ``packet_size`` bytes: the command text padded with NUL bytes;
  a datagram of any other length is dropped without an answer;
* the reply is one datagram of the same size: ``"<UTC time> <echo of the command>\\n<status> <message>"``,
  where the status is ``OKAY`` or ``E<code>`` (100 syntax, 201 file, 202 xfer, 300 hardware, 301 filter);
* iriscom serves one command at a time. While ``go`` (exposure) or ``filter`` (wheel move) runs, nothing else
  is answered; such a command waits in the socket until the running one ends;
* there is no status command: the state of the camera and the position of the filter wheel are known to TIC
  only from the commands TIC itself sent.

The connector forwards a client's command to iriscom, waits for the reply and returns it (the same model as the
ALPACA connector). One :class:`IrisSession` per iriscom address is shared by every component configured with
that address (camera and filter wheel), so the two never talk to iriscom at the same time:

* a short command (``sync``, ``et``, ``clear``, ``xfer``) waits for a running short command;
* while a long operation (exposure, filter move) runs, every request that needs iriscom gets ``4008`` (busy)
  at once; values that TIC keeps itself (``camerastate``, ``position`` ...) are answered at once;
* a long operation always runs to the end, also when the client stops waiting: iriscom finishes it anyway,
  and TIC keeps its result (``lastimage``, ``position``).

Protocol settings are in ``iris_ccd_config.yml``; settings of a device (address, filters, detector, the NATS
subject of the detector temperature) in the observatory configuration of its component.
"""
import asyncio
import logging
import math
import os
import posixpath
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional, Sequence, Tuple

import confuse
from obcom.data_colection.address import AddressError
from obcom.data_colection.coded_error import BaseCodedError, TreeOtherError, TreeStructureError
from obcom.data_colection.value import TreeValueError
from serverish.base.datetime import dt_ensure_datetime
from serverish.messenger import get_callbacksubscriber

from obsrv.protocols.alpaca.alpaca_connector import Connector

logger = logging.getLogger(__name__.rsplit('.')[-1])

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'iris_ccd_config.yml')

#: Failures of the link to iriscom (no answer, ICMP port unreachable, socket errors) -> 4005.
#: ``TimeoutError`` (and ``IrisTimeout``) is an ``OSError`` subclass.
_TRANSPORT_ERRORS = (OSError, asyncio.TimeoutError)

_CAMERA = 'camera'
_FILTERWHEEL = 'filterwheel'


# ----------------------------------------------------------------------------------------------- exceptions

class IrisTimeout(TimeoutError):
    """iriscom did not answer in time."""


class IrisProtocolError(Exception):
    """A reply of iriscom which cannot be understood."""


class IrisDeviceError(Exception):
    """iriscom answered ``E<code>``, or answered OKAY with something else than the command must produce."""

    def __init__(self, code: Optional[int], message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class IrisBusy(Exception):
    """iriscom is busy with a long operation started by TIC."""

    def __init__(self, operation: str):
        super().__init__(operation)
        self.operation = operation


class IrisConfigError(Exception):
    """The configuration of the protocol or of a component is missing or wrong."""


# ----------------------------------------------------------------------------------------------- configuration

def _path(cfg: dict, dotted: str):
    node: Any = cfg
    for key in dotted.split('.'):
        if not isinstance(node, dict) or key not in node:
            raise IrisConfigError(f"{CONFIG_PATH}: missing key {dotted!r}")
        node = node[key]
    return node


def load_protocol_config(path: str = CONFIG_PATH) -> dict:
    """The protocol settings (``iris_ccd_config.yml``) as a plain dict."""
    logger.info(f"Loading IRIS CCD protocol configuration from: {path}")
    try:
        config = confuse.Configuration('IrisCcdConnector', __name__, read=False)
        config.set_file(path)
        return config.get()
    except (confuse.ConfigError, OSError) as e:
        raise RuntimeError(f"IRIS CCD protocol configuration {path} is missing or corrupted: {e}") from e


class ProtocolSettings:
    """Validated settings of ``iris_ccd_config.yml``. Every value the connector needs comes from there."""

    def __init__(self, cfg: dict):
        try:
            self.packet_size = int(_path(cfg, 'transport.packet_size'))
            self.encoding = str(_path(cfg, 'transport.encoding'))
            self.timeouts = {k: float(_path(cfg, f'timeouts.{k}')) for k in
                             ('command', 'filter_move', 'filter_rehome', 'exposure_overhead', 'exposure_margin')}
            self.commands = {k: str(_path(cfg, f'commands.{k}')) for k in
                             ('sync', 'exposure_time', 'expose', 'clear', 'xfer', 'filter', 'filter_rehome')}
            self.forbidden = {str(c).lower() for c in _path(cfg, 'commands.forbidden')}
            self.reply_ok = str(_path(cfg, 'replies.ok'))
            self.error_re = re.compile(_path(cfg, 'replies.error_pattern'), re.DOTALL)
            self.exposure_time_re = re.compile(_path(cfg, 'replies.exposure_time_pattern'))
            self.exposure_time_tolerance = float(_path(cfg, 'replies.exposure_time_tolerance'))
            self.exposure_files_re = re.compile(_path(cfg, 'replies.exposure_files_pattern'))
            self.frame_number_re = re.compile(_path(cfg, 'replies.frame_number_pattern'))
            self.xfer_re = re.compile(_path(cfg, 'replies.xfer_pattern'))
            self.xfer_disabled_host = str(_path(cfg, 'replies.xfer_disabled_host'))
            self.images_dir = str(_path(cfg, 'images.dir'))
            self.images_suffix = str(_path(cfg, 'images.suffix'))
            self.frames = {k: str(_path(cfg, f'images.frames.{k}')) for k in ('reset', 'middle', 'data')}
            self.camera_states = {k: int(_path(cfg, f'camera.states.{k}')) for k in
                                  ('idle', 'exposing', 'reading', 'error')}
            self.camera_fixed = dict(_path(cfg, 'camera.fixed'))
            self.first_slot = int(_path(cfg, 'filterwheel.first_slot'))
            self.last_slot = int(_path(cfg, 'filterwheel.last_slot'))
            self.unknown_position = int(_path(cfg, 'filterwheel.unknown_position'))
            self.skip_same_position = bool(_path(cfg, 'filterwheel.skip_same_position'))
            self.rehome_enabled = bool(_path(cfg, 'filterwheel.rehome_enabled'))
            self.deliver_policy = str(_path(cfg, 'nats.deliver_policy'))
            self.first_value_wait = float(_path(cfg, 'nats.first_value_wait'))
            self.resubscribe_interval = float(_path(cfg, 'nats.resubscribe_interval'))
        except (TypeError, ValueError, re.error) as e:
            raise IrisConfigError(f"{CONFIG_PATH}: wrong value: {e}") from e
        for name in ('binx', 'biny', 'startx', 'starty'):
            if name not in self.camera_fixed:
                raise IrisConfigError(f"{CONFIG_PATH}: missing key 'camera.fixed.{name}'")

    def encode(self, command: str) -> bytes:
        data = command.encode(self.encoding)
        if len(data) >= self.packet_size:
            raise ValueError(f"command longer than {self.packet_size - 1} bytes")
        return data.ljust(self.packet_size, b'\0')

    def decode(self, data: bytes) -> str:
        return data.split(b'\0', 1)[0].decode(self.encoding, errors='replace')


# ----------------------------------------------------------------------------------------------- replies

@dataclass
class IrisReply:
    """One reply of iriscom: ``ok`` and the message after ``OKAY``, or the error ``code`` and its message."""
    command: str
    ok: bool
    message: str
    code: Optional[int] = None

    def require_ok(self) -> 'IrisReply':
        if not self.ok:
            raise IrisDeviceError(self.code, f"iriscom answered '{self.command}' with E{self.code}: {self.message}")
        return self


def split_reply(text: str) -> Tuple[str, str]:
    """``(echo of the command, status line)`` of a reply ``"<time> <echo>\\n<status>"``."""
    header, sep, status = text.partition('\n')
    if not sep:
        raise IrisProtocolError(f"reply without a status line: {text!r}")
    _time, _, echo = header.partition(' ')
    return echo, status.strip()


def parse_reply(command: str, status: str, settings: ProtocolSettings) -> IrisReply:
    ok = settings.reply_ok
    if status == ok or status.startswith(ok + ' '):
        return IrisReply(command=command, ok=True, message=status[len(ok):].strip())
    match = settings.error_re.match(status)
    if match:
        return IrisReply(command=command, ok=False, code=int(match.group(1)), message=match.group(2).strip())
    raise IrisProtocolError(f"unknown status in the reply to '{command}': {status!r}")


# ----------------------------------------------------------------------------------------------- transport

class _IrisDatagramProtocol(asyncio.DatagramProtocol):
    """Hands the reply to the command being waited for. A datagram whose echo is another command (a late reply to
    an earlier one) is dropped."""

    def __init__(self, settings: ProtocolSettings, address: str):
        self._settings = settings
        self._address = address
        self._command: Optional[str] = None
        self._waiter: Optional[asyncio.Future] = None

    def expect(self, command: Optional[str], waiter: Optional[asyncio.Future]) -> None:
        self._command = command
        self._waiter = waiter

    def datagram_received(self, data: bytes, addr) -> None:
        text = self._settings.decode(data)
        waiter = self._waiter
        if waiter is None or waiter.done():
            logger.debug(f"IRIS CCD ({self._address}): reply nobody waits for, dropped: {text!r}")
            return
        try:
            echo, status = split_reply(text)
        except IrisProtocolError as e:
            waiter.set_exception(e)
            return
        if echo != self._command:
            logger.warning(f"IRIS CCD ({self._address}): reply to '{echo}' while waiting for '{self._command}', "
                           f"dropped")
            return
        waiter.set_result(status)

    def error_received(self, exc: Exception) -> None:
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_exception(exc)

    def connection_lost(self, exc: Optional[Exception]) -> None:
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_exception(exc or ConnectionError("UDP endpoint closed"))


def _forget_result(task: asyncio.Task) -> None:
    """Retrieve the outcome of an operation nobody awaits any more (the client stopped waiting)."""
    if not task.cancelled():
        task.exception()


class IrisSession:
    """The link to one iriscom: its UDP endpoint, the exclusive use of it, and what TIC knows about the
    devices behind it (camera state, filter wheel position)."""

    def __init__(self, host: str, port: int, settings: ProtocolSettings):
        self.host = host
        self.port = port
        self.address = f'{host}:{port}'
        self.settings = settings
        self.loop = asyncio.get_running_loop()
        self._lock = asyncio.Lock()
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._protocol: Optional[_IrisDatagramProtocol] = None
        self.busy_with: Optional[str] = None
        self.camera = CameraState(settings)
        self.filter_position: Optional[int] = None  # None: unknown (server start, moving, after rehome)

    async def _endpoint(self) -> Tuple[asyncio.DatagramTransport, _IrisDatagramProtocol]:
        if self._transport is None or self._transport.is_closing():
            self._drop_endpoint()
            transport, protocol = await self.loop.create_datagram_endpoint(
                lambda: _IrisDatagramProtocol(self.settings, self.address), remote_addr=(self.host, self.port))
            self._transport, self._protocol = transport, protocol
            logger.info(f"IRIS CCD: UDP endpoint created for {self.address}")
        return self._transport, self._protocol

    def _drop_endpoint(self) -> None:
        """Close the endpoint. A new one gets a new local port, so a late reply to an abandoned command never
        reaches it; closing (not just forgetting) the transport releases the socket."""
        transport, self._transport, self._protocol = self._transport, None, None
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass

    async def _exchange(self, command: str, timeout: float) -> IrisReply:
        """Send one command and wait for its reply. The caller holds the lock."""
        # iriscom accepts any prefix of a command word ("q" is quit), so a prefix of a forbidden word is forbidden
        verb = command.split(maxsplit=1)[0].lower() if command.strip() else ''
        if not verb or any(word.startswith(verb) for word in self.settings.forbidden) \
                or any(ord(c) < 32 for c in command):
            raise ValueError(f"command {command!r} must not be sent to iriscom")
        packet = self.settings.encode(command)
        transport, protocol = await self._endpoint()
        waiter = self.loop.create_future()
        protocol.expect(command, waiter)
        answered = False
        try:
            logger.debug(f"IRIS CCD OUT ({self.address}) >>> {command}")
            transport.sendto(packet)
            try:
                status = await asyncio.wait_for(waiter, timeout=timeout)
            except asyncio.TimeoutError:
                raise IrisTimeout(f"iriscom at {self.address} did not answer '{command}' in {timeout:.1f} s")
            answered = True
            logger.debug(f"IRIS CCD IN  ({self.address}) <<< {status}")
        finally:
            protocol.expect(None, None)
            if not answered:
                self._drop_endpoint()
        return parse_reply(command, status, self.settings)

    async def command(self, command: str, timeout: float) -> IrisReply:
        """A short command: waits for another short one, refused while a long operation runs."""
        if self.busy_with is not None:
            raise IrisBusy(self.busy_with)
        async with self._lock:
            return await self._exchange(command, timeout)

    async def operation(self, name: str, body: Callable[[], Awaitable[Any]]) -> Any:
        """A long operation (exposure, filter move). It runs to the end even when the caller stops waiting:
        iriscom does not stop either, and the next command must not be sent before it ends."""
        if self.busy_with is not None:
            raise IrisBusy(self.busy_with)
        self.busy_with = name
        task = self.loop.create_task(self._run_operation(body), name=f'iris.{self.address}.{name}')
        task.add_done_callback(_forget_result)
        return await asyncio.shield(task)

    async def _run_operation(self, body: Callable[[], Awaitable[Any]]) -> Any:
        try:
            async with self._lock:
                return await body()
        finally:
            self.busy_with = None

    def close(self) -> None:
        self._drop_endpoint()


# ----------------------------------------------------------------------------------------------- device state

class CameraState:
    """What TIC knows about the camera: iriscom reports nothing but the replies to TIC's own commands."""

    def __init__(self, settings: ProtocolSettings):
        self._states = settings.camera_states
        self.exposing = False
        self.failure: Optional[str] = None
        self.image_ready = False
        self._duration = 0.0
        self._expected_total = 0.0
        self._started_mono = 0.0
        self.last_duration: Optional[float] = None
        self.last_start: Optional[str] = None
        self.last_image: Optional[dict] = None

    def begin(self, duration: float, expected_total: float) -> float:
        self.exposing = True
        self.failure = None
        self.image_ready = False
        self._duration = duration
        self._expected_total = expected_total
        self._started_mono = time.monotonic()
        started = time.time()
        self.last_start = datetime.fromtimestamp(started, timezone.utc).isoformat(timespec='milliseconds')[:-6]
        return started

    def finish(self, image: dict) -> None:
        self.exposing = False
        self.image_ready = True
        self.last_duration = self._duration
        self.last_image = image

    def fail(self, why: str) -> None:
        self.exposing = False
        self.image_ready = False
        self.failure = why

    def state(self) -> int:
        if self.exposing:
            elapsed = time.monotonic() - self._started_mono
            return self._states['exposing'] if elapsed < self._duration else self._states['reading']
        if self.failure is not None:
            return self._states['error']
        return self._states['idle']

    def percent_completed(self) -> int:
        if self.exposing:
            elapsed = time.monotonic() - self._started_mono
            return int(min(100.0, 100.0 * elapsed / self._expected_total)) if self._expected_total > 0 else 0
        return 100 if self.image_ready else 0


class TelemetryValue:
    """The last value of a NATS subject (detector temperature), kept by a serverish callback subscriber."""

    def __init__(self, subject: str, value_path: Sequence[str], timestamp_path: Optional[Sequence[str]]):
        self.subject = subject
        self.value_path = list(value_path)
        self.timestamp_path = list(timestamp_path) if timestamp_path else None
        self.value: Optional[float] = None
        self.ts: Optional[float] = None
        self.received = asyncio.Event()
        self.subscriber = None
        self.reading_task: Optional[asyncio.Task] = None
        self.reader_error: Optional[str] = None
        self.next_subscribe = 0.0  # monotonic time before which a failed subscription is not retried
        self._warned: set = set()

    def _warn_once(self, what: str, message: str) -> None:
        if what not in self._warned:
            self._warned.add(what)
            logger.warning(message)

    @staticmethod
    def _walk(node: Any, path: Sequence[str]) -> Any:
        for key in path:
            if not isinstance(node, dict) or key not in node:
                raise KeyError('.'.join(path))
            node = node[key]
        return node

    def _timestamp(self, data: dict) -> float:
        if self.timestamp_path is None:
            return time.time()
        try:
            raw = self._walk(data, self.timestamp_path)
            when = dt_ensure_datetime(float(raw) if isinstance(raw, (int, float)) else raw)
            if when is None:
                raise ValueError('empty')
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return when.timestamp()
        except (KeyError, TypeError, ValueError) as e:
            self._warn_once('ts', f"{self.subject}: no measurement time at {'.'.join(self.timestamp_path)} ({e}), "
                                  f"using the time of arrival")
            return time.time()

    async def on_message(self, data: dict, meta: dict) -> None:
        try:
            value = float(self._walk(data, self.value_path))
        except (KeyError, TypeError, ValueError) as e:
            self._warn_once('value', f"{self.subject}: message without a number at {'.'.join(self.value_path)} "
                                     f"({e}), ignored")
            return
        self.value = value
        self.ts = self._timestamp(data)
        self.received.set()

    def reader_alive(self) -> bool:
        return self.reading_task is not None and not self.reading_task.done()


# ----------------------------------------------------------------------------------------------- connector

class IrisCcdConnector(Connector):
    """Connector of components with ``protocol: iris_ccd`` (kinds ``camera`` and ``filterwheel``)."""

    #: iriscom address -> session, shared by every connector (camera and filter wheel are separate components
    #: with separate connectors, but one iriscom).
    _sessions: Dict[str, IrisSession] = {}
    #: NATS subject -> its last value.
    _telemetry: Dict[str, TelemetryValue] = {}
    #: Creates a serverish callback subscriber; replaced in tests.
    subscriber_factory = staticmethod(get_callbacksubscriber)

    def __init__(self, settings: Optional[dict] = None, **kwargs):
        super().__init__(**kwargs)
        cfg = settings if settings is not None else load_protocol_config()
        try:
            self.settings = ProtocolSettings(cfg)
        except IrisConfigError as e:
            raise RuntimeError(str(e)) from e
        self._get_handlers: Dict[str, Dict[str, Callable]] = {
            _CAMERA: {
                'connected': self._get_connected,
                'camerastate': self._get_camerastate,
                'imageready': self._get_imageready,
                'percentcompleted': self._get_percentcompleted,
                'lastexposureduration': self._get_lastexposureduration,
                'lastexposurestarttime': self._get_lastexposurestarttime,
                'lastimage': self._get_lastimage,
                'ccdtemperature': self._get_ccdtemperature,
                'sensorname': self._option_getter('sensorname'),
                'cameraxsize': self._option_getter('cameraxsize'),
                'cameraysize': self._option_getter('cameraysize'),
                'exposuremin': self._option_getter('exposuremin'),
                'exposuremax': self._option_getter('exposuremax'),
                'numx': self._get_numx,
                'numy': self._get_numy,
                'xfer': self._get_xfer,
                **{name: self._fixed_getter(name) for name in self.settings.camera_fixed},
            },
            _FILTERWHEEL: {
                'connected': self._get_connected,
                'position': self._get_position,
                'names': self._get_names,
            },
        }
        self._put_handlers: Dict[str, Dict[str, Callable]] = {
            _CAMERA: {
                'connected': self._put_connected,
                'startexposure': self._put_startexposure,
                'clear': self._put_clear,
                'xfer': self._put_xfer,
                'binx': self._frame_setter('binx'),
                'biny': self._frame_setter('biny'),
                'startx': self._frame_setter('startx'),
                'starty': self._frame_setter('starty'),
                'numx': self._frame_setter('numx'),
                'numy': self._frame_setter('numy'),
            },
            _FILTERWHEEL: {
                'connected': self._put_connected,
                'position': self._put_position,
                'rehome': self._put_rehome,
            },
        }
        logger.info('IrisCcdConnector created')

    # ------------------------------------------------------------------ Connector interface

    async def get(self, component: 'Component', variable: str, kind=None, **data):
        return await self._serve('GET', self._get_handlers, component, variable, data)

    async def put(self, component: 'Component', variable: str, kind=None, **data):
        return await self._serve('PUT', self._put_handlers, component, variable, data)

    async def call(self, component: 'Component', function: str, **data):
        return await self.put(component, function, **data)

    async def _serve(self, operation: str, handlers: Dict[str, Dict[str, Callable]], component, variable: str,
                     data: dict):
        handler = handlers.get(component.kind, {}).get(variable.lower())
        if handler is None:
            raise TreeStructureError(code=3002,
                                     message=f"{operation} {variable!r} is not implemented on {component.kind} "
                                             f"({component.sys_id}) by the IRIS CCD protocol",
                                     severity=TreeStructureError.SEVERITY_CRITICAL)
        where = f"{operation} {component.kind}.{variable}"
        try:
            return await handler(component, data)
        except (BaseCodedError, asyncio.CancelledError):
            raise
        except IrisBusy as e:
            raise TreeOtherError(code=4008, severity=TreeOtherError.SEVERITY_TEMPORARY,
                                 message=f"IRIS CCD busy ({e.operation} in progress), {where} refused")
        except IrisDeviceError as e:
            logger.warning(f"IRIS CCD device error on {where}: {e.message}")
            raise TreeOtherError(code=4009, severity=TreeOtherError.SEVERITY_NORMAL,
                                 message=f"IRIS CCD device error on {where}: {e.message}", device_errno=e.code)
        except IrisProtocolError as e:
            logger.error(f"IRIS CCD reply not understood on {where}: {e}")
            raise TreeValueError(code=2002, message=f"IRIS CCD reply not understood on {where}: {e}")
        except IrisConfigError as e:
            logger.error(f"IRIS CCD configuration error on {where}: {e}")
            raise TreeStructureError(code=3001, message=str(e), severity=TreeStructureError.SEVERITY_CRITICAL)
        except (TypeError, ValueError) as e:
            raise AddressError(code=1003, message=f"Wrong parameters for {where}: {e}")
        except _TRANSPORT_ERRORS as e:
            logger.warning(f"IRIS CCD unreachable on {where}: {e}")
            raise TreeOtherError(code=4005, severity=TreeOtherError.SEVERITY_NORMAL,
                                 message=f"IRIS CCD unreachable on {where}: {e}")

    # ------------------------------------------------------------------ sessions

    def _session(self, component) -> IrisSession:
        address = component.get_option_recursive('address')
        if not address:
            raise IrisConfigError(f"{component.sys_id}: no 'address' (host:port of iriscom) in the configuration")
        host, sep, port = str(address).rpartition(':')
        if not sep or not host or not port.isdigit():
            raise IrisConfigError(f"{component.sys_id}: address {address!r} is not host:port")
        key = f'{host}:{int(port)}'
        session = self._sessions.get(key)
        if session is None or session.loop is not asyncio.get_running_loop():
            if session is not None:
                session.close()
            session = IrisSession(host, int(port), self.settings)
            self._sessions[key] = session
        return session

    @classmethod
    def close_all(cls) -> None:
        """Close every UDP endpoint and forget sessions and telemetry (server stop, tests)."""
        for session in cls._sessions.values():
            session.close()
        cls._sessions.clear()
        for telemetry in cls._telemetry.values():
            if telemetry.reading_task is not None and not telemetry.reading_task.done():
                telemetry.reading_task.cancel()
        cls._telemetry.clear()

    @classmethod
    async def aclose_all(cls) -> None:
        subscribers = [t.subscriber for t in cls._telemetry.values() if t.subscriber is not None]
        cls.close_all()
        for subscriber in subscribers:
            try:
                await subscriber.close()
            except Exception as e:
                logger.debug(f"Closing a NATS subscriber failed: {e}")

    # ------------------------------------------------------------------ parameters and options

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
    def _as_int(value, name: str) -> int:
        try:
            if isinstance(value, bool):
                raise TypeError
            if isinstance(value, float) and not value.is_integer():
                raise ValueError
            return int(value)
        except (TypeError, ValueError):
            raise AddressError(code=1003, message=f"Parameter {name!r} must be an integer, got {value!r}")

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
    def _option(component, name: str):
        value = component.get_option_recursive(name)
        if value is None:
            raise IrisConfigError(f"{component.sys_id}: no {name!r} in the configuration of the component")
        return value

    def _option_getter(self, name: str):
        async def getter(component, data):
            return self._option(component, name)
        return getter

    def _fixed_getter(self, name: str):
        async def getter(component, data):
            return self.settings.camera_fixed[name]
        return getter

    def _full_frame(self, component, axis: str) -> int:
        size = int(self._option(component, f'camera{axis}size'))
        return size // int(self.settings.camera_fixed[f'bin{axis}'])

    def _frame_setter(self, name: str):
        """binx, biny, startx, starty, numx, numy: iriscom always reads the full detector without binning, so the
        only value accepted is the one in force; it is not sent anywhere."""
        async def setter(component, data):
            value = self._as_int(self._param(data, name), name)
            if name in ('numx', 'numy'):
                allowed = self._full_frame(component, name[-1])
            else:
                allowed = int(self.settings.camera_fixed[name])
            if value != allowed:
                raise AddressError(code=1003, message=f"IRIS CCD supports only {name} = {allowed}, got {value}")
            return None
        return setter

    # ------------------------------------------------------------------ common

    async def _get_connected(self, component, data) -> bool:
        """``sync``: iriscom answers and the camera electronics respond. While a long operation runs iriscom is
        evidently alive, and it would not answer before the operation ends."""
        session = self._session(component)
        if session.busy_with is not None:
            return True
        reply = await session.command(self.settings.commands['sync'], self.settings.timeouts['command'])
        if not reply.ok:
            logger.warning(f"IRIS CCD ({session.address}): sync answered E{reply.code}: {reply.message}")
        return reply.ok

    async def _put_connected(self, component, data) -> None:
        if not self._as_bool(self._param(data, 'Connected'), 'Connected'):
            return None
        session = self._session(component)
        if session.busy_with is not None:
            return None
        reply = await session.command(self.settings.commands['sync'], self.settings.timeouts['command'])
        reply.require_ok()
        return None

    # ------------------------------------------------------------------ camera

    async def _get_camerastate(self, component, data) -> int:
        return self._session(component).camera.state()

    async def _get_imageready(self, component, data) -> bool:
        return self._session(component).camera.image_ready

    async def _get_percentcompleted(self, component, data) -> int:
        return self._session(component).camera.percent_completed()

    async def _get_lastexposureduration(self, component, data) -> Optional[float]:
        return self._session(component).camera.last_duration

    async def _get_lastexposurestarttime(self, component, data) -> Optional[str]:
        return self._session(component).camera.last_start

    async def _get_lastimage(self, component, data) -> Optional[dict]:
        image = self._session(component).camera.last_image
        return dict(image) if image is not None else None

    async def _get_numx(self, component, data) -> int:
        return self._full_frame(component, 'x')

    async def _get_numy(self, component, data) -> int:
        return self._full_frame(component, 'y')

    async def _put_startexposure(self, component, data) -> dict:
        duration = self._as_float(self._param(data, 'Duration'), 'Duration')
        light_param = self._param(data, 'Light', required=False)
        light = True if light_param is None else self._as_bool(light_param, 'Light')
        low = float(self._option(component, 'exposuremin'))
        high = float(self._option(component, 'exposuremax'))
        if not low <= duration <= high:
            raise AddressError(code=1003, message=f"Duration {duration} s outside {low}..{high} s")
        if not light:
            logger.warning(f"{component.sys_id}: Light=False requested, but iriscom has no shutter; "
                           f"the exposure is taken as usual")
        session = self._session(component)
        return await session.operation('exposure', lambda: self._expose(session, duration, light))

    async def _expose(self, session: IrisSession, duration: float, light: bool) -> dict:
        s = self.settings
        camera = session.camera
        try:
            reply = await session._exchange(f"{s.commands['exposure_time']} {duration!r}", s.timeouts['command'])
            reply.require_ok()
            match = s.exposure_time_re.match(reply.message)
            if not match:
                raise IrisProtocolError(f"unexpected reply to '{reply.command}': {reply.message!r}")
            set_to = float(match.group(1))
            if abs(set_to - duration) > s.exposure_time_tolerance:
                raise IrisDeviceError(None, f"iriscom set the exposure time to {set_to} s instead of {duration} s")
        except Exception as e:
            camera.fail(str(e))
            raise
        expected = duration + s.timeouts['exposure_overhead']
        started = camera.begin(duration, expected)
        logger.info(f"IRIS CCD ({session.address}): exposure {duration} s started")
        try:
            reply = await session._exchange(s.commands['expose'], expected + s.timeouts['exposure_margin'])
            reply.require_ok()
            match = s.exposure_files_re.match(reply.message)
            if not match:
                raise IrisDeviceError(None, f"exposure ended without image files: {reply.message!r}")
            image = self._image_info(match.group('name'), started, duration, light)
        except BaseException as e:
            camera.fail(str(e) or type(e).__name__)
            logger.error(f"IRIS CCD ({session.address}): exposure {duration} s failed: {e}")
            raise
        camera.finish(image)
        logger.info(f"IRIS CCD ({session.address}): exposure {duration} s done, {image['name']}")
        return dict(image)

    def _image_info(self, name: str, started: float, duration: float, light: bool) -> dict:
        s = self.settings
        number = s.frame_number_re.search(name)
        return {
            'name': name,
            'number': int(number.group(1)) if number else None,
            'dir': s.images_dir,
            'files': {frame: posixpath.join(s.images_dir, f'{name}{letter}{s.images_suffix}')
                      for frame, letter in s.frames.items()},
            'start': datetime.fromtimestamp(started, timezone.utc).isoformat(timespec='milliseconds')[:-6],
            'duration': duration,
            'light': light,
        }

    async def _put_clear(self, component, data) -> str:
        session = self._session(component)
        reply = await session.command(self.settings.commands['clear'], self.settings.timeouts['command'])
        return reply.require_ok().message

    def _parse_xfer(self, reply: IrisReply) -> dict:
        match = self.settings.xfer_re.match(reply.message)
        if not match:
            raise IrisProtocolError(f"unexpected reply to '{reply.command}': {reply.message!r}")
        host = match.group('host')
        return {'host': None if host == self.settings.xfer_disabled_host else host,
                'port': int(match.group('port'))}

    async def _get_xfer(self, component, data) -> dict:
        session = self._session(component)
        reply = await session.command(self.settings.commands['xfer'], self.settings.timeouts['command'])
        return self._parse_xfer(reply.require_ok())

    async def _put_xfer(self, component, data) -> dict:
        """``Host`` (``'-'`` or empty switches the transfer off) and optional ``Port``. With a host set, iriscom
        sends every frame there and an exposure fails when the host does not receive it."""
        host = self._param(data, 'Host')
        host = self.settings.xfer_disabled_host if host in (None, '') else str(host)
        if not host or any(c.isspace() or ord(c) < 32 for c in host):
            raise AddressError(code=1003, message=f"Parameter 'Host' must be one word, got {host!r}")
        command = f"{self.settings.commands['xfer']} {host}"
        port = self._param(data, 'Port', required=False)
        if port is not None:
            port = self._as_int(port, 'Port')
            if not 0 <= port <= 65535:
                raise AddressError(code=1003, message=f"Parameter 'Port' must be 0..65535, got {port}")
            command += f" {port}"
        session = self._session(component)
        reply = await session.command(command, self.settings.timeouts['command'])
        return self._parse_xfer(reply.require_ok())

    async def _get_ccdtemperature(self, component, data) -> float:
        cfg = self._option(component, 'ccdtemperature')
        if not isinstance(cfg, dict):
            raise IrisConfigError(f"{component.sys_id}: 'ccdtemperature' must be a mapping")
        for key in ('subject', 'value_path', 'max_age'):
            if key not in cfg:
                raise IrisConfigError(f"{component.sys_id}: no 'ccdtemperature.{key}' in the configuration")
        telemetry = await self._telemetry_for(cfg)
        if telemetry.value is None and telemetry.reader_alive():
            try:
                await asyncio.wait_for(telemetry.received.wait(), timeout=self.settings.first_value_wait)
            except asyncio.TimeoutError:
                pass
        if telemetry.value is None:
            why = f" (reader: {telemetry.reader_error})" if telemetry.reader_error else ''
            raise TreeOtherError(code=4005, severity=TreeOtherError.SEVERITY_NORMAL,
                                 message=f"No detector temperature received from {telemetry.subject}{why}")
        age = time.time() - telemetry.ts
        if age > float(cfg['max_age']):
            raise TreeOtherError(code=4005, severity=TreeOtherError.SEVERITY_NORMAL,
                                 message=f"Detector temperature from {telemetry.subject} is {age:.0f} s old "
                                         f"(limit {float(cfg['max_age']):.0f} s)")
        return telemetry.value

    async def _telemetry_for(self, cfg: dict) -> TelemetryValue:
        subject = str(cfg['subject'])
        telemetry = self._telemetry.get(subject)
        if telemetry is None:
            telemetry = TelemetryValue(subject, cfg['value_path'], cfg.get('timestamp_path'))
            self._telemetry[subject] = telemetry
        if telemetry.reader_alive() or time.monotonic() < telemetry.next_subscribe:
            return telemetry
        telemetry.next_subscribe = time.monotonic() + self.settings.resubscribe_interval
        try:
            if telemetry.subscriber is not None:
                await telemetry.subscriber.close()
        except Exception:
            pass
        try:
            subscriber = self.subscriber_factory(subject, deliver_policy=self.settings.deliver_policy)
            reading = await subscriber.subscribe(telemetry.on_message)
        except Exception as e:
            telemetry.subscriber = None
            telemetry.reading_task = None
            telemetry.reader_error = str(e) or type(e).__name__
            raise TreeOtherError(code=4005, severity=TreeOtherError.SEVERITY_NORMAL,
                                 message=f"Cannot subscribe to {subject}: {telemetry.reader_error}") from e
        telemetry.subscriber = subscriber
        telemetry.reading_task = getattr(reading, 'task', reading)
        telemetry.reader_error = None

        def ended(task: asyncio.Task, telemetry=telemetry) -> None:
            if task.cancelled():
                telemetry.reader_error = 'reader cancelled'
            else:
                error = task.exception()
                telemetry.reader_error = str(error) if error else 'reader ended'
            logger.error(f"NATS reader of {telemetry.subject} stopped: {telemetry.reader_error}")

        if isinstance(telemetry.reading_task, asyncio.Future):
            telemetry.reading_task.add_done_callback(ended)
        logger.info(f"IRIS CCD: subscribed to {subject}")
        return telemetry

    # ------------------------------------------------------------------ filter wheel

    def _filters(self, component) -> Dict[str, int]:
        filters = self._option(component, 'filters')
        if not isinstance(filters, dict) or not filters:
            raise IrisConfigError(f"{component.sys_id}: 'filters' must map filter names to slot numbers")
        out = {}
        for name, slot in filters.items():
            if isinstance(slot, bool) or not isinstance(slot, int) or \
                    not self.settings.first_slot <= slot <= self.settings.last_slot:
                raise IrisConfigError(f"{component.sys_id}: filter {name!r} has slot {slot!r}, expected an integer "
                                      f"{self.settings.first_slot}..{self.settings.last_slot}")
            out[str(name)] = slot
        return out

    async def _get_names(self, component, data) -> Dict[str, int]:
        return self._filters(component)

    async def _get_position(self, component, data) -> int:
        position = self._session(component).filter_position
        return self.settings.unknown_position if position is None else position

    def _slot(self, component, value) -> Tuple[int, str]:
        filters = self._filters(component)
        if isinstance(value, str) and value in filters:
            return filters[value], value
        slot = self._as_int(value, 'Position')
        for name, s in filters.items():
            if s == slot:
                return slot, name
        raise AddressError(code=1003, message=f"Slot {slot} has no filter in the configuration; available: "
                                              f"{', '.join(f'{n}={s}' for n, s in filters.items())}")

    async def _put_position(self, component, data) -> dict:
        slot, name = self._slot(component, self._param(data, 'Position'))
        session = self._session(component)
        if self.settings.skip_same_position and session.filter_position == slot:
            logger.info(f"IRIS CCD ({session.address}): filter {name} ({slot}) already in place, not moved")
            return {'position': slot, 'name': name, 'moved': False}
        return await session.operation('filter move', lambda: self._move_filter(session, slot, name))

    async def _move_filter(self, session: IrisSession, slot: int, name: str) -> dict:
        s = self.settings
        session.filter_position = None
        logger.info(f"IRIS CCD ({session.address}): moving filter wheel to {name} ({slot})")
        reply = await session._exchange(f"{s.commands['filter']} {slot}", s.timeouts['filter_move'])
        reply.require_ok()
        session.filter_position = slot
        logger.info(f"IRIS CCD ({session.address}): filter wheel at {name} ({slot})")
        return {'position': slot, 'name': name, 'moved': True}

    async def _put_rehome(self, component, data) -> dict:
        if not self.settings.rehome_enabled:
            raise TreeStructureError(code=3002, severity=TreeStructureError.SEVERITY_CRITICAL,
                                     message="Filter wheel rehome is disabled (filterwheel.rehome_enabled in "
                                             "iris_ccd_config.yml)")
        session = self._session(component)
        return await session.operation('filter rehome', lambda: self._rehome(session))

    async def _rehome(self, session: IrisSession) -> dict:
        s = self.settings
        session.filter_position = None
        logger.info(f"IRIS CCD ({session.address}): filter wheel rehome started")
        reply = await session._exchange(f"{s.commands['filter']} {s.commands['filter_rehome']}",
                                        s.timeouts['filter_rehome'])
        reply.require_ok()
        logger.info(f"IRIS CCD ({session.address}): filter wheel rehome done, no filter selected")
        return {'position': s.unknown_position, 'name': None, 'moved': True}
