"""The link to one iriscom (:class:`IrisSession`): replies, the UDP endpoint, the exclusive use of iriscom (short
commands and long operations), and what TIC knows about the devices behind it (:class:`CameraState`, the filter
wheel position).

One session per iriscom address is shared by every component with that address (``IrisCcdConnector._sessions``).
How iriscom talks: the docstring of ``iris_ccd_connector.py``.
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Tuple

from obsrv.protocols.iris_ccd.iris_ccd_exceptions import IrisBusy, IrisDeviceError, IrisProtocolError, IrisTimeout
from obsrv.protocols.iris_ccd.iris_ccd_settings import ProtocolSettings

logger = logging.getLogger('iris_ccd_connector')     # one logger for the whole IRIS CCD connector


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

    async def exchange(self, command: str, timeout: float) -> IrisReply:
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
            return await self.exchange(command, timeout)

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
