import asyncio
import time
import unittest
from typing import List, Tuple

from obcom.data_colection.address import Address
from obsrv.data_colection.base_components.tree_component import ProvidesResponseProtocol
from obsrv.data_colection.base_components.tree_provider import TreeProvider
from obcom.data_colection.coded_error import TreeStructureError
from obsrv.data_colection.specialistic_components.tree_cache_observatory import TreeCache
from obcom.data_colection.value import Value
from obcom.data_colection.value_call import ValueRequest


class TreeCacheTest(unittest.IsolatedAsyncioTestCase):
    class SampleTestValueProvider(TreeProvider):
        RESPONSE_DELAY = 0

        async def get_value(self, request: ValueRequest, **kwargs) -> Value or None:
            address = request.address
            value_name = address[address.get_last_index()]
            self.count_current_tasks += 1
            self.count_tasks += 1
            await asyncio.sleep(self.response_delay)
            self.count_current_tasks -= 1
            for n, v in self.test_values:
                if value_name == n:
                    return v
            if value_name == 'error':
                raise TreeStructureError
            return None

        def __init__(self, component_name: str, source_name: str, test_values: List[Tuple[str, Value]],
                     subcontractor: ProvidesResponseProtocol = None, **kwargs):
            self.response_delay = self.RESPONSE_DELAY
            self.count_current_tasks = 0
            self.count_tasks = 0
            self.test_values: List[Tuple[str, Value]] = test_values
            super().__init__(component_name=component_name, source_name=source_name, subcontractor=subcontractor,
                             **kwargs)

    def setUp(self):
        super().setUp()
        self.v1 = ('val1', Value(1, 1661349399.030824))
        self.v2 = ('val2', Value(2, 1661349399.030824 - 152))

        self.tree_provider2 = self.SampleTestValueProvider('sample_name2', 'provider2', [self.v1, self.v2])
        self.tree_cache = TreeCache('sample_name_cache', self.tree_provider2)
        self.tree_provider1 = TreeProvider('sample_name1', 'provider1', self.tree_cache)

    def tearDown(self) -> None:
        super().tearDown()

    def test__value_meets_requirements(self):
        """
        Test method _value_meets_requirements()
        :return:
        """
        address = Address('.'.join(['sample_address', self.v1[0]]))
        kv = TreeCache._KnownValue(address, self.v1[1], None, change_time=0)

        expected_date = self.v1[1].ts + 10
        result = TreeCache._value_meets_requirements(kv, expected_date, 20)
        self.assertTrue(result)
        result = TreeCache._value_meets_requirements(kv, expected_date, 10)
        self.assertTrue(result)
        result = TreeCache._value_meets_requirements(kv, expected_date, 5)
        self.assertFalse(result)

    def test__update_known_value(self):
        """
        Test method _update_known_value()
        :return:
        """
        address = Address('.'.join(['sample_address', self.v1[0]]))
        tc = TreeCache('sample_name', None)

        # test add any value
        self.assertTrue(len(tc._known_values) == 0)
        asyncio.run(tc._update_known_value(address, self.v1[1]))
        self.assertTrue(len(tc._known_values) == 1)
        self.assertEqual(tc._known_values[0].value, self.v1[1])
        self.assertEqual(tc._known_values[0].address, address)

        # test add new value
        address2 = Address('.'.join(['sample_address', self.v2[0]]))
        asyncio.run(tc._update_known_value(address2, self.v2[1]))
        self.assertTrue(len(tc._known_values) == 2)

        # test change value to newer
        address2 = Address('.'.join(['sample_address', self.v2[0]]))
        v2 = self.v2[1].copy()
        v2.ts = v2.ts + 10
        v2.v = v2.v + 10
        asyncio.run(tc._update_known_value(address2, v2))

        self.assertTrue(len(tc._known_values) == 2)
        self.assertEqual(tc._known_values[1].value, v2)
        self.assertEqual(tc._known_values[1].address, address2)

        # test do not change value to older
        address2 = Address('.'.join(['sample_address', self.v2[0]]))
        v3 = self.v2[1].copy()
        v3.ts = v3.ts - 10
        v3.v = v3.v + 20
        asyncio.run(tc._update_known_value(address2, v3))

        self.assertTrue(len(tc._known_values) == 2)
        self.assertNotEqual(tc._known_values[1].value, v3)
        self.assertEqual(tc._known_values[1].address, address2)

    def test_flow_empty_cache(self):
        """
        Test of information flow through TreeCache block. Ask about any value when cache is empty.

        :return:
        """

        address = Address('.'.join([self.tree_provider1.get_source_name(), self.tree_provider2.get_source_name(),
                                    self.v1[0]]))
        request = ValueRequest(address, self.v1[1].ts)

        # ask about any value when cache is empty
        response = asyncio.run(self.tree_provider1.get_response(request))
        self.assertEqual(response.value, self.v1[1])

    def test_flow_get_from_cache(self):
        """
        Test of information flow through TreeCache block. Ask about any value when cache is empty.

        :return:
        """

        address = Address('.'.join([self.tree_provider1.get_source_name(), self.tree_provider2.get_source_name(),
                                    self.v1[0]]))
        request = ValueRequest(address, self.v1[1].ts)

        # get first time and save in cache
        asyncio.run(self.tree_provider1.get_response(request))

        # change value returned by provider but set the same timestamp
        new_value = self.tree_provider2.test_values[0][1].copy()  # chere change return value in lase component
        new_value.v += 20
        self.tree_provider2.test_values[0] = (self.tree_provider2.test_values[0][0], new_value)

        # make request again witch refresh index
        request = ValueRequest(address, self.v1[1].ts)

        # get value again, value should be provided by cache witch old value
        response = asyncio.run(self.tree_provider1.get_response(request))
        self.assertEqual(response.value.v, self.v1[1].v)

    def test_wrong_address_behind_cache(self):

        # change last provider
        self.tree_cache._subcontractor = TreeProvider('sample_name3', 'provider3', None)
        self.assertEqual(len(self.tree_cache._known_values), 0)

        address = Address('.'.join([self.tree_provider1.get_source_name(), 'no_existing_provider',
                                    self.v1[0]]))
        request = ValueRequest(address, self.v1[1].ts)
        # get first time and save in cache
        response = asyncio.run(self.tree_provider1.get_response(request))

        self.assertIsNone(response.value)  # no value return
        self.assertFalse(response.status)  # have some errors

        self.assertEqual(len(self.tree_cache._known_values), 1)
        k_val = self.tree_cache.get_k_val(address=address)
        self.assertIsNotNone(k_val)
        self.assertIsNone(k_val.value)
        self.assertEqual(k_val.change_time, 0)

    def test_flow_no_repeat_answer(self):
        """
        A test that checks if the cache has no value and several tasks ask for it at the same time, then only one task
        should ask the provider and the rest should wait and download from the cache.

        :return:
        """
        self.tree_provider2.response_delay = 1
        address = Address('.'.join([self.tree_provider1.get_source_name(), self.tree_provider2.get_source_name(),
                                    self.v1[0]]))
        nr_of_requests = 3

        # sample_list_request = [request for _ in range(nr_of_requests)]  # list witch 10 requests

        async def coro():
            request_tasks = [asyncio.create_task(self.tree_provider1.get_response(
                ValueRequest(address, self.v1[1].ts))) for _ in range(nr_of_requests)]
            time_start = time.time()

            await asyncio.sleep(0)
            self.assertTrue(self.tree_provider2.count_current_tasks == 1)
            for t in request_tasks:
                await t
            time_end = time.time()
            self.assertTrue(self.tree_provider2.count_tasks == 1)
            self.assertTrue(time_end - time_start < self.tree_provider2.response_delay * nr_of_requests)

        asyncio.run(coro())

    def test_no_save_put_request(self):
        """Test no save Put, EXECUTE or other request except GET"""
        address = Address('.'.join([self.tree_provider1.get_source_name(), self.tree_provider2.get_source_name(),
                                    self.v1[0]]))
        request = ValueRequest(address, self.v1[1].ts, request_type='PUT')

        asyncio.run(self.tree_provider1.get_response(request))

        self.assertFalse(len(self.tree_cache._known_values))

    def test_exceeding_number_of_recalls(self):
        """This test checks that the task correctly stops waiting for other tasks when they exceed number of recalls.
        By running e.g. 6 tasks and setting recall to 3 means that tasks from 0-3 should wait for others and the last
        two tasks number 4-5 should get bored with waiting and asking without waiting. Tasks 5-6 should not block
        value"""
        self.tree_provider2.response_delay = 1
        address = Address('.'.join([self.tree_provider1.get_source_name(), self.tree_provider2.get_source_name(),
                                    self.v1[0]]))
        nr_of_requests = 6
        # sample_list_request = [request for _ in range(nr_of_requests)]  # list witch 10 requests

        async def coro():
            self.tree_cache._max_recall = 3
            request_tasks = [asyncio.create_task(self.tree_provider1.get_response(
                ValueRequest(address, time_of_data=time.time() + 5,
                             time_of_data_tolerance=1))) for _ in range(nr_of_requests)]

            time_start = time.time()

            await asyncio.sleep(0)
            self.assertTrue(self.tree_provider2.count_current_tasks == 1)
            count_recalls = 0
            for t in request_tasks:
                await t
            time_end = time.time()
            self.assertTrue(self.tree_provider2.count_tasks == nr_of_requests)
            self.assertTrue(time_end - time_start < self.tree_provider2.response_delay * nr_of_requests)

        asyncio.run(coro())

    def test_address_regex_list(self):
        """Test list of no-cachable address witch specifics regex"""
        # excluded address
        address = Address('.'.join([self.tree_provider1.get_source_name(), self.tree_provider2.get_source_name(),
                                    "is_access"]))
        request = ValueRequest(address, self.v1[1].ts)
        self.assertFalse(self.tree_cache.is_cachable_request(request=request))

        # normal cachable address
        address = Address('.'.join([self.tree_provider1.get_source_name(), self.tree_provider2.get_source_name(),
                                    "is_access2"]))
        request = ValueRequest(address, self.v1[1].ts)
        self.assertTrue(self.tree_cache.is_cachable_request(request=request))
        # normal cachable address 2
        address = Address('.'.join([self.tree_provider1.get_source_name(), self.tree_provider2.get_source_name(),
                                    "_is_access"]))
        request = ValueRequest(address, self.v1[1].ts)
        self.assertTrue(self.tree_cache.is_cachable_request(request=request))


if __name__ == '__main__':
    unittest.main()
