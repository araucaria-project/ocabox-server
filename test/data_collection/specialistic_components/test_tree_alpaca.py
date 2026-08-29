import asyncio
import logging
import time
import unittest

from obsrv.communication.request_solver import RequestSolver
from obcom.data_colection.address import Address
from obsrv.tree_components.specialized_components.tree_alpaca import TreeAlpacaObservatory
from obcom.data_colection.value_call import ValueRequest
from obsrv.ob_config import SingletonConfig
from obsrv.utils.asyncio_util_functions import wait_for_psce

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




class TreeAlpacaDeadlineSheddingTest(unittest.IsolatedAsyncioTestCase):
    """Deadline shedding: an already-expired request must be refused with
    4004 TEMPORARY before any device I/O is attempted (no Alpaca server is
    running in this test on purpose — reaching the device would produce a
    different error)."""
    OBSERVATORY_NAME = 'test_observatory'

    def setUp(self):
        super().setUp()
        self.tao = TreeAlpacaObservatory(component_name='sample_component',
                                         observatory_name=self.OBSERVATORY_NAME)

    def _sample_request(self, request_timeout: float) -> ValueRequest:
        sample_components = SingletonConfig.get_config()['tree'][self.OBSERVATORY_NAME]['observatory'][
            'components'].get()
        component = list(sample_components.keys())[0]
        address = Address('.'.join([component, 'name']))
        return ValueRequest(address, time.time(), request_timeout=request_timeout)

    async def test_expired_request_is_shed_when_enabled(self):
        self.tao._shed_expired_requests = True
        request = self._sample_request(request_timeout=time.time() - 1.0)
        response = await self.tao.get_response(request)
        self.assertFalse(response.status)
        self.assertIsNotNone(response.error)
        self.assertEqual(response.error.code, 4004)
        self.assertEqual(response.error.severity, 'TEMPORARY')

    async def test_expired_request_not_shed_when_disabled(self):
        """Flag off (default): the expired request proceeds toward the device
        and fails there — anything but 4004 proves shedding did not trigger."""
        self.tao._shed_expired_requests = False
        request = self._sample_request(request_timeout=time.time() - 1.0)
        response = await self.tao.get_response(request)
        self.assertFalse(response.status)
        self.assertIsNotNone(response.error)
        self.assertNotEqual(response.error.code, 4004)

    async def test_fresh_request_is_not_shed(self):
        """Shedding on, but deadline is in the future — request must pass the
        gate (and then fail on the missing device, again not with 4004)."""
        self.tao._shed_expired_requests = True
        request = self._sample_request(request_timeout=time.time() + 5.0)
        response = await self.tao.get_response(request)
        if not response.status:
            self.assertNotEqual(response.error.code, 4004)


if __name__ == '__main__':
    unittest.main()
