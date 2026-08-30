"""End-to-end Staleness Contract: a real obcom ConditionalCycleQuery (phase 1,
ocabox-common 1.3.0) subscribed to a real freezer+cache tree (phase 2, 2.6.0),
with a controllable in-process transport. Exercises the full protocol:
fragment on the wire, server-side masking + rich None + stateless dedup,
recovery-as-change, client-side synthesis on transport silence, and
redelivery after reconnect.
"""

import asyncio
import time
import unittest

from obcom.comunication.comunication_error import CommunicationTimeoutError
from obcom.comunication.cycle_query import ConditionalCycleQuery
from obcom.comunication.error_policy import Backoff, ErrorPolicy, SeverityAction, SeverityRule, ValuePolicy
from obcom.data_colection.value_call import ValueRequest
from obcom.data_colection.address import Address
from obsrv.tree_components.base_components.tree_provider import TreeProvider
from obsrv.tree_components.specialized_components import TreeCache, TreeConditionalFreezer

from test.data_collection.specialistic_components.test_staleness_contract import SwitchableProvider


class DirectTreeSolver:
    """Client-request solver that talks straight to the tree (no ZMQ)."""

    def __init__(self, root: TreeProvider):
        self.root = root
        self.transport_down = False

    async def send_request(self, requests, timeout=None, no_wait=False):
        if self.transport_down:
            await asyncio.sleep(0.03)
            raise CommunicationTimeoutError(message='transport down')
        out = []
        for r in requests:
            rq = r.copy()
            rq.request_timeout = time.time() + 0.4  # what ClientAPI would stamp
            out.append(await self.root.get_response(rq))
        return out


class StalenessContractIntegrationTest(unittest.IsolatedAsyncioTestCase):
    TOLERANCE = 0.2

    async def asyncSetUp(self):
        self.provider = SwitchableProvider('switchable', 'provider2')
        self.cache = TreeCache('cache', self.provider)
        self.freezer = TreeConditionalFreezer('test_sample_freezer', self.cache)
        self.freezer.set_max_refreshes(2)
        self.freezer._min_time_of_data_tolerance = 0.05
        self.freezer._alarm_timeout_offset = 0.1
        self.root = TreeProvider('root', 'provider1', self.freezer)
        await self.root.run()
        self.solver = DirectTreeSolver(self.root)
        self.deliveries = []  # (v, tags) per callback fire

    async def asyncTearDown(self):
        await self.root.stop()

    def _make_cq(self):
        policy = ErrorPolicy.DISPLAY.with_overrides(
            # quick TEMPORARY retries so transport blips don't slow the test
            temporary=SeverityRule(action=SeverityAction.RETRY, backoff=Backoff.immediate()),
        )
        request = ValueRequest(Address('provider1.provider2.reading'),
                               time_of_data_tolerance=self.TOLERANCE, cycle_query=True)
        cq = ConditionalCycleQuery(crs=self.solver, list_request=[request],
                                   delay=0.01, request_timeout=0.5, error_policy=policy)

        async def on_msg(resps):
            r = resps[0]
            self.deliveries.append((r.value.v, dict(r.value.tags or {})))

        cq.add_callback_async_method(on_msg)
        return cq

    async def _wait_for(self, count, timeout=3.0):
        deadline = asyncio.get_event_loop().time() + timeout
        while len(self.deliveries) < count and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.01)

    async def test_full_contract_lifecycle(self):
        cq = self._make_cq()
        cq.start()
        try:
            # --- A: healthy subscribe delivers the value ---
            await self._wait_for(1)
            self.assertEqual(self.deliveries[0][0], 42)

            # --- B: device dies -> exactly one SERVER-side rich None ---
            self.provider.dead = True
            await self._wait_for(2)
            v, tags = self.deliveries[1]
            self.assertIsNone(v)
            self.assertEqual(tags['reason'], 4005)
            self.assertEqual(tags['last_good'], 42)
            self.assertIn('from_cf', tags, 'the None must come from the freezer, not client synthesis')
            # continued outage: dedup — no further Nones
            await asyncio.sleep(3 * self.TOLERANCE)
            self.assertEqual(len(self.deliveries), 2, f'unexpected extra deliveries: {self.deliveries}')

            # --- C: device heals with the SAME payload -> recovery delivered ---
            self.provider.dead = False
            await self._wait_for(3)
            self.assertEqual(self.deliveries[2][0], 42)

            # --- D: transport dies -> CLIENT-side synthesized None ---
            self.solver.transport_down = True
            await self._wait_for(4)
            v, tags = self.deliveries[3]
            self.assertIsNone(v)
            self.assertEqual(tags['reason'], 4002)
            self.assertNotIn('from_cf', tags, 'this None must be client-synthesized')
            self.assertTrue(not cq.is_stopped(), 'declared policy must survive transport loss')

            # --- E: transport returns -> current value redelivered ---
            self.solver.transport_down = False
            await self._wait_for(5)
            self.assertEqual(self.deliveries[4][0], 42)
            self.assertEqual(len(self.deliveries), 5)
        finally:
            await cq.stop_and_wait()

    async def test_undeclared_client_sees_no_contract_machinery(self):
        """A SERVICE (wire-inert) subscription against the same tree: no
        value_policy on the wire, no Nones — errors surface per severity
        actions exactly as before the contract existed."""
        request = ValueRequest(Address('provider1.provider2.reading'),
                               time_of_data_tolerance=self.TOLERANCE, cycle_query=True)
        cq = ConditionalCycleQuery(crs=self.solver, list_request=[request],
                                   delay=0.01, request_timeout=0.5,
                                   error_policy=ErrorPolicy.SERVICE)
        deliveries = []

        async def on_msg(resps):
            deliveries.append(resps[0].value.v if resps[0].value else None)

        cq.add_callback_async_method(on_msg)
        cq.start()
        try:
            deadline = asyncio.get_event_loop().time() + 2.0
            while not deliveries and asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.01)
            self.assertEqual(deliveries, [42])
            self.assertNotIn('value_policy', request.request_data)
            self.provider.dead = True
            await asyncio.sleep(4 * self.TOLERANCE)
            # SERVICE retries NORMAL silently: no None, no extra delivery
            self.assertEqual(deliveries, [42])
        finally:
            await cq.stop_and_wait()


if __name__ == '__main__':
    unittest.main()
