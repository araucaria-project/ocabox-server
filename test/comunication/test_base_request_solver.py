import asyncio
import logging
import unittest
import datetime
from typing import List

from serverish.base import StatusEnum, dt_utcnow_array, dt_ensure_datetime
from serverish.messenger import get_reader

from obsrv.comunication.base_request_solver import BaseRequestSolver
from obsrv.comunication.nats_streams import NatsStreams
from obsrv.comunication.request_solver import RequestSolver
from obsrv.data_colection.base_components.tree_base_broker_default_target import TreeBaseBrokerDefaultTarget
from obsrv.data_colection.specialistic_components.tree_alpaca import TreeAlpacaObservatory
from obsrv.ob_config import SingletonConfig
from obsrv.util_functions.asyncio_util_functions import wait_for_psce
from test.data_colection.sample_test_value_provider import SampleTestValueProvider

logger = logging.getLogger(__name__.rsplit('.')[-1])


class RequestSolverTest(unittest.IsolatedAsyncioTestCase):

    class SampleRequestSolver(BaseRequestSolver):
        async def get_answer(self, request: List[bytes], user_id: bytes, timeout=None) -> List[bytes]:
            pass

        async def get_single_answer(self, request: bytes, user_id: bytes, timeout=None) -> bytes:
            pass

    def setUp(self):
        super().setUp()
        self.provider = SampleTestValueProvider("provider", "provider", [])
        self.RS = self.SampleRequestSolver(data_provider=self.provider)
        self.RS._nats_port = 4222
        self.RS._nats_host = "localhost"

    def test_get_config_alpaca(self):
        """test checks if the method `_get_alpaca_modules_configuration()` correctly returns the configuration
        of the observatories"""
        OBSERVATORY_NAME = 'test_observatory'
        tao = TreeAlpacaObservatory(component_name='jk98', observatory_name=OBSERVATORY_NAME)
        broker = TreeBaseBrokerDefaultTarget(component_name="broker", list_providers=[], default_provider=tao)
        rs = RequestSolver(broker)
        cfg = rs._get_alpaca_modules_configuration()
        configuration = SingletonConfig.get_config()
        self.assertDictEqual(cfg, {OBSERVATORY_NAME: configuration["tree"][OBSERVATORY_NAME].get()})

    async def test_connect_to_nats_failed(self):
        """test connect to nats failed"""

        self.RS._nats_port = 7452  # set wrong port
        with self.assertRaises(asyncio.TimeoutError):
            await wait_for_psce(self.RS.run_tree(), 1.5)
        self.assertEqual(self.RS._tree_data.nats_messenger.conn.status.get("nats_server"), StatusEnum.fail)
        await wait_for_psce(self.RS.stop_tree(), 1.5)

    async def test_connect_to_nats(self):
        """test connect to nats"""
        await wait_for_psce(self.RS.run_tree(), 1.5)
        self.assertEqual(self.RS._tree_data.nats_messenger.conn.status.get("nats_server"), StatusEnum.ok)
        await wait_for_psce(self.RS.stop_tree(), 1.5)

    async def test_update_nats_witch_config_alpaca(self):
        """test update nats witch alpaca configuration"""
        OBSERVATORY_NAME = 'test_observatory'
        tao = TreeAlpacaObservatory(component_name='jk98', observatory_name=OBSERVATORY_NAME)
        broker = TreeBaseBrokerDefaultTarget(component_name="broker", list_providers=[], default_provider=tao)
        self.RS.data_provider = broker

        await wait_for_psce(self.RS.run_tree(), 1.5)
        self.assertEqual(self.RS._tree_data.nats_messenger.conn.status.get("nats_server"), StatusEnum.ok)
        await asyncio.sleep(1)

        reader = get_reader(NatsStreams.ALPACA_CONFIG, deliver_policy="last")
        cfg = await reader.read_next()
        await wait_for_psce(self.RS.stop_tree(), 1.5)  # here close connection to nats

        configuration = SingletonConfig.get_config()
        time_save_read__dif = dt_ensure_datetime(dt_utcnow_array()) - dt_ensure_datetime(tuple(cfg[0].get("published", [])))
        self.assertTrue(time_save_read__dif < datetime.timedelta(minutes=1))
        self.assertDictEqual(cfg[0].get("config").get("telescopes"), {OBSERVATORY_NAME: configuration["tree"][OBSERVATORY_NAME].get()})
        self.assertDictEqual(cfg[0].get("config").get("site"),  configuration["site"].get())

    def test_updating_tree_after_init(self):
        """Test check thee is correct updating after init (call method `post_init_tree()`)"""
        OBSERVATORY_NAME = 'test_observatory'
        tao = TreeAlpacaObservatory(component_name='jk98', observatory_name=OBSERVATORY_NAME)
        broker = TreeBaseBrokerDefaultTarget(component_name="broker", list_providers=[], default_provider=tao)
        RS = self.SampleRequestSolver(data_provider=broker)
        self.assertTrue(RS.data_provider.target_requests == RS)
        self.assertIsInstance(RS.data_provider, TreeBaseBrokerDefaultTarget)
        self.assertTrue(RS.data_provider._default_provider.target_requests == RS)
        self.assertIsNotNone(RS.data_provider._tree_data)
        self.assertIsInstance(RS.data_provider._default_provider, TreeAlpacaObservatory)
        self.assertIsNotNone(RS.data_provider._default_provider._tree_data)
        self.assertEqual(RS.data_provider._default_provider.tree_path, "")


if __name__ == '__main__':
    unittest.main()
