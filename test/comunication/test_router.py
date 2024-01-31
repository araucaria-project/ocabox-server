import asyncio
import logging
import time
import unittest
from typing import List

from obcom.comunication.message_serializer import MessageSerializer
from obcom.comunication.multipart_structure import MultipartStructure
from obsrv.ob_config import SingletonConfig
from obsrv.comunication.base_request_solver import BaseRequestSolver
from obsrv.comunication.router import Router
from obsrv.util_functions.asyncio_util_functions import wait_for_psce
from test.comunication.sample_test_resolver import SampleTestResolver
from test.data_colection.sample_test_value_provider import SampleTestValueProvider

logger = logging.getLogger(__name__.rsplit('.')[-1])


class RouterTest(unittest.TestCase):
    class CrashResolver(BaseRequestSolver):

        async def get_single_answer(self, request: bytes, user_id: bytes, timeout=None) -> bytes:
            pass

        async def get_answer(self, request: List[bytes], user_id: bytes, timeout=None) -> List[bytes]:
            await asyncio.sleep(5)
            return [b'']

    SAMPLE_MESSAGE = [b'\x00\x80\x00A\xa7',
                      b'',
                      b'123456789',
                      b'0',
                      b'123456789',
                      b'\xc2',
                      b'',
                      b'\x84\xa7address\x81\xa3adr\xb8sample_telescope.any_val\xactime_of_data\xcbA\xd8\xb8\xab7w\xc1a'
                      b'\xb6time_of_data_tolerance\xcb@N\x00\x00\x00\x00\x00\x00\xafrequest_timeout\xcbA\xd8\xb8\xab'
                      b'>\xf6]\xee']

    def setUp(self):
        super().setUp()
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self) -> None:
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
        super().tearDown()

    def test_routers_name_conflict(self):
        """
        the test checks if it is not possible to loop two routers with the same name

        :return:
        """
        name1 = 'router1'
        name2 = 'router1'
        vr = Router(None, name=name1, port=5559)
        vr2 = Router(None, name=name2, port=5558)

        async def coro():
            vr.start()
            await asyncio.sleep(0)
            vr2.start()
            # don't care about stop router because in unit test tearDown() method close all task and safety end loop

        with self.assertRaises(ValueError):
            self.loop.run_until_complete(coro())

    def test_stop_router(self):
        """
        This test check, if method stop() correctly close all router task

        :return:
        """
        name1 = 'SampleTestRouter'
        vr = Router(None, name=name1)

        async def coro():
            vr.start()
            await asyncio.sleep(0)
            vr.stop()
            await asyncio.sleep(0)

        self.loop.run_until_complete(coro())
        self.assertTrue(vr.is_stopped())
        all_tasks = asyncio.all_tasks(self.loop)
        self.assertTrue(len(all_tasks) == 0)

    def test_router_is_stopped(self):
        """
        This test check if .is_stopped() method work good
        """
        name1 = 'SampleTestRouter'
        vr = Router(None, name=name1, port=5559)

        async def coro():
            vr.start()
            await asyncio.sleep(0)
            return vr.is_stopped()

        out = self.loop.run_until_complete(coro())
        self.assertFalse(out)
        self.assertFalse(self.loop.is_running())
        # the loop is stop but don't close so the router should be not stop just freeze
        self.assertFalse(vr.is_stopped())
        # force close router
        all_tasks = asyncio.all_tasks(self.loop)
        for t in all_tasks:
            t.cancel()
        self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        # now router should be stopped
        self.assertTrue(vr.is_stopped())
        vr._front_socket.close()  # close socket to unlock for other tests

    def test_router_stop_task(self):
        """
        This test check if stop task for router work correct

        :return:
        """
        name1 = 'SampleTestRouter'
        vr = Router(None, name=name1, port=5559)

        async def coro():
            vr.start()
            await asyncio.sleep(0)

        self.loop.run_until_complete(coro())
        vr.stop()
        stop_task = vr.get_stop_task()
        self.assertIsNotNone(stop_task)  # here stop task was create but not run, so it must exist and can't finish

        # here should be existing some task in loop because stop task don't finished yet
        all_tasks = asyncio.all_tasks(self.loop)
        self.assertFalse(len(all_tasks) == 0)

        async def coro2():
            # it should be finished immediately if it's waiting that mean is something wrong
            await wait_for_psce(stop_task, timeout=1.0)

        self.loop.run_until_complete(coro2())
        stop_task = vr.get_stop_task()
        self.assertIsNone(stop_task)  # here stop task finished, so it can't exist in router

        #  now shouldn't be any task in loop
        all_tasks = asyncio.all_tasks(self.loop)
        self.assertTrue(len(all_tasks) == 0)

    def test__get_cfg(self):
        """
        test method _get_cfg()
        """
        name = 'SampleTestRouter'
        vr = Router(None, name=name)
        # try to get some value from config
        port = vr._get_cfg('port')
        self.assertEqual(port, SingletonConfig.get_config()['router'][name]['port'].get())
        # try to get no existing value from config
        port = vr._get_cfg('port_not_existing')
        self.assertIsNone(port)
        # try to get no existing value from config witch default value
        default = 'sample'
        port = vr._get_cfg('port_not_existing', default)
        self.assertEqual(port, default)

    def test__send_back_canceled(self):
        """
        Test task that returns the answer has been canceled. Checks if it will be correctly removed from the active
        tasks list.
        """

        name1 = 'SampleTestRouter'
        vr = Router(self.CrashResolver(SampleTestValueProvider("xxx", "xxx", [])), name=name1, port=5559)

        self.assertTrue(len(asyncio.all_tasks(self.loop)) == 0)
        self.assertTrue(len(vr._message_tasks) == 0)

        async def primitive_router_main_coro():
            task = asyncio.create_task(vr._send_back(self.SAMPLE_MESSAGE), name=vr._message_task_name)
            vr._message_tasks.append(task)
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)  # !!! need 3 times to change focus for cancel all sub-task
            await asyncio.sleep(0)  # !!! need 3 times to change focus for cancel all sub-task
            await asyncio.sleep(0)  # !!! need 3 times to change focus for cancel all sub-task

        self.loop.run_until_complete(primitive_router_main_coro())
        self.assertTrue(len(vr._message_tasks) == 0)
        self.assertTrue(len(asyncio.all_tasks(self.loop)) == 0)

    # def test_timeout_task(self):
    #     """
    #     Test task that returns the answer has timeout.
    #     """
    #     name1 = 'SampleTestRouter'
    #     vr = Router(self.CrashResolver(SampleTestValueProvider("xxx", "xxx", [])), name=name1)
    #     vr.default_timeout = 1
    #
    #     async def primitive_router_main_coro():
    #         task = asyncio.create_task(vr._send_back(self.SAMPLE_MESSAGE), name=vr._message_task_name)
    #         vr._message_tasks.append(task)
    #         await asyncio.sleep(0)
    #         result = await task
    #         self.assertIsNone(result)
    #
    #     self.loop.run_until_complete(primitive_router_main_coro())

    def test__open_envelope(self):
        """
        Test metchod _open_envelope()
        """
        sample_envelope = self.SAMPLE_MESSAGE
        name1 = 'SampleTestRouter'
        vr = Router(self.CrashResolver(SampleTestValueProvider("xxx", "xxx", [])), name=name1, port=5559)

        ms = vr._open_envelope(sample_envelope)
        prefix_size = 1
        self.assertEqual(ms.prefix_data, [sample_envelope[prefix_size - 1]])
        self.assertEqual(ms.create_time, sample_envelope[MultipartStructure.CREATE_TIME + prefix_size])
        self.assertEqual(ms.id_, sample_envelope[MultipartStructure.ID_ + prefix_size])
        self.assertEqual(ms.data, [sample_envelope[MultipartStructure.DATA + prefix_size]])

        # no create data in envelope
        sample_wrong_envelope = sample_envelope.copy()
        sample_wrong_envelope[MultipartStructure.CREATE_TIME + prefix_size] = b''
        with self.assertRaises(ValueError):
            ms = vr._open_envelope(sample_wrong_envelope)

        # no id in envelope
        sample_wrong_envelope = sample_envelope.copy()
        sample_wrong_envelope[MultipartStructure.ID_ + prefix_size] = b''
        with self.assertRaises(ValueError):
            ms = vr._open_envelope(sample_wrong_envelope)

        # no id in envelope
        sample_wrong_envelope = sample_envelope.copy()
        sample_wrong_envelope[MultipartStructure.REQUEST_TIMEOUT + prefix_size] = b''
        with self.assertRaises(ValueError):
            ms = vr._open_envelope(sample_wrong_envelope)

    def test_task_timeout_from_ordered_message(self):
        """
        Test task get timeout from ordered message.
        """
        import zmq
        from zmq.asyncio import Context
        response_delay = 0.3
        resolver = SampleTestResolver(SampleTestValueProvider("xxx", "xxx", []))
        resolver.response_delay = response_delay

        name1 = 'SampleTestRouter'
        vr = Router(resolver, name=name1)
        vr.default_timeout = 1

        async def receive(time_to_expire):
            with Context() as context:
                with context.socket(zmq.DEALER) as socket:
                    # IMPORTANT Pending messages shall be discarded immediately when the socket is closed
                    socket.setsockopt(zmq.LINGER, 0)
                    socket.connect(f"tcp://localhost:{vr._port}")
                    prefix_size = 0
                    sample_envelope = self.SAMPLE_MESSAGE.copy()[1:]
                    sample_envelope[MultipartStructure.REQUEST_TIMEOUT + prefix_size] = MessageSerializer.pack_b(time.time() + time_to_expire)
                    await socket.send_multipart(sample_envelope)
                    message = await socket.recv_multipart()
                    return message

        async def primitive_coro():
            vr.start(self.loop)
            try:
                with self.assertRaises(asyncio.exceptions.TimeoutError):
                    result = await wait_for_psce(receive(time_to_expire=response_delay/2), timeout=1)
                result = await wait_for_psce(receive(time_to_expire=response_delay*2), timeout=1)
                self.assertIsNotNone(result)
            finally:
                vr.stop()

        self.loop.run_until_complete(primitive_coro())


if __name__ == '__main__':
    unittest.main()
