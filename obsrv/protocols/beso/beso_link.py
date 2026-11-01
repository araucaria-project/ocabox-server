"""TCP link to the BESO server: one connection, one command in flight, one reply per command.

BESO has no transaction ids, so a reply can only be matched to a command by order. Anything that breaks the
order (timeout, unsolicited bytes, oversized or truncated reply) makes the link unusable: the caller must
close it and open a new one. This module never retries; the worker decides.

Reply framing is not documented. The CLI reads one ``recv()`` per reply. Here a reply ends at the first
configured terminator byte sequence or, as a fallback, after ``idle_gap`` seconds of silence following the
first byte. Terminator bytes that trail a reply (``\\n``, ``\\0``) are discarded.
"""
import asyncio
import logging
import socket
from typing import Optional

from obsrv.protocols.beso.beso_config import Framing, Timeouts
from obsrv.protocols.beso.beso_errors import BesoLinkError, BesoProtocolError, BesoTimeoutError

logger = logging.getLogger(__name__.rsplit('.')[-1])


class _ReplyProtocol(asyncio.Protocol):
    """Collects incoming bytes into a bounded buffer and wakes a single waiter."""

    def __init__(self, loop: asyncio.AbstractEventLoop, max_bytes: int):
        self._loop = loop
        self._max_bytes = max_bytes
        self.buffer = bytearray()
        self.transport: Optional[asyncio.Transport] = None
        self.closed = False
        self.close_exc: Optional[BaseException] = None
        self.overflow = False
        self._waiter: Optional[asyncio.Future] = None
        self._can_write = asyncio.Event()
        self._can_write.set()
        self.closed_future: asyncio.Future = loop.create_future()

    def connection_made(self, transport):
        self.transport = transport

    def data_received(self, data: bytes):
        if len(self.buffer) + len(data) > self._max_bytes:
            # Never grow beyond the limit; the reader sees `overflow` and the link is rebuilt.
            self.overflow = True
            self.buffer.clear()
            self.transport.abort()
            return
        self.buffer.extend(data)
        self._wake()

    def eof_received(self):
        return False  # close the transport

    def connection_lost(self, exc):
        self.closed = True
        self.close_exc = exc
        self._can_write.set()
        self._wake()
        if not self.closed_future.done():
            self.closed_future.set_result(None)

    def pause_writing(self):
        self._can_write.clear()

    def resume_writing(self):
        self._can_write.set()

    def _wake(self):
        waiter = self._waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    async def wait_data(self, timeout: float) -> bool:
        """Wait until new bytes arrive or the connection closes. False on timeout."""
        if self.closed:
            return True
        self._waiter = self._loop.create_future()
        try:
            await asyncio.wait_for(self._waiter, timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self._waiter = None

    async def wait_writable(self, timeout: float) -> None:
        await asyncio.wait_for(self._can_write.wait(), timeout)


class BesoLink:
    """An open connection. Create with ``await BesoLink.open(...)``; always ``await close()``."""

    def __init__(self, host: str, port: int, framing: Framing, timeouts: Timeouts,
                 transport: asyncio.Transport, protocol: _ReplyProtocol):
        self.host = host
        self.port = port
        self._framing = framing
        self._timeouts = timeouts
        self._transport = transport
        self._protocol = protocol
        self._loop = asyncio.get_running_loop()
        self._terminators = framing.reply_terminators
        self._trailing = frozenset(b''.join(framing.reply_terminators) + b' \t\r\n\x00')

    @classmethod
    async def open(cls, host: str, port: int, framing: Framing, timeouts: Timeouts,
                   tcp_keepalive: bool = True) -> 'BesoLink':
        loop = asyncio.get_running_loop()
        try:
            transport, protocol = await asyncio.wait_for(
                loop.create_connection(lambda: _ReplyProtocol(loop, framing.max_reply_bytes), host, port),
                timeouts.connect)
        except asyncio.TimeoutError:
            raise BesoTimeoutError(f"connect to {host}:{port} timed out after {timeouts.connect} s") from None
        except OSError as exc:
            raise BesoLinkError(f"connect to {host}:{port} failed: {exc}") from exc
        if tcp_keepalive:
            _enable_tcp_keepalive(transport.get_extra_info('socket'))
        return cls(host, port, framing, timeouts, transport, protocol)

    @property
    def is_open(self) -> bool:
        return not self._protocol.closed

    def _discard_trailing_terminators(self) -> None:
        buffer = self._protocol.buffer
        index = 0
        while index < len(buffer) and buffer[index] in self._trailing:
            index += 1
        if index:
            del buffer[:index]

    async def exchange(self, command: str, timeout: float) -> str:
        """Send one command and return its reply (stripped, without terminators)."""
        protocol = self._protocol
        if protocol.closed:
            raise BesoLinkError("connection is closed")
        self._discard_trailing_terminators()
        if protocol.buffer:
            stray = bytes(protocol.buffer[:64])
            raise BesoProtocolError(f"unsolicited data before command {command!r}: {stray!r}")
        data = command.encode(self._framing.encoding) + self._framing.command_terminator
        try:
            self._transport.write(data)
            await protocol.wait_writable(self._timeouts.write)
        except asyncio.TimeoutError:
            raise BesoTimeoutError(f"write of {command!r} timed out") from None
        except (OSError, RuntimeError) as exc:
            raise BesoLinkError(f"write of {command!r} failed: {exc}") from exc
        return await self._read_reply(command, timeout)

    def _split_reply(self) -> Optional[bytes]:
        buffer = self._protocol.buffer
        end = -1
        for terminator in self._terminators:
            position = buffer.find(terminator)
            if position != -1 and (end == -1 or position < end):
                end = position
        if end == -1:
            return None
        reply = bytes(buffer[:end])
        del buffer[:end]
        self._discard_trailing_terminators()
        if buffer:
            # A second message right behind the first one means the link is out of step.
            raise BesoProtocolError(f"unexpected data after reply {reply!r}: {bytes(buffer[:64])!r}")
        return reply

    async def _read_reply(self, command: str, timeout: float) -> str:
        protocol = self._protocol
        idle_gap = self._framing.idle_gap
        deadline = self._loop.time() + timeout
        while True:
            reply = self._split_reply()
            if reply is not None:
                return self._decode(reply)
            if protocol.overflow:
                raise BesoProtocolError(f"reply to {command!r} exceeds {self._framing.max_reply_bytes} bytes")
            if protocol.closed:
                partial = bytes(protocol.buffer[:64])
                raise BesoLinkError(f"connection closed by BESO while waiting for reply to {command!r}"
                                    + (f" (partial: {partial!r})" if partial else ""))
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                raise BesoTimeoutError(f"no complete reply to {command!r} within {timeout} s")
            use_gap = bool(protocol.buffer) and idle_gap > 0
            wait = min(remaining, idle_gap) if use_gap else remaining
            arrived = await protocol.wait_data(wait)
            if not arrived and use_gap and wait == idle_gap:
                reply = bytes(protocol.buffer)
                protocol.buffer.clear()
                return self._decode(reply)

    def _decode(self, reply: bytes) -> str:
        return reply.decode(self._framing.encoding, errors='replace').strip(' \t\r\n\x00')

    async def close(self) -> None:
        """Close the connection and wait (bounded) until the transport is really gone."""
        if not self._protocol.closed:
            self._transport.close()
        try:
            await asyncio.wait_for(asyncio.shield(self._protocol.closed_future), self._timeouts.close)
        except asyncio.TimeoutError:
            self._transport.abort()
        self._protocol.buffer.clear()


def _enable_tcp_keepalive(sock: Optional[socket.socket]) -> None:
    # Detects a dead peer on an idle link; BESO accepts one client only, so a half-open connection must not linger.
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for option, value in (('TCP_KEEPIDLE', 60), ('TCP_KEEPINTVL', 10), ('TCP_KEEPCNT', 5)):
            if hasattr(socket, option):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, option), value)
    except OSError as exc:
        logger.debug("Cannot enable TCP keepalive: %s", exc)
