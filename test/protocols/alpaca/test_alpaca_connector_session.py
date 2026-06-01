"""Session-management tests for the production AlpacaConnector
(`obsrv.protocols.alpaca.alpaca_connector`).

These are self-contained: they inspect the shared `aiohttp.ClientSession` and its
connector configuration without touching the network, so they do not require the
ALPACA simulator or a NATS server.

Regression cover for the DNS-resolver-wedge fix (v2.3.16): a single long-lived
session per connector, created lazily and exactly once under concurrent first use,
with bounded timeouts, a per-host connection cap, and connector-level DNS caching.
"""
import asyncio
import unittest

from aiohttp.resolver import DefaultResolver

from obsrv.protocols.alpaca.alpaca_connector import (
    AlpacaConnector,
    _DEFAULT_TIMEOUT,
    _DEFAULT_LIMIT_PER_HOST,
)


class AlpacaConnectorSessionTest(unittest.TestCase):

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        asyncio.set_event_loop(None)
        self.loop.close()

    def test_session_created_lazily(self):
        """No session exists until the first request needs one."""
        alpaca = AlpacaConnector()
        self.assertIsNone(alpaca._http_session)

        async def coro():
            session = await alpaca._ensure_session()
            self.assertIsNotNone(session)
            self.assertFalse(session.closed)
            await alpaca.close()

        self.loop.run_until_complete(coro())

    def test_concurrent_first_use_shares_one_session(self):
        """A thundering herd of concurrent first requests must create exactly one
        session, not one per request (the lock collapses the race)."""
        alpaca = AlpacaConnector()

        async def coro():
            sessions = await asyncio.gather(*[alpaca._ensure_session() for _ in range(25)])
            self.assertTrue(all(s is sessions[0] for s in sessions))
            await alpaca.close()

        self.loop.run_until_complete(coro())

    def test_session_is_reused(self):
        """Subsequent calls return the same session instance (keep-alive)."""
        alpaca = AlpacaConnector()

        async def coro():
            first = await alpaca._ensure_session()
            second = await alpaca._ensure_session()
            self.assertIs(first, second)
            await alpaca.close()

        self.loop.run_until_complete(coro())

    def test_session_configuration(self):
        """Bounded timeout, per-host cap and DNS cache are applied."""
        alpaca = AlpacaConnector()

        async def coro():
            session = await alpaca._ensure_session()
            self.assertEqual(session.timeout.total, _DEFAULT_TIMEOUT.total)
            self.assertEqual(session.timeout.connect, _DEFAULT_TIMEOUT.connect)
            self.assertEqual(session.timeout.sock_connect, _DEFAULT_TIMEOUT.sock_connect)
            self.assertEqual(session.connector._limit_per_host, _DEFAULT_LIMIT_PER_HOST)
            self.assertTrue(session.connector._use_dns_cache)
            await alpaca.close()

        self.loop.run_until_complete(coro())

    def test_recreated_after_close(self):
        """After an explicit close, the next request transparently builds a fresh
        session (the `session.closed` branch in `_ensure_session`)."""
        alpaca = AlpacaConnector()

        async def coro():
            first = await alpaca._ensure_session()
            await alpaca.close()
            self.assertTrue(alpaca.is_session_closed())
            second = await alpaca._ensure_session()
            self.assertIsNot(first, second)
            self.assertFalse(second.closed)
            await alpaca.close()

        self.loop.run_until_complete(coro())

    def test_default_resolver_is_async(self):
        """aiodns must be installed so aiohttp uses the async (c-ares) resolver
        rather than the blocking getaddrinfo thread-pool. This is the dependency
        half of the wedge fix; if it regresses, DNS goes back on the thread-pool."""
        self.assertEqual(DefaultResolver.__name__, "AsyncResolver")


if __name__ == "__main__":
    unittest.main()
