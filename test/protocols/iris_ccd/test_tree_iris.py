"""TreeIrisObservatory built from the `tree.iris` configuration of obsrv/config.yaml, with the camera and the
filter wheel pointed at a fake iriscom: requests go client address -> tree -> device -> IRIS-CCD connector."""
import asyncio
import time
import unittest

from obcom.data_colection.address import Address
from obcom.data_colection.value_call import ValueRequest

from obsrv.protocols.iris_ccd.iris_ccd_connector import IrisCcdConnector
from obsrv.tree_components.specialized_components.tree_iris import TreeIrisObservatory
from test.protocols.iris_ccd.fake_iriscom import FakeIriscom, fast_settings


class TreeIrisTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        IrisCcdConnector.close_all()
        self.fake = await FakeIriscom.start()
        self.tree = TreeIrisObservatory(component_name='iris-test', observatory_name='iris')
        observatory = self.tree._observatory
        for name in ('camera', 'filterwheel'):
            component = observatory.children[name]
            component.component_options['address'] = self.fake.address
            component._connector = IrisCcdConnector(settings=fast_settings())

    async def asyncTearDown(self):
        await self.tree.stop()
        self.fake.close()

    async def request(self, address: str, request_type: str = 'GET', timeout: float = 5.0, **data):
        request = ValueRequest(Address(address), time.time(), request_type=request_type, request_data=data,
                               request_timeout=time.time() + timeout)
        return await self.tree.get_response(request)

    def test_tree_has_iris_components(self):
        self.assertEqual(set(self.tree._observatory.children),
                         {'mount', 'covercalibrator', 'dome', 'camera', 'filterwheel', 'focuser'})
        self.assertIsInstance(self.tree._observatory.children['filterwheel'].connector, IrisCcdConnector)

    async def test_exposure_through_the_tree(self):
        response = await self.request('camera.startexposure', 'PUT', Duration=0.2, Light=True)
        self.assertTrue(response.status, response.error)
        self.assertEqual(response.value.v['files']['data'], '/scr/iris00447182d.fits')
        response = await self.request('camera.camerastate')
        self.assertEqual(response.value.v, 0)
        response = await self.request('camera.imageready')
        self.assertTrue(response.value.v)

    async def test_filter_through_the_tree(self):
        response = await self.request('filterwheel.names')
        self.assertEqual(response.value.v, {'J': 8, 'H': 7, 'K': 5})
        response = await self.request('filterwheel.position', 'PUT', Position=7)
        self.assertTrue(response.status, response.error)
        response = await self.request('filterwheel.position')
        self.assertEqual(response.value.v, 7)

    async def test_errors_through_the_tree(self):
        response = await self.request('filterwheel.position', 'PUT', Position=1)
        self.assertFalse(response.status)
        self.assertEqual(response.error.code, 1003)
        response = await self.request('camera.abortexposure', 'PUT')
        self.assertEqual(response.error.code, 3002)
        exposure = asyncio.create_task(self.request('camera.startexposure', 'PUT', Duration=0.3))
        await asyncio.sleep(0.1)
        response = await self.request('filterwheel.position', 'PUT', Position=8)
        self.assertEqual(response.error.code, 4008)
        self.assertTrue((await exposure).status)

    async def test_client_deadline_shorter_than_exposure(self):
        """The client stops waiting (TIC deadline), iriscom does not: the result is kept."""
        response = await self.request('camera.startexposure', 'PUT', timeout=0.2, Duration=0.3)
        self.assertFalse(response.status)
        await asyncio.sleep(0.5)
        response = await self.request('camera.lastimage')
        self.assertEqual(response.value.v['name'], 'iris00447182')


if __name__ == '__main__':
    unittest.main()
