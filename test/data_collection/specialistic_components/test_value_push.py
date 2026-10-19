"""Values pushed into the cache reach waiting cycle-query subscribers at once.

A provider that learns values without being asked (the NATS service driver) stores them with
``TreeCache.push_value``. Waking the freezer alone would not do: it would re-read an unchanged cache
entry and sleep until ``ts + time_of_data_tolerance``. This pins the whole path freezer → cache →
push, plus the ordering and wiring rules.
"""
import asyncio
import time
import unittest

from obcom.data_colection.address import Address
from obcom.data_colection.value import Value
from obcom.data_colection.value_call import ValueRequest

from obsrv.tree_components.base_components.tree_base_broker_default_target import TreeBaseBrokerDefaultTarget
from obsrv.tree_components.base_components.tree_provider import TreeProvider
from obsrv.tree_components.specialized_components import TreeCache, TreeConditionalFreezer
from obsrv.utils.push_wiring import assert_push_wiring

ADDRESS = 'tel.optics.camera.sees'


class _Pusher(TreeProvider):
    """Answers a pull with what it holds — the NATS driver's mirror, reduced to one value."""
    VALUE_PUSHING = True

    def __init__(self):
        super().__init__('pusher', 'optics')
        self.held = 'dark'

    async def get_value(self, request: ValueRequest, **kwargs) -> Value:
        return Value(self.held, time.time())


class ValuePushTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.pusher = _Pusher()
        self.cache = TreeCache('cache', self.pusher)
        self.freezer = TreeConditionalFreezer('freezer', self.cache)
        self.root = TreeProvider('root', 'tel', self.freezer)
        self.pusher.set_value_sink(self.cache.push_value)
        await self.root.run()

    async def asyncTearDown(self):
        await self.root.stop()

    def _cycle_query(self, time_of_known_change):
        return ValueRequest(Address(ADDRESS), time.time(), time_of_data_tolerance=30,
                            request_type='GET', request_data={'time_of_known_change': time_of_known_change},
                            request_timeout=time.time() + 60, cycle_query=True)

    async def _push(self, v):
        self.pusher.held = v
        await self.pusher._value_sink(Address(ADDRESS), Value(v, time.time()))

    async def test_waiting_subscriber_gets_pushed_value_within_a_tick(self):
        first = await self.root.get_response(self._cycle_query(None))
        self.assertEqual(first.value.v, 'dark')
        known = self.cache.get_k_val(Address(ADDRESS)).get_change_time()

        waiting = asyncio.create_task(self.root.get_response(self._cycle_query(known)))
        await asyncio.sleep(0.05)
        self.assertFalse(waiting.done(), 'nothing changed yet: the subscriber must still wait')

        started = time.monotonic()
        await self._push('sky.science')
        answer = await asyncio.wait_for(waiting, timeout=2)
        self.assertEqual(answer.value.v, 'sky.science')
        self.assertLess(time.monotonic() - started, 0.5, 'delivery must not wait for time_of_data_tolerance (30 s)')

    async def test_older_or_equal_value_never_rolls_state_back(self):
        await self.root.get_response(self._cycle_query(None))
        newer = Value('sky.science', time.time() + 1)
        await self.cache.push_value(Address(ADDRESS), newer)
        await self.cache.push_value(Address(ADDRESS), Value('dark', newer.ts - 0.5))
        await self.cache.push_value(Address(ADDRESS), Value('dark', newer.ts))
        self.assertEqual(self.cache.get_k_val(Address(ADDRESS)).get_value().v, 'sky.science')

    async def test_unchanged_keepalive_refreshes_ts_without_waking(self):
        await self.root.get_response(self._cycle_query(None))
        kv = self.cache.get_k_val(Address(ADDRESS))
        change_time = kv.get_change_time()
        await self._push('dark')
        self.assertEqual(kv.get_change_time(), change_time)
        self.assertGreater(kv.get_timestamp(), change_time)


class ValuePushWiringTest(unittest.TestCase):

    def test_unwired_value_pushing_provider_fails_also_behind_a_broker_default(self):
        pusher = _Pusher()
        broker = TreeBaseBrokerDefaultTarget('broker', [], default_provider=pusher)
        with self.assertRaises(AssertionError) as ctx:
            assert_push_wiring(broker)
        self.assertIn('set_value_sink', str(ctx.exception))

    def test_wired_value_pushing_provider_passes(self):
        pusher = _Pusher()
        pusher.set_value_sink(TreeCache('cache', pusher).push_value)
        assert_push_wiring(TreeBaseBrokerDefaultTarget('broker', [pusher]))


if __name__ == '__main__':
    unittest.main()
