import asyncio
import logging
import time
import unittest

from obcom.data_colection.address import Address
from obsrv.data_colection.base_components.tree_provider import TreeProvider
from obsrv.data_colection.specialistic_components.tree_cache_observatory import TreeCache
from obsrv.data_colection.specialistic_components.tree_conditional_freezer import TreeConditionalFreezer
from obcom.data_colection.value import Value
from obcom.data_colection.value_call import ValueRequest
from test.data_colection.sample_test_value_provider import SampleTestValueProvider

logger = logging.getLogger(__name__.rsplit('.')[-1])


class TestTreeConditionalFreezer(unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.v1, self.v2, *other = SampleTestValueProvider.SAMPLE_VALUES

        self.tree_provider2 = SampleTestValueProvider('sample_name2', 'provider2', [self.v1, self.v2])
        self.tree_cache = TreeCache('sample_name_cache', self.tree_provider2)
        self.tree_freezer = TreeConditionalFreezer('test_sample_freezer', self.tree_cache)
        self.tree_provider1 = TreeProvider('sample_name1', 'provider1', self.tree_freezer)
        self.max_refreshes = 3
        self.tree_freezer.set_max_refreshes(self.max_refreshes)
        self.time_interval = 0.2  # number of seconds to test the temporal aspects of the module
        self.tree_freezer._min_time_of_data_tolerance = self.time_interval  # maximum value refresh rate

    def tearDown(self) -> None:
        super().tearDown()

    async def _start_stop_tree(self, coro):
        try:
            await self.tree_provider1.run()
            return await coro
        finally:
            await self.tree_provider1.stop()

    def test_empty_cache_unable_to_cache_value(self):
        """
        Test of the situation when the cache is empty, and it is asked for a value that it cannot initialize because
        it is not cachable
        """
        address = Address('.'.join([self.tree_provider1.get_source_name(),
                                    self.tree_provider2.get_source_name(),
                                    'new_val']))
        time_of_data_tolerance = 1
        request = ValueRequest(address, time.time(),
                               time_of_data_tolerance=time_of_data_tolerance,
                               request_type='PUT',
                               request_data={'time_of_known_change': None},
                               cycle_query=True)

        response = asyncio.run(self._start_stop_tree(self.tree_provider1.get_response(request)))
        self.assertEqual(response.value, None)
        self.assertFalse(response.status)
        self.assertEqual(response.error.code, 4003)
        self.assertEqual(len(self.tree_cache._known_values), 0)
        self.assertEqual(self.tree_provider2.nr_requests, 0)  # There should be zero query because request is non
        # cacheable and should return error

    def test_empty_cache_able_to_cache_value(self):
        """
        Test of the situation when the cache is empty, and it will be asked for a value that it can initiate
        (value is cachable), but the provider does not return a value, but an error.
        """
        address = Address('.'.join([self.tree_provider1.get_source_name(),
                                    self.tree_provider2.get_source_name(),
                                    'value_error']))

        request = ValueRequest(address, time.time(),
                               time_of_data_tolerance=self.time_interval,
                               request_type='GET',
                               request_data={'time_of_known_change': None},
                               cycle_query=True)
        logger.warning('Time test started - may take a while to complete')
        start_time = time.time()
        response = asyncio.run(self._start_stop_tree(self.tree_provider1.get_response(request)))
        total_time = time.time() - start_time
        # Checks if the program does not send back the answer too quickly when the minimum time between successive
        # value updates is defined, minus 1 because the initial value is empty and wants to refresh it ASAP
        self.assertTrue(total_time > self.time_interval * (self.max_refreshes - 1))

        self.assertEqual(response.value, None)
        self.assertFalse(response.status)
        self.assertEqual(response.error.code, 2003)
        self.assertEqual(len(self.tree_cache._known_values), 1)

        self.assertEqual(self.tree_provider2.nr_requests, self.max_refreshes)

    def test_initialized_cache_unable_to_cache_value(self):
        """
        IMPORTANT TEST.\n
        Test the situation when the cache is initialized with a blank value and is requested for a value that it can
        be cached. This situation can occur when someone calls the address via GET request and then does it via PUT
        """
        address = Address('.'.join([self.tree_provider1.get_source_name(),
                                    self.tree_provider2.get_source_name(),
                                    'new_val']))

        request = ValueRequest(address, time.time(),
                               time_of_data_tolerance=self.time_interval,
                               request_type='PUT',
                               request_data={'time_of_known_change': None},
                               cycle_query=True)
        # initialize cache list witch empty value
        self.tree_cache._known_values.append(self.tree_cache._KnownValue(address=address, value=None,
                                                                         task=None, change_time=0))
        response = asyncio.run(self._start_stop_tree(self.tree_provider1.get_response(request)))

        self.assertEqual(response.value, None)
        self.assertFalse(response.status)
        self.assertEqual(response.error.code, 4003)
        self.assertEqual(len(self.tree_cache._known_values), 1)

        self.assertEqual(self.tree_provider2.nr_requests, 0)

    def test_initialized_cache_able_to_get_value(self):
        """Test the situation when the cache is initialized with a blank value and is asked for a value that it can
        initialize but provider does not return a value, but an error"""
        address = Address('.'.join([self.tree_provider1.get_source_name(),
                                    self.tree_provider2.get_source_name(),
                                    'new_val']))

        # set the 'raise_value_error' simulate situation when provider is offline and return error all time
        request = ValueRequest(address, time.time(),
                               time_of_data_tolerance=self.time_interval,
                               request_type='GET',
                               request_data={'time_of_known_change': None, 'raise_value_error': True},
                               cycle_query=True)
        # initialize cache list witch empty value
        self.tree_cache._known_values.append(self.tree_cache._KnownValue(address=address, value=None,
                                                                         task=None, change_time=0))

        logger.warning('Time test started - may take a while to complete')
        start_time = time.time()
        response = asyncio.run(self._start_stop_tree(self.tree_provider1.get_response(request)))
        total_time = time.time() - start_time
        # Checks if the program does not send back the answer too quickly when the minimum time between successive
        # value updates is defined, minus 1 because the initial value is empty and wants to refresh it ASAP
        self.assertTrue(total_time > self.time_interval * (self.max_refreshes - 1))

        self.assertEqual(response.value, None)
        self.assertFalse(response.status)
        self.assertEqual(len(self.tree_cache._known_values), 1)

        self.assertEqual(self.tree_provider2.nr_requests, self.max_refreshes)

    def test_value_not_change_before_timeout(self):
        """Test situation when client subscribe a value that does not change for a long time"""
        address = Address('.'.join([self.tree_provider1.get_source_name(),
                                    self.tree_provider2.get_source_name(),
                                    'static_val']))
        time_of_data_tolerance = self.time_interval
        current_time = time.time()
        time_interval_multiplier = 5
        request_timeout = self.time_interval * time_interval_multiplier + current_time
        timeout_offset_multiplier = 2
        freezer_alarm_timeout_offset = self.time_interval * timeout_offset_multiplier
        self.tree_freezer._alarm_timeout_offset = freezer_alarm_timeout_offset
        request = ValueRequest(address, time.time(),
                               time_of_data_tolerance=time_of_data_tolerance,
                               request_type='GET',
                               request_data={'time_of_known_change': current_time},
                               cycle_query=True,
                               request_timeout=request_timeout)
        # initialize cache list witch some value - this value will never change
        self.tree_cache._known_values.append(self.tree_cache._KnownValue(address=address,
                                                                         value=Value(v=self.tree_provider2.static_val,
                                                                                     ts=current_time),
                                                                         task=None, change_time=current_time))

        logger.warning('Time test started - may take a while to complete')
        start_time = time.time()
        response = asyncio.run(self._start_stop_tree(self.tree_provider1.get_response(request)))
        total_time = time.time() - start_time

        self.assertTrue(
            request_timeout - current_time - freezer_alarm_timeout_offset < total_time < request_timeout - current_time)

        self.assertEqual(response.value, None)
        self.assertFalse(response.status)
        self.assertEqual(response.error.code, 4004)
        self.assertEqual(len(self.tree_cache._known_values), 1)

        # IMPORTANT minus 1 because the last query will not fit in the time limit
        self.assertEqual(self.tree_provider2.nr_requests, time_interval_multiplier - timeout_offset_multiplier - 1)

    def test_value_change_again(self):
        """Test situation when client know about past changes and value changed again in cache before timeout"""
        address = Address('.'.join([self.tree_provider1.get_source_name(),
                                    self.tree_provider2.get_source_name(),
                                    'new_val']))
        current_time = time.time()

        request = ValueRequest(address, time.time(),
                               time_of_data_tolerance=self.time_interval,
                               request_type='GET',
                               request_data={'time_of_known_change': current_time},
                               cycle_query=True)
        # initialize cache list witch some value - here was set 6000 because provider can generate value from 0 to 1000,
        # so we make sure that the value will not be the same
        self.tree_cache._known_values.append(self.tree_cache._KnownValue(address=address,
                                                                         value=Value(v=6000, ts=current_time),
                                                                         task=None, change_time=current_time))

        response = asyncio.run(self._start_stop_tree(self.tree_provider1.get_response(request)))

        self.assertIsNotNone(response.value)
        self.assertNotEqual(response.value.v, 6000)
        self.assertTrue(response.status)
        self.assertEqual(response.error, None)
        self.assertEqual(len(self.tree_cache._known_values), 1)
        # Only 1 because value will be changed at first time
        self.assertEqual(self.tree_provider2.nr_requests, 1)

    def test_return_value_from_cache_immediately(self):
        """Test situation when client don't know about past changes and cache already contains a value that meets the
        requirements"""
        address = Address('.'.join([self.tree_provider1.get_source_name(),
                                    self.tree_provider2.get_source_name(),
                                    'new_val']))
        current_time = time.time()
        # request witch long time tolerance
        request = ValueRequest(address, time.time(),
                               time_of_data_tolerance=60,
                               request_type='GET',
                               request_data={'time_of_known_change': None},
                               cycle_query=True)
        # initialize cache list witch some value - here was set 6000 because provider can generate value from 0 to 1000,
        # so we make sure that the value will not be the same
        self.tree_cache._known_values.append(self.tree_cache._KnownValue(address=address,
                                                                         value=Value(v=6000, ts=current_time),
                                                                         task=None, change_time=current_time))
        logger.warning('Time test started - may take a while to complete')
        start_time = time.time()
        response = asyncio.run(self._start_stop_tree(self.tree_provider1.get_response(request)))
        total_time = time.time() - start_time
        # should be immediately
        self.assertTrue(total_time < 0.1)

        self.assertIsNotNone(response.value)
        self.assertEqual(response.value.v, 6000)  # we need exactly the value that is in the cache at the beginning
        self.assertTrue(response.status)
        self.assertEqual(response.error, None)
        self.assertEqual(len(self.tree_cache._known_values), 1)
        # Only 0 because value from cache meet requirements
        self.assertEqual(self.tree_provider2.nr_requests, 0)

    def test_return_value_from_cache_immediately_witch_refresh_cache(self):
        """Test situation when cache hasn't value and client don't know about past changes and ask about it"""
        address = Address('.'.join([self.tree_provider1.get_source_name(),
                                    self.tree_provider2.get_source_name(),
                                    'new_val']))
        # request witch long time tolerance
        request = ValueRequest(address, time.time(),
                               time_of_data_tolerance=60,
                               request_type='GET',
                               request_data={'time_of_known_change': None},
                               cycle_query=True)

        logger.warning('Time test started - may take a while to complete')
        start_time = time.time()
        response = asyncio.run(self._start_stop_tree(self.tree_provider1.get_response(request)))
        total_time = time.time() - start_time
        # should be immediately
        self.assertTrue(total_time < 0.1)

        self.assertIsNotNone(response.value)
        self.assertTrue(response.status)
        self.assertEqual(response.error, None)
        self.assertEqual(len(self.tree_cache._known_values), 1)
        # Only 0 because value from cache meet requirements
        self.assertEqual(self.tree_provider2.nr_requests, 1)

    def test_provider_crash_between_requests(self):
        """Test situation when provider suddenly stopped responding while subscribing. The cache still stores old
        values and client knows about the past, but no new data arrives to cache"""
        address = Address('.'.join([self.tree_provider1.get_source_name(),
                                    self.tree_provider2.get_source_name(),
                                    'new_val']))
        current_time = time.time()
        # request witch raising StructureError by end provider and witch information about past data
        request = ValueRequest(address, time.time(),
                               time_of_data_tolerance=self.time_interval,
                               request_type='GET',
                               request_data={'time_of_known_change': current_time, 'raise_structure_error': True},
                               cycle_query=True)
        # simulates that the cache has the old values
        self.tree_cache._known_values.append(self.tree_cache._KnownValue(address=address,
                                                                         value=Value(v=6000, ts=current_time),
                                                                         task=None, change_time=current_time))
        logger.warning('Time test started - may take a while to complete')
        start_time = time.time()
        response = asyncio.run(self._start_stop_tree(self.tree_provider1.get_response(request)))
        total_time = time.time() - start_time
        # should repeat the query self.max_refreshes times at intervals of self.time_interval wait because
        # the value is fresh but not too late
        self.assertTrue(
            self.time_interval * (self.max_refreshes + 1) > total_time > self.time_interval * self.max_refreshes)

        self.assertIsNone(response.value)

        self.assertFalse(response.status)
        self.assertIsNotNone(response.error)
        self.assertEqual(response.error.code, 2003)
        self.assertEqual(len(self.tree_cache._known_values), 1)
        # Only 0 because value from cache meet requirements
        self.assertEqual(self.tree_provider2.nr_requests, self.max_refreshes)

    def test_concurrent_requests_waking_up_by_fastest_client(self):
        """Test situation when it is two client asking about the same value but one of them ask faster and alvays was
        a new value. In this situation, the fastest client should update the value and the rest of the clients should
        be awakened by the condition. We assume that clients do not have a minimum time between requests set, but the
        value should always be reported immediately. The expected result should also be that everyone will finish at
        the same time."""
        address = Address('.'.join([self.tree_provider1.get_source_name(),
                                    self.tree_provider2.get_source_name(),
                                    'new_val']))
        current_time = time.time()
        time_of_data_tolerance = self.time_interval
        time_of_data_tolerance2 = time_of_data_tolerance / 2  # two times faster than first client
        client1_number_of_repetitions = 6
        client2_number_of_repetitions = 6
        self.tree_freezer._min_time_of_data_tolerance = time_of_data_tolerance2  # Allow the values to refresh faster
        request = ValueRequest(address, time.time(),
                               time_of_data_tolerance=time_of_data_tolerance,
                               request_type='GET',
                               request_data={'time_of_known_change': current_time},
                               cycle_query=True)
        request2 = ValueRequest(address, time.time(),
                                time_of_data_tolerance=time_of_data_tolerance2,
                                request_type='GET',
                                request_data={'time_of_known_change': current_time},
                                cycle_query=True)
        # simulates that the cache has the old values
        self.tree_cache._known_values.append(self.tree_cache._KnownValue(address=address,
                                                                         value=Value(v=6000, ts=current_time),
                                                                         task=None, change_time=current_time))

        async def simple_client(repetitions, req):
            response = None
            for i in range(repetitions):
                # repack msg
                r = req.copy()
                if response is not None:
                    self.assertIsNotNone(response.value)
                    r.request_data['time_of_known_change'] = response.value.ts
                response = await self.tree_provider1.get_response(r)  # send copies so as not to change
                # the original
            return response  # return the last value received

        async def coro():
            client1 = asyncio.create_task(simple_client(client1_number_of_repetitions, request))
            client2 = asyncio.create_task(simple_client(client2_number_of_repetitions, request2))
            result1 = await client1
            result2 = await client2
            return result1, result2

        logger.warning('Time test started - may take a while to complete')
        start_time = time.time()
        response1, response2 = asyncio.run(self._start_stop_tree(coro()))
        total_time = time.time() - start_time
        self.assertTrue(time_of_data_tolerance * (
            client1_number_of_repetitions) > total_time > time_of_data_tolerance2 * client2_number_of_repetitions)

        self.assertIsNotNone(response1.value)
        self.assertIsNotNone(response2.value)

        self.assertTrue(response1.status)
        self.assertTrue(response2.status)
        self.assertEqual(len(self.tree_cache._known_values), 1)
        # as many times as the fastest client
        self.assertEqual(self.tree_provider2.nr_requests, client2_number_of_repetitions)

    def test_async_react_for_condition(self):
        """test checks if the pending task will be woken up after the condition is reported and will not make
        additional attempts to update the value."""
        address = Address('.'.join([self.tree_provider1.get_source_name(),
                                    self.tree_provider2.get_source_name(),
                                    'new_val']))
        current_time = time.time()
        time_of_data_tolerance = self.time_interval * 6
        request = ValueRequest(address, time.time(),
                               time_of_data_tolerance=time_of_data_tolerance,
                               request_type='GET',
                               request_data={'time_of_known_change': current_time},
                               cycle_query=True)
        # simulates that the cache has the old values
        self.tree_cache._known_values.append(self.tree_cache._KnownValue(address=address,
                                                                         value=Value(v=6000, ts=current_time),
                                                                         task=None, change_time=current_time))

        async def simple_client(req):
            r = await self.tree_provider1.get_response(req)
            return r

        async def coro():
            client1 = asyncio.create_task(simple_client(request))
            await asyncio.sleep(time_of_data_tolerance / 12)  # wait for the task to set itself up to wait
            self.assertFalse(client1.done())  # make sure the job is still running
            await self.tree_cache._update_known_value(address=address, value=Value(v=8000, ts=time.time()))
            await asyncio.sleep(0)
            self.assertFalse(client1.done())  # make sure the job is still running
            result = await client1
            self.assertEqual(result.value.v, 8000)
            return result

        response = asyncio.run(self._start_stop_tree(coro()))
        self.assertIsNotNone(response.value)
        self.assertTrue(response.status)
        self.assertEqual(len(self.tree_cache._known_values), 1)


if __name__ == '__main__':
    unittest.main()
