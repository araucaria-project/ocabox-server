import asyncio
import logging
import time
import unittest
from obcom.comunication.comunication_error import CommunicationRuntimeError
from obcom.comunication.cycle_query import ConditionalCycleQuery
from obsrv.comunication.internal_client_request_solver import InternalClientRequestSolver
from obsrv.comunication.request_solver import RequestSolver
from obsrv.comunication.router import Router
from obcom.data_colection.address import AddressError, Address
from obsrv.data_colection.base_components.tree_provider import TreeProvider
from obsrv.data_colection.specialistic_components.tree_cache_observatory import TreeCache
from obsrv.data_colection.specialistic_components.tree_conditional_freezer import TreeConditionalFreezer
from obcom.data_colection.value import Value
from obcom.data_colection.value_call import ValueRequest, ValueResponse
from obsrv.util_functions.asyncio_util_functions import wait_for_psce
from test.data_colection.sample_test_value_provider import SampleTestValueProvider

logger = logging.getLogger(__name__.rsplit('.')[-1])
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)


class TestConditionalCycleQueryFlow(unittest.TestCase):

    def setUp(self):
        super().setUp()
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        # tree
        self.tree_provider2 = SampleTestValueProvider('sample_name2', 'provider2', [])
        self.tree_cache = TreeCache('sample_name_cache', self.tree_provider2)
        self.tree_freezer = TreeConditionalFreezer('sample_freezer', self.tree_cache)
        self.tree_provider1 = TreeProvider('sample_name1', 'provider1', self.tree_freezer)
        self.max_refreshes = 3
        self.tree_freezer._alarm_timeout_offset = 0.1  # Manual timeout alarm setting for tests
        self.tree_freezer.set_max_refreshes(self.max_refreshes)
        self.time_interval = 0.2  # number of seconds to test the temporal aspects of the module
        # router
        name = 'SampleTestRouter'
        self.rs = RequestSolver(self.tree_provider1)
        self.vr = Router(self.rs, name=name)
        self.vr.start(self.loop)
        self.loop.run_until_complete(self.vr.request_solver.run_tree())

        self.icrs = InternalClientRequestSolver(self.rs)

    def tearDown(self) -> None:
        self.vr.stop()
        stop_task = self.vr.get_stop_task()
        if stop_task:
            vr_stop = asyncio.gather(stop_task, self.vr.request_solver.stop_tree(), return_exceptions=True)
            self.loop.run_until_complete(vr_stop)
        try:
            all_tasks = asyncio.all_tasks(self.loop)
            for t in all_tasks:
                t.cancel()
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            all_tasks = asyncio.all_tasks(self.loop)
            if not all_tasks:
                logger.info('All task in loop is finished')
            else:
                logger.error('Some of the tasks in current loop is still running')
                raise RuntimeError
        finally:
            asyncio.set_event_loop(None)
            self.loop.close()
        self.vr.__del__()
        super().tearDown()

    @staticmethod
    def multipart_to_V_response(resp):
        try:
            vr = ValueResponse.from_byte(resp)
        except TypeError:
            vr = ValueResponse('', None, False, None)
        except AddressError:
            vr = ValueResponse('', None, False, None)
        except ValueError:
            vr = ValueResponse('', None, False, None)
        return vr

    def test_get_no_cachable_value(self):
        address = Address('.'.join([self.tree_provider1.get_source_name(),
                                    self.tree_provider2.get_source_name(),
                                    'new_val']))
        time_of_data_tolerance = self.time_interval

        request = ValueRequest(address, time.time(),
                               time_of_data_tolerance=time_of_data_tolerance,
                               request_type='PUT',
                               request_data={'time_of_known_change': None},
                               cycle_query=True)
        delay = 0.5

        async def coro():
            cq = ConditionalCycleQuery(crs=self.icrs, list_request=[request], delay=delay)
            try:
                cq.start()
                with self.assertRaises(CommunicationRuntimeError):
                    result = await cq.get_response()
                self.assertTrue(cq.is_stopped())
            finally:
                cq.stop()

        self.loop.run_until_complete(coro())

    # def test_router_not_response(self):
    #     address = Address('.'.join([self.tree_provider1.get_source_name(),
    #                                 self.tree_provider2.get_source_name(),
    #                                 'new_val']))
    #     time_of_data_tolerance = self.time_interval
    #
    #     request = ValueRequest(address, time.time(),
    #                            time_of_data_tolerance=time_of_data_tolerance,
    #                            request_type='GET',
    #                            request_data={'time_of_known_change': None},
    #                            cycle_query=True)
    #     delay = 0.2
    #     max_missed_msg = 5
    #     request_timeout = 0.2
    #
    #     async def coro():
    #         client = Client(name='SampleTestClient')
    #         crs = ClientRequestSolver(client=client)
    #         cq = ConditionalCycleQuery(crs=crs, list_request=[request], delay=delay,
    #                                    max_missed_msg=max_missed_msg, request_timeout=request_timeout)
    #         try:
    #             self.vr.stop()
    #             await self.vr.wait_for_stop()  # stop the router
    #             start_time = time.time()
    #             cq.start()
    #             with self.assertRaises(CommunicationRuntimeError):
    #                 result = await cq.get_response()
    #             end_time = time.time()
    #             self.assertTrue(
    #                 (max_missed_msg + 1) * request_timeout > end_time - start_time > max_missed_msg * request_timeout)
    #             self.assertTrue(cq.is_stopped())
    #         finally:
    #             cq.stop()
    #
    #     self.loop.run_until_complete(coro())

    def test_two_clients_at_the_same_time(self):
        """This test checks if the slow task will be in sync with the fast task. A slow task should wait for the
        moment to update the value in the cache, but the fast task should overtake it and both tasks should return
        the same value to the client at once."""
        address = Address('.'.join([self.tree_provider1.get_source_name(),
                                    self.tree_provider2.get_source_name(),
                                    'new_val']))
        time_of_data_tolerance = self.time_interval
        time_of_data_tolerance2 = self.time_interval * 5
        current_time = time.time()
        request = ValueRequest(address, time.time(),
                               time_of_data_tolerance=time_of_data_tolerance,
                               request_type='GET',
                               request_data={'time_of_known_change': current_time},  # client known about past
                               cycle_query=True)
        request2 = request.copy()
        request2.time_of_data_tolerance = time_of_data_tolerance2
        # initialize cache list witch some value
        self.tree_cache._known_values.append(self.tree_cache._KnownValue(address=address,
                                                                         value=Value(v=6000, ts=current_time),
                                                                         task=None, change_time=current_time))
        delay = 0.3

        async def coro():
            result = await self.icrs.send_request(requests=[request.copy()], timeout=time.time() + 2,
                                                  no_wait=False)
            cq = ConditionalCycleQuery(crs=self.icrs, list_request=[request], delay=delay, query_name='fast query')
            cq2 = ConditionalCycleQuery(crs=self.icrs, list_request=[request2], delay=delay,
                                        query_name='slow query')
            cq._last_response = result
            cq2._last_response = result
            try:
                start_time = time.time()
                cq2.start()
                cq.start()
                result1, result2 = await asyncio.gather(cq2.get_response(), cq.get_response())

                end_time = time.time()
                self.assertIsNotNone(result1[0].value)
                self.assertIsNotNone(result2[0].value)
                self.assertEqual(result1[0].value.v, result2[0].value.v)
                self.assertTrue(time_of_data_tolerance < end_time - start_time < time_of_data_tolerance2)
            finally:
                await cq2.stop_and_wait()
                await cq.stop_and_wait()

        self.loop.run_until_complete(coro())

    def test_correct_renewing_message(self):
        """This test checks correct renewing message after it expire in server."""
        address = Address('.'.join([self.tree_provider1.get_source_name(),
                                    self.tree_provider2.get_source_name(),
                                    self.tree_provider2.SAMPLE_VALUES[0][0]]))
        time_of_data_tolerance = 0.2
        time_expire = 0.5
        current_time = time.time()
        request = ValueRequest(address, current_time,
                               time_of_data_tolerance=time_of_data_tolerance,
                               request_type='GET',
                               request_data={'time_of_known_change': current_time},  # client known about past
                               cycle_query=True)
        self.tree_freezer._alarm_timeout_offset = time_expire / 2  # set short time to expire
        # initialize cache list witch some value
        self.tree_cache._known_values.append(self.tree_cache._KnownValue(address=address,
                                                                         value=Value(v=1, ts=current_time),
                                                                         task=None, change_time=current_time))
        delay = 0.2

        async def val_changer():
            await asyncio.sleep(time_expire)
            self.tree_provider2.SAMPLE_VALUES[0][1].v += 1
            self.tree_provider2.SAMPLE_VALUES[0][1].ts = time.time()

        async def coro():
            cq = ConditionalCycleQuery(crs=self.icrs, list_request=[request], delay=delay, query_name='sample',
                                       request_timeout=time_expire)  # set short timeout message
            try:
                cq.start()
                asyncio.create_task(val_changer())
                result = await cq.get_response()
                self.assertIsNotNone(result[0].value)
                self.assertTrue(result[0].status)
            finally:
                await cq.stop_and_wait()

        self.loop.run_until_complete(coro())

    def test_renewing_subscription_with_error(self):
        """Test the renewal subscription when the server fails to update the value due to an error and reaches a
        timeout before it exceeds the number of re-refreshes. After renewing the subscription, it should continue
        retrying the number with which it ended"""
        address = Address('.'.join([self.tree_provider1.get_source_name(),
                                    self.tree_provider2.get_source_name(),
                                    'new_val']))
        time_of_data_tolerance = self.time_interval

        request = ValueRequest(address, time.time(),
                               time_of_data_tolerance=time_of_data_tolerance,
                               request_type='GET',
                               request_data={'time_of_known_change': None},
                               cycle_query=True)
        delay = 0.2
        max_missed_msg = 7
        request_timeout = 0.2

        async def time_coro():
            cq = ConditionalCycleQuery(crs=self.icrs, list_request=[request], delay=delay,
                                       max_missed_msg=max_missed_msg, request_timeout=request_timeout)
            try:
                cq.start()
                request.request_data['raise_value_error'] = True
                with self.assertRaises(CommunicationRuntimeError):
                    result = await cq.get_response()
            finally:
                cq.stop()

        async def coro():
            # if there is a TimeoutError here, it means that the freezer is blocked indefinitely because parameters
            # are not sent back to it
            await wait_for_psce(time_coro(), 2)

        self.loop.run_until_complete(coro())

    def test_renewing_if_temporary_error(self):
        """Test reeving subscription when server return temporary errors"""
        address = Address('.'.join([self.tree_provider1.get_source_name(),
                                    self.tree_provider2.get_source_name(),
                                    'new_val']))
        time_of_data_tolerance = self.time_interval

        request = ValueRequest(address, time.time(),
                               time_of_data_tolerance=time_of_data_tolerance,
                               request_type='GET',
                               request_data={'time_of_known_change': None},
                               cycle_query=True)
        delay = 0.2
        max_missed_msg = 5
        request_timeout = 0.2

        async def time_coro(cq):
            result = await cq.get_response()

        async def coro():
            cq = ConditionalCycleQuery(crs=self.icrs, list_request=[request], delay=delay,
                                       max_missed_msg=max_missed_msg, request_timeout=request_timeout)
            try:
                cq.start()
                await asyncio.sleep(0.2)
                result = await cq.get_response()
                request.request_data['raise_value_error_temporary'] = True
                with self.assertRaises(asyncio.TimeoutError):
                    result = await wait_for_psce(time_coro(cq), 0.5)
                await asyncio.sleep(0.1)  # wait to make sure cycle query doesn't close
                self.assertFalse(cq.is_stopped())
            finally:
                cq.stop()

        self.loop.run_until_complete(coro())

    def test_last_callback_after_error(self):
        """Test do callback last time after retrieve error from server"""
        address = Address('.'.join([self.tree_provider1.get_source_name(),
                                    self.tree_provider2.get_source_name(),
                                    'new_val']))
        time_of_data_tolerance = self.time_interval

        request = ValueRequest(address, time.time(),
                               time_of_data_tolerance=time_of_data_tolerance,
                               request_type='GET',
                               request_data={'time_of_known_change': None},
                               cycle_query=True)
        delay = 0.2
        max_missed_msg = 7
        request_timeout = 0.2
        checker = {'a': 0}  # musi być słownikiem, żeby przekazywać object, a nie int

        async def callback(response):
            if response[0].status is False:
                checker['a'] = checker['a'] + 1

        async def time_coro():
            cq = ConditionalCycleQuery(crs=self.icrs, list_request=[request], delay=delay,
                                       max_missed_msg=max_missed_msg, request_timeout=request_timeout)
            try:
                cq.add_callback_async_method(callback)
                cq.start()
                await asyncio.sleep(0.5)
                request.request_data['raise_value_error'] = True
                await asyncio.sleep(0.5)
            finally:
                await cq.stop_and_wait()

        async def coro():
            # if there is a TimeoutError here, it means that the freezer is blocked indefinitely because parameters
            # are not sent back to it
            await wait_for_psce(time_coro(), 2)

        self.loop.run_until_complete(coro())
        self.assertEqual(1, checker.get('a'))


if __name__ == '__main__':
    unittest.main()
