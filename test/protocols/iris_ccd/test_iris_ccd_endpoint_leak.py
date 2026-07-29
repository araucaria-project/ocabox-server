"""Regression tests for the IRIS CCD UDP endpoint FD leak (v2.3.16).

`_execute_command` used to `del self._endpoints[address]` on timeout / reconnect
without closing the transport, orphaning one UDP socket per timeout. With the
device unreachable that leaked continuously until RLIMIT_NOFILE was exhausted —
previously masked by the hourly DNS-cascade restarts, exposed once the process
ran stably. These tests pin the close-on-drop behaviour.

Deterministic and self-contained: a fake transport stands in for the UDP socket,
so no IRIS CCD device or real sockets are required.
"""
import asyncio
import types
import unittest

from obsrv.protocols.iris_ccd.iris_ccd_connector import IrisCcdConnector


class _FakeTransport:
    def __init__(self):
        self._closing = False
        self.close_calls = 0

    def is_closing(self):
        return self._closing

    def close(self):
        self.close_calls += 1
        self._closing = True

    def sendto(self, data):
        pass


class IrisCcdEndpointLeakTest(unittest.TestCase):

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.conn = IrisCcdConnector()

    def tearDown(self):
        asyncio.set_event_loop(None)
        self.loop.close()

    def _seed_endpoint(self, address):
        transport = _FakeTransport()
        protocol = types.SimpleNamespace(response_future=None)
        self.conn._endpoints[address] = (transport, protocol)
        self.conn._locks[address] = asyncio.Lock()
        return transport

    def test_drop_endpoint_closes_and_removes(self):
        addr = "1.2.3.4:4980"
        transport = self._seed_endpoint(addr)
        self.conn._drop_endpoint(addr)
        self.assertNotIn(addr, self.conn._endpoints)
        self.assertEqual(transport.close_calls, 1)
        # idempotent: dropping an absent address is a no-op, not a KeyError
        self.conn._drop_endpoint(addr)

    def test_timeout_closes_socket_no_leak(self):
        """Each timed-out command must close its transport, not just forget it."""
        addr = "1.2.3.4:4980"
        self.conn._timeout = 0.01  # force a fast asyncio.wait_for timeout

        async def coro():
            closed = []
            for _ in range(10):
                transport = self._seed_endpoint(addr)
                with self.assertRaises(TimeoutError):
                    await self.conn._execute_command(addr, "filter status")
                closed.append(transport)
            # every endpoint was dropped from the cache and its socket closed
            self.assertEqual(self.conn._endpoints, {})
            self.assertTrue(all(t.close_calls == 1 for t in closed))
            self.assertTrue(all(t.is_closing() for t in closed))

        self.loop.run_until_complete(coro())


if __name__ == "__main__":
    unittest.main()
