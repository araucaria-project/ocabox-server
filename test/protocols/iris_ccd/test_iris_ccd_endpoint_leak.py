"""Regression tests for the IRIS CCD UDP endpoint FD leak (v2.3.16).

A command iriscom did not answer used to drop the cached UDP endpoint without closing it, orphaning one socket per
timeout. With the device unreachable that leaked continuously until RLIMIT_NOFILE was exhausted. These tests pin
the close-on-drop behaviour of :class:`IrisSession`: an unanswered command closes its transport (a late reply must
not reach the endpoint of the next command), and the next command gets a fresh one.

Deterministic and self-contained: a fake transport stands in for the UDP socket, so no IRIS CCD device or real
sockets are required.
"""
import asyncio
import unittest

from obsrv.protocols.iris_ccd.iris_ccd_connector import IrisSession, IrisTimeout, ProtocolSettings
from test.protocols.iris_ccd.fake_iriscom import fast_settings


class _FakeTransport:
    def __init__(self):
        self._closing = False
        self.close_calls = 0
        self.sent = []

    def is_closing(self):
        return self._closing

    def close(self):
        self.close_calls += 1
        self._closing = True

    def sendto(self, data, addr=None):
        self.sent.append(data)


class IrisCcdEndpointLeakTest(unittest.TestCase):

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.transports = []

    def tearDown(self):
        asyncio.set_event_loop(None)
        self.loop.close()

    def _session(self) -> IrisSession:
        async def make():
            return IrisSession('1.2.3.4', 4980, ProtocolSettings(fast_settings()))
        session = self.loop.run_until_complete(make())

        async def create_datagram_endpoint(factory, remote_addr=None):
            transport, protocol = _FakeTransport(), factory()
            self.transports.append(transport)
            return transport, protocol
        session.loop.create_datagram_endpoint = create_datagram_endpoint
        return session

    def test_drop_endpoint_closes_and_forgets(self):
        session = self._session()
        self.loop.run_until_complete(session._endpoint())
        transport = self.transports[0]
        session._drop_endpoint()
        self.assertIsNone(session._transport)
        self.assertEqual(transport.close_calls, 1)
        # idempotent: dropping an absent endpoint is a no-op
        session._drop_endpoint()
        self.assertEqual(transport.close_calls, 1)

    def test_timeout_closes_socket_no_leak(self):
        """Each unanswered command must close its transport, not just forget it."""
        session = self._session()

        async def coro():
            for _ in range(10):
                with self.assertRaises(IrisTimeout):
                    await session.command('sync', 0.01)
        self.loop.run_until_complete(coro())
        self.assertEqual(len(self.transports), 10)  # a fresh endpoint for every command
        self.assertIsNone(session._transport)
        self.assertTrue(all(t.close_calls == 1 for t in self.transports))
        self.assertTrue(all(len(t.sent[0]) == 1024 for t in self.transports))

    def test_cancelled_command_closes_socket(self):
        session = self._session()

        async def coro():
            task = asyncio.ensure_future(session.command('sync', 5.0))
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.loop.run_until_complete(coro())
        self.assertEqual(self.transports[0].close_calls, 1)
        self.assertIsNone(session._transport)


if __name__ == "__main__":
    unittest.main()
