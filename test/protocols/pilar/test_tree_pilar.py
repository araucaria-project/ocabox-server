"""TreeIrisObservatory built from the `tree.iris` configuration of obsrv/config.yaml, with the Pilar components
(mount, its derotator, covers, focuser) pointed at a fake Pilar: requests go client address -> tree -> device ->
Pilar connector."""
import asyncio
import time
import unittest

from obcom.data_colection.address import Address
from obcom.data_colection.value_call import ValueRequest

from obsrv.protocols.pilar.pilar_connector import PilarConnector
from obsrv.tree_components.specialized_components.tree_iris import TreeIrisObservatory
from test.protocols.pilar.fake_pilar import FakePilar
from test.protocols.pilar.helpers import fast_settings


class TreePilarTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        PilarConnector.close_all()
        self.fake = await FakePilar.start()
        self.tree = TreeIrisObservatory(component_name='iris-test', observatory_name='iris')
        observatory = self.tree._observatory
        for name in ('mount', 'covercalibrator', 'focuser'):
            component = observatory.children[name]
            component.component_options['address'] = self.fake.address
            component._connector = PilarConnector(settings=fast_settings())

    async def asyncTearDown(self):
        await self.tree.stop()
        self.assertEqual(PilarConnector._links, {})
        await self.fake.close()

    async def request(self, address: str, request_type: str = 'GET', timeout: float = 5.0, **data):
        request = ValueRequest(Address(address), time.time(), request_type=request_type, request_data=data,
                               request_timeout=time.time() + timeout)
        return await self.tree.get_response(request)

    async def test_derotator_is_a_rotator_of_the_mount(self):
        derotator = self.tree._observatory.children['mount'].children['derotator']
        self.assertEqual(derotator.kind, 'rotator')
        self.assertEqual(derotator.device_nr, 2)
        self.assertIsInstance(derotator.connector, PilarConnector)
        response = await self.request('mount.derotator.position')
        self.assertTrue(response.status, response.error)
        self.assertEqual(response.value.v, 10.0)

    async def test_reads_through_the_tree(self):
        response = await self.request('mount.azimuth')
        self.assertEqual(response.value.v, 180.0)
        response = await self.request('mount.rightascension')        # the tree turns hours into degrees
        self.assertTrue(0.0 <= response.value.v < 360.0)
        self.assertGreater(response.value.v, 24.0)
        response = await self.request('mount.errorstring')           # tree: put('action', telescope:errorstring)
        self.assertIn('ERR_USV_BatteryAlarm', response.value.v)
        response = await self.request('covercalibrator.coverstate')
        self.assertEqual(response.value.v, 3)
        response = await self.request('focuser.position')
        self.assertEqual(response.value.v, 27000)

    async def test_commands_through_the_tree(self):
        response = await self.request('mount.slewtoaltazasync', 'PUT', Azimuth=200.0, Altitude=50.0)
        self.assertTrue(response.status, response.error)
        response = await self.request('mount.slewing')
        self.assertTrue(response.value.v)
        response = await self.request('mount.slewtocoordinates', 'PUT', RightAscension=300.0, Declination=-30.0)
        self.assertTrue(response.status, response.error)            # degrees from the client, hours to Pilar
        self.assertIn('SET OBJECT.EQUATORIAL.RA=20', self.fake.received)
        response = await self.request('mount.tracking')
        self.assertTrue(response.value.v)
        response = await self.request('focuser.move', 'PUT', Position=27200)
        self.assertTrue(response.status, response.error)

    async def test_errors_through_the_tree(self):
        response = await self.request('mount.findhome', 'PUT')
        self.assertEqual(response.error.code, 3002)
        response = await self.request('mount.motorstatus')           # tree: put('commandstring', MotStat)
        self.assertEqual(response.error.code, 3002)
        response = await self.request('mount.slewtoaltazasync', 'PUT', Azimuth=10.0, Altitude=1.0)
        self.assertEqual(response.error.code, 1003)
        response = await self.request('mount.derotator.moveabsolute', 'PUT', Position=500.0)
        self.assertEqual(response.error.code, 1003)
        self.fake.freeze(0.8)
        await asyncio.sleep(0.5)
        response = await self.request('mount.azimuth', timeout=2.0)     # during a freeze: the last value
        self.assertTrue(response.status, response.error)
        self.assertEqual(response.value.v, 180.0)
        response = await self.request('covercalibrator.closecover', 'PUT', timeout=2.0)   # done after it
        self.assertTrue(response.status, response.error)
        response = await self.request('focuser.fansstatus')
        self.assertEqual(response.value.v, 0)


if __name__ == '__main__':
    unittest.main()
