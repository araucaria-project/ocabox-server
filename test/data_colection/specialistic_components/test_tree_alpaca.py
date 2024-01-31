import asyncio
import logging
import time
import unittest

from obsrv.comunication.request_solver import RequestSolver
from obcom.data_colection.address import Address
from obsrv.data_colection.specialistic_components.tree_alpaca import TreeAlpacaObservatory
from obcom.data_colection.value_call import ValueRequest
from obsrv.ob_config import SingletonConfig
from obsrv.util_functions.asyncio_util_functions import wait_for_psce

logger = logging.getLogger(__name__.rsplit('.')[-1])


class TreeAlpacaTest(unittest.IsolatedAsyncioTestCase):
    OBSERVATORY_NAME = 'test_observatory'
    OBSERVATORY_DOWN_NAME = 'test_observatory_down'

    def setUp(self):
        super().setUp()
        self.tao = TreeAlpacaObservatory(component_name='sample_component', observatory_name=self.OBSERVATORY_NAME)
        self.rs = RequestSolver(self.tao)

    async def test_alpaca_call(self):
        """
        Test connection to the alpaca server. This test requires the alpaca server to be running in the background.
        """
        sample_alpaca_call = 'name'

        sample_components = SingletonConfig.get_config()['tree'][self.OBSERVATORY_NAME]['observatory'][
            'components'].get()
        component = list(sample_components.keys())[0]

        address = Address('.'.join([component, sample_alpaca_call]))
        request = ValueRequest(address, time.time())

        response = await self.tao.get_response(request)
        self.assertIsNotNone(response.value)
        self.assertTrue(isinstance(response.value.v, str))

    async def test_alpaca_wrong_call(self):
        sample_alpaca_call = 'no_existing_method'

        sample_components = SingletonConfig.get_config()['tree'][self.OBSERVATORY_NAME]['observatory'][
            'components'].get()
        component = list(sample_components.keys())[0]

        address = Address('.'.join([component, sample_alpaca_call]))
        request = ValueRequest(address, time.time())

        response = await self.tao.get_response(request)
        self.assertIsNone(response.value)
        self.assertFalse(response.status)
        self.assertIsNotNone(response.error)
        self.assertTrue(response.error.code == 2002)

    async def test_change_address(self):
        """Test situation when address alpaca module is different from the name of the observatory"""
        sample_alpaca_call = 'name'

        sample_components = SingletonConfig.get_config()['tree'][self.OBSERVATORY_NAME]['observatory'][
            'components'].get()
        component = list(sample_components.keys())[0]

        address = Address('.'.join([component, sample_alpaca_call]))
        request = ValueRequest(address, time.time())

        response = await self.tao.get_response(request)
        self.assertIsNotNone(response.value)
        self.assertTrue(isinstance(response.value.v, str))

    async def test_too_short_address(self):
        """Test situation when incoming address is too short"""
        sample_alpaca_call = 'name'

        sample_components = SingletonConfig.get_config()['tree'][self.OBSERVATORY_NAME]['observatory'][
            'components'].get()
        component = list(sample_components.keys())[0]

        address = Address("block1.block2")
        request = ValueRequest(address, time.time())
        request.index = 2

        response = await self.tao.get_response(request)
        self.assertIsNone(response.value)
        self.assertFalse(response.status)
        self.assertTrue(response.error.code == 1001)

    def test_get_resource(self):
        """Test method get_resources"""
        resources = self.tao.get_resources()
        self.assertEqual(resources, [('dibi_RESOURCE', ['dibi']),
                                     ('dome_RESOURCE', ['dome']),
                                     ('filterwheel_RESOURCE', ['filterwheel']),
                                     ('filterwheel2_RESOURCE', ['filterwheel2']),
                                     ('covercalibrator_RESOURCE', ['covercalibrator']),
                                     ('camera_RESOURCE', ['camera']),
                                     ('focuser_RESOURCE', ['focuser']),
                                     ('derotator_RESOURCE', ['derotator']),
                                     ('switch_RESOURCE', ['switch']),
                                     ('safetymonitor_RESOURCE', ['safetymonitor']),
                                     ('guider_RESOURCE', ['guider'])])

    def test_get_res_manager(self):
        """Test method get_resource_manager()"""
        from obsrv.data_colection.resource_manager.resource_manager import TelescopeComponentManager
        manager: TelescopeComponentManager = asyncio.run(self.tao.get_res_manager())
        self.assertTrue(isinstance(manager, TelescopeComponentManager))
        self.assertEqual(len(manager._resources), 11)

    async def test_alpaca_not_response(self):
        """Test situation when alpaca server is down."""
        # Module with connection to no responding Alpaca
        tao = TreeAlpacaObservatory(component_name='sample_component', observatory_name=self.OBSERVATORY_DOWN_NAME)

        sample_alpaca_call = 'name'
        timeout = 1
        sample_components = SingletonConfig.get_config()['tree'][self.OBSERVATORY_NAME]['observatory'][
            'components'].get()
        component = list(sample_components.keys())[0]

        address = Address('.'.join([component, sample_alpaca_call]))
        request = ValueRequest(address, time.time(), request_timeout=timeout)
        try:
            response = await wait_for_psce(tao.get_response(request), timeout=timeout+2)
        except asyncio.TimeoutError:
            # if this error was raise that mean alpaca module is not working correctly
            raise RuntimeError
        self.assertIsNone(response.value)
        self.assertFalse(response.status)
        self.assertIsNotNone(response.error)
        self.assertTrue(response.error.code == 4005)

    def test_get_configuration(self):
        """Test method get_configuration()"""
        provider = self.tao
        cfg = provider.get_configuration()
        self.assertListEqual(list(cfg.keys()), ["sample_component"])
        self.assertListEqual(list(cfg.get("sample_component").keys()), ["child", "type", "config"])
        self.assertEqual(cfg.get("sample_component").get("type"), "TreeAlpacaObservatory")
        self.assertDictEqual(cfg.get("sample_component").get("child"), {})


if __name__ == '__main__':
    unittest.main()
