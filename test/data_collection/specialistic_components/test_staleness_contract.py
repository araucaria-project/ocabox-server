"""Staleness Contract, phase 2 (ocabox-common#8 / #40): freezer tolerance-clock
masking, rich stale-None delivery with stateless dedup, recovery-as-change in
TreeCache, and counter retirement for declared-policy requests.

Full component tree (provider -> TreeCache -> TreeConditionalFreezer -> root)
with a switchable provider, mirroring TestFreezerNegativeCacheInterplay.
"""

import asyncio
import time
import unittest

from obcom.data_colection.address import Address
from obcom.data_colection.coded_error import TreeOtherError
from obcom.data_colection.response_error import ResponseError
from obcom.data_colection.value import Value
from obcom.data_colection.value_call import ValueRequest
from obsrv.tree_components.base_components.tree_provider import TreeProvider
from obsrv.tree_components.specialized_components import TreeCache, TreeConditionalFreezer


class SwitchableProvider(TreeProvider):
    """Healthy: fresh Value(payload, now). Dead: raises 4005 with set severity."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dead = False
        self.fail_times = 0  # fail exactly N next calls (transient blip), then heal
        self.severity = ResponseError.SEVERITY_NORMAL
        self.payload = 42
        self.calls = 0

    async def get_value(self, request: ValueRequest, **kwargs) -> Value or None:
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise TreeOtherError(code=4005, message='transient blip', severity=self.severity)
        if self.dead:
            raise TreeOtherError(code=4005, message='device down', severity=self.severity)
        return Value(self.payload, time.time())


class StalenessContractTestBase(unittest.TestCase):
    TOLERANCE = 0.15          # T1 — healthy refresh cadence
    MAX_AGE = 2 * TOLERANCE   # T2 — default truth bound (2*T1)

    def setUp(self):
        super().setUp()
        self.provider = SwitchableProvider('switchable', 'provider2')
        self.cache = TreeCache('cache', self.provider)
        self.freezer = TreeConditionalFreezer('test_sample_freezer', self.cache)
        self.freezer.set_max_refreshes(2)
        self.freezer._min_time_of_data_tolerance = 0.05
        self.freezer._alarm_timeout_offset = 0.1
        self.root = TreeProvider('root', 'provider1', self.freezer)
        self.address = Address('provider1.provider2.reading')

    def make_request(self, *, tokc=None, value_policy=None, tolerance=None, max_age=None, timeout=0.9):
        request_data = {'time_of_known_change': tokc}
        if value_policy is not None:
            request_data['value_policy'] = value_policy
        return ValueRequest(self.address, time.time(),
                            time_of_data_tolerance=tolerance or self.TOLERANCE,
                            time_of_data_max_age=max_age,
                            request_timeout=time.time() + timeout,
                            request_data=request_data,
                            cycle_query=True)

    def run_scenario(self, coro):
        async def wrapped():
            try:
                await self.root.run()
                return await coro()
            finally:
                await self.root.stop()
        return asyncio.run(wrapped())


class TestStaleNoneDelivery(StalenessContractTestBase):

    def test_none_policy_delivers_rich_stale_none_once(self):
        async def scenario():
            first = await self.root.get_response(self.make_request(value_policy='none'))
            self.assertTrue(first.status)
            self.assertEqual(first.value.v, 42)
            good_ts = first.value.ts
            self.provider.dead = True
            await asyncio.sleep(self.MAX_AGE + 0.05)  # let the cached value break T2
            stale = await self.root.get_response(self.make_request(tokc=good_ts, value_policy='none'))
            # rich None as a value change
            self.assertTrue(stale.status)
            self.assertIsNone(stale.value.v)
            self.assertEqual(stale.value.tags['reason'], 4005)
            self.assertEqual(stale.value.tags['last_good'], 42)
            self.assertEqual(stale.value.tags['last_good_ts'], good_ts)
            self.assertIn('from_cf', stale.value.tags)
            # echoing the None's ts dedups: no repeat, subscription renews (4004)
            renew = await self.root.get_response(self.make_request(tokc=stale.value.ts,
                                                                   value_policy='none', timeout=0.6))
            self.assertFalse(renew.status)
            self.assertEqual(renew.error.code, 4004)
        self.run_scenario(lambda: scenario())

    def test_none_policy_masks_failures_inside_the_repair_window(self):
        """(T1, T2]: probing already happens (value older than T1) and fails,
        but the value is younger than T2 — failures stay invisible, even past
        max_unsuccessful_refreshes (counter retired). The Mirek case: prefer
        fresh, tolerate old-but-bounded, never a premature None."""
        async def scenario():
            first = await self.root.get_response(self.make_request(value_policy='none',
                                                                   tolerance=0.1, max_age=5.0))
            self.provider.dead = True
            await asyncio.sleep(0.15)  # past T1 (probes fail), well inside T2
            masked = await self.root.get_response(self.make_request(tokc=first.value.ts,
                                                                    value_policy='none',
                                                                    tolerance=0.1, max_age=5.0,
                                                                    timeout=0.6))
            self.assertFalse(masked.status)
            self.assertEqual(masked.error.code, 4004, 'within T2 the long-poll must just renew')
            # repair retries pace at (T2-T1)/4 ≈ 1.2s here — at least the T1
            # probe itself must have hit the device inside this request
            self.assertGreaterEqual(self.provider.calls, 2)
        self.run_scenario(lambda: scenario())

    def test_undeclared_requests_keep_the_counter_path(self):
        async def scenario():
            first = await self.root.get_response(self.make_request())
            self.provider.dead = True
            await asyncio.sleep(self.TOLERANCE + 0.05)
            resp = await self.root.get_response(self.make_request(tokc=first.value.ts))
            self.assertFalse(resp.status)
            self.assertEqual(resp.error.code, 2003, 'undeclared requests must fail via the historical counter')
        self.run_scenario(lambda: scenario())

    def test_recovery_after_stale_none_is_delivered_as_change(self):
        """First successful refresh after a failure episode must reach the
        client even when the payload is unchanged (unknown -> v is a change)."""
        async def scenario():
            first = await self.root.get_response(self.make_request(value_policy='none'))
            self.provider.dead = True
            await asyncio.sleep(self.MAX_AGE + 0.05)
            stale = await self.root.get_response(self.make_request(tokc=first.value.ts, value_policy='none'))
            self.assertIsNone(stale.value.v)
            self.provider.dead = False  # heals with the SAME payload (42)
            recovered = await self.root.get_response(self.make_request(tokc=stale.value.ts, value_policy='none'))
            self.assertTrue(recovered.status)
            self.assertEqual(recovered.value.v, 42)
        self.run_scenario(lambda: scenario())


class TestOtherPolicies(StalenessContractTestBase):

    def test_raise_policy_surfaces_error_at_tolerance(self):
        async def scenario():
            first = await self.root.get_response(self.make_request(value_policy='raise'))
            self.provider.dead = True
            await asyncio.sleep(self.MAX_AGE + 0.05)
            resp = await self.root.get_response(self.make_request(tokc=first.value.ts, value_policy='raise'))
            self.assertFalse(resp.status)
            self.assertEqual(resp.error.code, 2003)
        self.run_scenario(lambda: scenario())

    def test_raise_policy_is_max_age_driven_not_counter_driven(self):
        """With a huge counter budget the 2003 must still arrive (T2 clock),
        and inside the repair window it must NOT arrive (masked)."""
        self.freezer.set_max_refreshes(10_000)
        async def scenario():
            first = await self.root.get_response(self.make_request(value_policy='raise'))
            self.provider.dead = True
            masked = await self.root.get_response(self.make_request(tokc=first.value.ts,
                                                                    value_policy='raise',
                                                                    tolerance=0.1, max_age=5.0,
                                                                    timeout=0.6))
            self.assertEqual(masked.error.code, 4004)
            await asyncio.sleep(self.MAX_AGE + 0.05)
            resp = await self.root.get_response(self.make_request(tokc=first.value.ts, value_policy='raise'))
            self.assertEqual(resp.error.code, 2003)
        self.run_scenario(lambda: scenario())

    def test_last_good_policy_never_delivers_none_nor_error(self):
        async def scenario():
            first = await self.root.get_response(self.make_request(value_policy='last_good'))
            self.provider.dead = True
            await asyncio.sleep(self.MAX_AGE + 0.05)
            resp = await self.root.get_response(self.make_request(tokc=first.value.ts,
                                                                  value_policy='last_good', timeout=0.6))
            self.assertFalse(resp.status)
            self.assertEqual(resp.error.code, 4004, 'last_good masks forever: long-poll renews')
        self.run_scenario(lambda: scenario())

    def test_critical_failure_surfaces_immediately_despite_masking(self):
        async def scenario():
            first = await self.root.get_response(self.make_request(value_policy='none'))
            self.provider.dead = True
            self.provider.severity = ResponseError.SEVERITY_CRITICAL
            # tolerance freshly satisfied — masking would hide a NORMAL error,
            # but a permanent one must cut straight through
            await asyncio.sleep(self.TOLERANCE + 0.05)  # freezer probes after the value expires
            resp = await self.root.get_response(self.make_request(tokc=first.value.ts, value_policy='none'))
            self.assertFalse(resp.status)
            self.assertEqual(resp.error.code, 2003)
            self.assertEqual(resp.error.severity, ResponseError.SEVERITY_CRITICAL)
        self.run_scenario(lambda: scenario())


class TestRepairWindow(StalenessContractTestBase):
    """(T1, T2] is the repair window: probing runs at the healthy T1 cadence,
    failures are retried quickly inside the window — a transient blip is
    repaired while masking is still allowed, instead of becoming a stale-None."""

    def test_transient_blip_never_reaches_the_client(self):
        async def scenario():
            first = await self.root.get_response(self.make_request(value_policy='none'))
            self.provider.fail_times = 1  # exactly one failed probe, then healthy
            resp = await self.root.get_response(self.make_request(tokc=first.value.ts,
                                                                  value_policy='none'))
            self.assertTrue(resp.status)
            self.assertEqual(resp.value.v, 42, 'a repaired blip must deliver the value, never a None')
            self.assertGreaterEqual(self.provider.calls, 3, 'the failed probe must have been retried')
        self.run_scenario(lambda: scenario())

    def test_persistent_failure_gets_retries_before_the_none(self):
        async def scenario():
            first = await self.root.get_response(self.make_request(value_policy='none'))
            calls_before = self.provider.calls
            self.provider.dead = True
            resp = await self.root.get_response(self.make_request(tokc=first.value.ts,
                                                                  value_policy='none'))
            self.assertIsNone(resp.value.v)
            self.assertGreaterEqual(self.provider.calls - calls_before, 2,
                                    'at least one repair attempt must precede the stale-None')
        self.run_scenario(lambda: scenario())


class TestFreshSubscribeHonesty(StalenessContractTestBase):

    def test_stale_cache_is_not_served_to_declared_policy(self):
        """Initial subscribe (no time_of_known_change): an undeclared request
        gets whatever the cache holds (historical), a declared-none request
        gets a stale-None instead of a lie once the cache is older than T2."""
        async def scenario():
            first = await self.root.get_response(self.make_request())
            self.provider.dead = True
            await asyncio.sleep(self.MAX_AGE + 0.05)
            # undeclared: historical behaviour — the (stale) cached value
            legacy = await self.root.get_response(self.make_request())
            self.assertTrue(legacy.status)
            self.assertEqual(legacy.value.v, 42)
            # declared none: the lie is refused, refresh fails -> rich None
            honest = await self.root.get_response(self.make_request(value_policy='none'))
            self.assertTrue(honest.status)
            self.assertIsNone(honest.value.v)
            self.assertEqual(honest.value.tags['last_good'], 42)
            self.assertEqual(first.value.v, 42)
        self.run_scenario(lambda: scenario())


if __name__ == '__main__':
    unittest.main()
