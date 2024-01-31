import asyncio
import logging
import time
import unittest

from obcom.comunication.message_serializer import MessageSerializer
from obsrv.data_colection.base_components.tree_base_provider import TreeBaseProvider
from obsrv.data_colection.base_components.tree_component import ProvidesResponseProtocol
from obcom.data_colection.value import Value
from obcom.data_colection.value_call import ValueRequest
from obsrv.comunication.request_solver import RequestSolver
from test.data_colection.sample_test_value_provider import SampleTestValueProvider

logger = logging.getLogger(__name__.rsplit('.')[-1])


class RequestSolverTest(unittest.IsolatedAsyncioTestCase):
    class SampleTestValueProvider(TreeBaseProvider):

        RESPONSE_DELAY = 1

        async def get_value(self, request: ValueRequest, **kwargs) -> Value or None:
            await asyncio.sleep(self.RESPONSE_DELAY)
            return Value(34, time.time(), None)

        def __init__(self, component_name: str, subcontractor: ProvidesResponseProtocol = None,
                     **kwargs):
            super().__init__(component_name=component_name, subcontractor=subcontractor, **kwargs)

    async def test_get_single_answer_no_provider(self):
        """
        test of get answer when is no provider set
        """
        sample_request = {'address': {'adr': 'sample_telescope.any_val'}, 'time_of_data': 1655975599.883751,
                          'time_of_data_tolerance': 20.0}
        rs = RequestSolver(None)
        answer = await rs.get_single_answer(MessageSerializer.pack_b(sample_request), user_id=b'12345')
        answer = MessageSerializer.unpack_b(answer)
        self.assertEqual(answer.get('address'), sample_request.get('address'))
        self.assertIsNone(answer.get('value'))
        self.assertEqual(answer.get('error').get('code'), 4002)

    async def test_get_single_answer_corrupted_request(self):
        """
        test of get answer when request is damaged
        """
        # no address in request
        sample_request = {'wrong_address': {'adr': 'sample_telescope.any_val'}, 'time_of_data': 1655975599.883751,
                          'time_of_data_tolerance': 20.0}
        rs = RequestSolver(SampleTestValueProvider("xxx", "xxx", []))

        answer = await rs.get_single_answer(MessageSerializer.pack_b(sample_request), user_id=b'12345')
        answer = MessageSerializer.unpack_b(answer)
        self.assertEqual(answer.get('address').get('adr'), '')
        self.assertIsNone(answer.get('value'))
        self.assertEqual(answer.get('error').get('code'), 4001)

    async def test_get_single_answer(self):
        """
        test of get answer correct request
        """
        sample_request = {'address': {'adr': 'sample_telescope.any_val'}, 'time_of_data': 1655975599.883751,
                          'time_of_data_tolerance': 20.0}
        provider = self.SampleTestValueProvider('sample_provider')
        rs = RequestSolver(provider)

        answer = await rs.get_single_answer(MessageSerializer.pack_b(sample_request), user_id=b'12345')
        answer = MessageSerializer.unpack_b(answer)
        self.assertEqual(answer.get('address'), sample_request.get('address'))
        self.assertIsNotNone(answer.get('value'))
        self.assertIsNone(answer.get('error'))

    async def test_get_answer_parallel_operation(self):
        """
        test of parallel operation. If the request concerns several values at once, the method should divide them into
        tasks and run them in parallel.
        """
        sample_request = {'address': {'adr': 'sample_telescope.any_val'}, 'time_of_data': 1655975599.883751,
                          'time_of_data_tolerance': 20.0}
        provider = self.SampleTestValueProvider('sample_provider')
        rs = RequestSolver(provider)
        nr_of_requests = 10
        # list witch 10 requests
        sample_list_request = [MessageSerializer.pack_b(sample_request) for _ in range(nr_of_requests)]

        time_start = time.time()
        answer = await rs.get_answer(sample_list_request, user_id=b'12345')
        time_end = time.time()
        self.assertTrue(time_end - time_start < nr_of_requests * self.SampleTestValueProvider.RESPONSE_DELAY)
        for a in answer:
            a = MessageSerializer.unpack_b(a)
            self.assertEqual(a.get('address'), sample_request.get('address'))
            self.assertIsNotNone(a.get('value'))
            self.assertIsNone(a.get('error'))

    async def test_cancel_sub_task(self):
        """
        this test will check that when the main task which runs the get answer method is canceled then all subtasks
        will also be canceled.
        """
        sample_request = {'address': {'adr': 'sample_telescope.any_val'}, 'time_of_data': 1655975599.883751,
                          'time_of_data_tolerance': 20.0}
        provider = self.SampleTestValueProvider('sample_provider')
        rs = RequestSolver(provider)
        nr_of_requests = 10
        sample_list_request = [MessageSerializer.pack_b(sample_request) for _ in
                               range(nr_of_requests)]  # list witch 10 requests

        def count_tasks():
            count = 0
            all_tasks = asyncio.all_tasks(asyncio.get_running_loop())
            for _ in all_tasks:
                count += 1
            return count

        async def coro_test():
            await asyncio.sleep(0)
            task_on_start = count_tasks()
            c = asyncio.create_task(rs.get_answer(sample_list_request, user_id=b'12345'))
            await asyncio.sleep(0)
            self.assertEqual(count_tasks(), task_on_start + nr_of_requests + 1)
            c.cancel()
            await asyncio.sleep(0.1)  # here we need to wait some time because gather() closing subtasks and just
            # changing the focus is not enough
            self.assertEqual(count_tasks(), task_on_start)

        await coro_test()


if __name__ == '__main__':
    unittest.main()
