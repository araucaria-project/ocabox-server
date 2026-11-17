"""IRIS-CCD connector against a fake iriscom (same packets and replies as iriscom.c 3.5, one command at a time)."""
import asyncio
import time
import unittest

from obcom.data_colection.address import AddressError
from obcom.data_colection.coded_error import TreeOtherError, TreeStructureError
from obcom.data_colection.response_error import ResponseError

from obsrv.protocols.iris_ccd.iris_ccd_connector import (IrisCcdConnector, IrisProtocolError, ProtocolSettings,
                                                         parse_reply, split_reply)
from test.protocols.iris_ccd.fake_iriscom import (FakeIriscom, StubComponent, camera_options, fast_settings,
                                                  filterwheel_options)


class ReplyParsingTest(unittest.TestCase):
    """Replies exactly as iriscom sent them during the tests on the instrument (2026-11-17)."""

    def setUp(self):
        self.settings = ProtocolSettings(fast_settings())

    def parse(self, text):
        echo, status = split_reply(text)
        return echo, parse_reply(echo, status, self.settings)

    def test_okay(self):
        echo, reply = self.parse('2026-11-17T16:56:42Z go\nOKAY iris00447182r iris00447182d')
        self.assertEqual(echo, 'go')
        self.assertTrue(reply.ok)
        self.assertEqual(reply.message, 'iris00447182r iris00447182d')

    def test_okay_with_arguments_in_echo(self):
        echo, reply = self.parse('2026-11-17T16:58:06Z filter status\nOKAY Success')
        self.assertEqual(echo, 'filter status')
        self.assertEqual(reply.message, 'Success')

    def test_error(self):
        _, reply = self.parse('2026-11-17T17:01:15Z filter 9\nE301 Filter-wheel error')
        self.assertFalse(reply.ok)
        self.assertEqual(reply.code, 301)
        self.assertEqual(reply.message, 'Filter-wheel error')

    def test_error_with_newline_in_message(self):
        _, reply = self.parse('2026-11-17T17:01:15Z go\nE300 Reset read failed.\n')
        self.assertEqual(reply.code, 300)
        self.assertEqual(reply.message, 'Reset read failed.')

    def test_unknown_status(self):
        with self.assertRaises(IrisProtocolError):
            self.parse('2026-11-17T17:01:15Z go\nWHAT now')

    def test_no_status_line(self):
        with self.assertRaises(IrisProtocolError):
            split_reply('2026-11-17T17:01:15Z go')


class IrisTestCase(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        IrisCcdConnector.close_all()
        self.fake = await FakeIriscom.start()
        self.camera_connector = IrisCcdConnector(settings=fast_settings())
        self.wheel_connector = IrisCcdConnector(settings=fast_settings())  # a separate connector, like in the tree
        self.camera = StubComponent('camera', camera_options(self.fake.address))
        self.wheel = StubComponent('filterwheel', filterwheel_options(self.fake.address))

    async def asyncTearDown(self):
        IrisCcdConnector.close_all()
        self.fake.close()

    async def assertCoded(self, coro, exc_class, code):
        with self.assertRaises(exc_class) as ctx:
            await coro
        self.assertEqual(ctx.exception.code, code, ctx.exception.message)
        return ctx.exception


class CameraTest(IrisTestCase):

    async def test_connected(self):
        self.assertTrue(await self.camera_connector.get(self.camera, 'connected'))
        self.assertEqual(self.fake.received, ['sync'])

    async def test_not_connected_when_electronics_do_not_respond(self):
        self.fake.electronics_ok = False
        self.assertFalse(await self.camera_connector.get(self.camera, 'connected'))
        await self.assertCoded(self.camera_connector.put(self.camera, 'connected', Connected=True),
                               TreeOtherError, 4009)

    async def test_exposure(self):
        image = await self.camera_connector.put(self.camera, 'startexposure', Duration=0.3, Light=True)
        self.assertEqual(self.fake.received, ['et 0.3', 'go'])
        self.assertEqual(image['name'], 'iris00447182')
        self.assertEqual(image['number'], 447182)
        self.assertEqual(image['files'], {'reset': '/scr/iris00447182r.fits',
                                          'middle': '/scr/iris00447182m.fits',
                                          'data': '/scr/iris00447182d.fits'})
        self.assertEqual(image['duration'], 0.3)
        self.assertEqual(await self.camera_connector.get(self.camera, 'camerastate'), 0)
        self.assertTrue(await self.camera_connector.get(self.camera, 'imageready'))
        self.assertEqual(await self.camera_connector.get(self.camera, 'percentcompleted'), 100)
        self.assertEqual(await self.camera_connector.get(self.camera, 'lastexposureduration'), 0.3)
        self.assertEqual(await self.camera_connector.get(self.camera, 'lastimage'), image)
        self.assertEqual(await self.camera_connector.get(self.camera, 'lastexposurestarttime'), image['start'])

    async def test_state_during_exposure(self):
        exposure = asyncio.create_task(self.camera_connector.put(self.camera, 'startexposure', Duration=0.4))
        await asyncio.sleep(0.15)
        self.assertEqual(await self.camera_connector.get(self.camera, 'camerastate'), 2)  # exposing
        self.assertFalse(await self.camera_connector.get(self.camera, 'imageready'))
        self.assertTrue(await self.camera_connector.get(self.camera, 'connected'))      # no command sent
        await asyncio.sleep(0.35)
        self.assertEqual(await self.camera_connector.get(self.camera, 'camerastate'), 3)  # reading
        await exposure
        self.assertEqual(await self.camera_connector.get(self.camera, 'camerastate'), 0)
        self.assertEqual(self.fake.received, ['et 0.4', 'go'])

    async def test_busy_during_exposure(self):
        exposure = asyncio.create_task(self.camera_connector.put(self.camera, 'startexposure', Duration=0.3))
        await asyncio.sleep(0.1)
        error = await self.assertCoded(self.wheel_connector.put(self.wheel, 'position', Position=8),
                                       TreeOtherError, 4008)
        self.assertEqual(error.severity, ResponseError.SEVERITY_TEMPORARY)
        await self.assertCoded(self.camera_connector.put(self.camera, 'startexposure', Duration=0.1),
                               TreeOtherError, 4008)
        await self.assertCoded(self.camera_connector.put(self.camera, 'clear'), TreeOtherError, 4008)
        await exposure
        self.assertEqual(self.fake.received, ['et 0.3', 'go'])

    async def test_exposure_finishes_when_client_stops_waiting(self):
        request = asyncio.create_task(self.camera_connector.put(self.camera, 'startexposure', Duration=0.3))
        await asyncio.sleep(0.1)
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertEqual(await self.camera_connector.get(self.camera, 'camerastate'), 2)
        await asyncio.sleep(0.5)
        self.assertTrue(await self.camera_connector.get(self.camera, 'imageready'))
        self.assertEqual((await self.camera_connector.get(self.camera, 'lastimage'))['name'], 'iris00447182')
        self.assertTrue(await self.camera_connector.get(self.camera, 'connected'))  # the link is free again
        self.assertEqual(self.fake.received, ['et 0.3', 'go', 'sync'])

    async def test_exposure_time_not_accepted(self):
        self.fake.et_ignores_value = True
        await self.assertCoded(self.camera_connector.put(self.camera, 'startexposure', Duration=2.5),
                               TreeOtherError, 4009)
        self.assertEqual(self.fake.received, ['et 2.5'])  # no `go` after a wrong exposure time
        self.assertEqual(await self.camera_connector.get(self.camera, 'camerastate'), 5)

    async def test_exposure_error_reply(self):
        self.fake.go_error = 'E300 Reset read failed.\n'
        error = await self.assertCoded(self.camera_connector.put(self.camera, 'startexposure', Duration=0.1),
                                       TreeOtherError, 4009)
        self.assertEqual(error.kwargs.get('device_errno'), 300)
        self.assertEqual(await self.camera_connector.get(self.camera, 'camerastate'), 5)
        self.assertFalse(await self.camera_connector.get(self.camera, 'imageready'))
        # the next exposure clears the error state
        self.fake.go_error = None
        await self.camera_connector.put(self.camera, 'startexposure', Duration=0.1)
        self.assertEqual(await self.camera_connector.get(self.camera, 'camerastate'), 0)

    async def test_exposure_timeout(self):
        connector = IrisCcdConnector(settings=fast_settings(exposure_overhead=0.0, exposure_margin=0.2))
        self.fake.go_overhead = 0.5
        await self.assertCoded(connector.put(self.camera, 'startexposure', Duration=0.1), TreeOtherError, 4005)
        self.assertEqual(await connector.get(self.camera, 'camerastate'), 5)
        # iriscom ends the exposure on its own; the late reply is not taken for the answer to `sync`
        self.assertTrue(await connector.get(self.camera, 'connected'))
        self.assertEqual(self.fake.received, ['et 0.1', 'go', 'sync'])

    async def test_light_false_is_taken_with_a_warning(self):
        with self.assertLogs('iris_ccd_connector', level='WARNING') as logs:
            await self.camera_connector.put(self.camera, 'startexposure', Duration=0.1, Light=False)
        self.assertTrue(any('Light=False' in line for line in logs.output))
        self.assertFalse((await self.camera_connector.get(self.camera, 'lastimage'))['light'])

    async def test_exposure_parameters(self):
        await self.assertCoded(self.camera_connector.put(self.camera, 'startexposure'), AddressError, 1003)
        await self.assertCoded(self.camera_connector.put(self.camera, 'startexposure', Duration='long'),
                               AddressError, 1003)
        await self.assertCoded(self.camera_connector.put(self.camera, 'startexposure', Duration=-1),
                               AddressError, 1003)
        await self.assertCoded(self.camera_connector.put(self.camera, 'startexposure', Duration=7200),
                               AddressError, 1003)
        await self.assertCoded(self.camera_connector.put(self.camera, 'startexposure', Duration=1, Light='maybe'),
                               AddressError, 1003)
        self.assertEqual(self.fake.received, [])

    async def test_frame(self):
        c, cam = self.camera_connector, self.camera
        self.assertEqual(await c.get(cam, 'binx'), 1)
        self.assertEqual(await c.get(cam, 'numx'), 1024)
        self.assertEqual(await c.get(cam, 'cameraysize'), 1024)
        self.assertEqual(await c.get(cam, 'sensorname'), 'QUIRC')
        self.assertFalse(await c.get(cam, 'canabortexposure'))
        self.assertIsNone(await c.put(cam, 'binx', BinX=1))
        self.assertIsNone(await c.put(cam, 'numy', NumY=1024))
        self.assertIsNone(await c.put(cam, 'startx', StartX=0))
        await self.assertCoded(c.put(cam, 'binx', BinX=2), AddressError, 1003)
        await self.assertCoded(c.put(cam, 'numx', NumX=512), AddressError, 1003)
        self.assertEqual(self.fake.received, [])

    async def test_not_implemented(self):
        for operation, name in (('put', 'abortexposure'), ('put', 'stopexposure'), ('get', 'imagearray'),
                                ('get', 'gain'), ('put', 'cooleron')):
            error = await self.assertCoded(getattr(self.camera_connector, operation)(self.camera, name),
                                           TreeStructureError, 3002)
            self.assertEqual(error.severity, ResponseError.SEVERITY_CRITICAL)

    async def test_clear_and_xfer(self):
        self.assertEqual(await self.camera_connector.put(self.camera, 'clear'), 'Reset successful.')
        self.assertEqual(await self.camera_connector.get(self.camera, 'xfer'), {'host': None, 'port': 4980})
        self.assertEqual(await self.camera_connector.put(self.camera, 'xfer', Host='aux', Port=5000),
                         {'host': 'aux', 'port': 5000})
        self.assertEqual(await self.camera_connector.put(self.camera, 'xfer', Host='-'),
                         {'host': None, 'port': 5000})
        await self.assertCoded(self.camera_connector.put(self.camera, 'xfer', Host='a b'), AddressError, 1003)
        self.assertEqual(self.fake.received, ['clear', 'xfer', 'xfer aux 5000', 'xfer -'])


class FilterWheelTest(IrisTestCase):

    async def test_names(self):
        self.assertEqual(await self.wheel_connector.get(self.wheel, 'names'), {'J': 8, 'H': 7, 'K': 5})

    async def test_position_unknown_until_set(self):
        self.assertEqual(await self.wheel_connector.get(self.wheel, 'position'), -1)
        result = await self.wheel_connector.put(self.wheel, 'position', Position=8)
        self.assertEqual(result, {'position': 8, 'name': 'J', 'moved': True})
        self.assertEqual(await self.wheel_connector.get(self.wheel, 'position'), 8)
        self.assertEqual(self.fake.received, ['filter 8'])

    async def test_position_by_name(self):
        self.assertEqual((await self.wheel_connector.put(self.wheel, 'position', Position='H'))['position'], 7)
        self.assertEqual(self.fake.received, ['filter 7'])

    async def test_same_filter_is_not_moved(self):
        await self.wheel_connector.put(self.wheel, 'position', Position=5)
        result = await self.wheel_connector.put(self.wheel, 'position', Position=5)
        self.assertEqual(result, {'position': 5, 'name': 'K', 'moved': False})
        self.assertEqual(self.fake.received, ['filter 5'])  # no full turn

    async def test_position_while_moving(self):
        move = asyncio.create_task(self.wheel_connector.put(self.wheel, 'position', Position=7))
        await asyncio.sleep(0.05)
        self.assertEqual(await self.wheel_connector.get(self.wheel, 'position'), -1)
        await self.assertCoded(self.camera_connector.put(self.camera, 'startexposure', Duration=1),
                               TreeOtherError, 4008)
        await move
        self.assertEqual(await self.wheel_connector.get(self.wheel, 'position'), 7)

    async def test_slot_without_filter(self):
        for position in (1, 9, 0, 'X', 2.5, True):
            await self.assertCoded(self.wheel_connector.put(self.wheel, 'position', Position=position),
                                   AddressError, 1003)
        self.assertEqual(self.fake.received, [])

    async def test_move_timeout_leaves_position_unknown(self):
        connector = IrisCcdConnector(settings=fast_settings(filter_move=0.2))
        self.fake.filter_delay = 0.05
        await connector.put(self.wheel, 'position', Position=8)
        self.fake.filter_delay = 0.4
        await self.assertCoded(connector.put(self.wheel, 'position', Position=5), TreeOtherError, 4005)
        self.assertEqual(await connector.get(self.wheel, 'position'), -1)

    async def test_rehome_disabled(self):
        await self.assertCoded(self.wheel_connector.put(self.wheel, 'rehome'), TreeStructureError, 3002)
        self.assertEqual(self.fake.received, [])

    async def test_rehome_when_enabled(self):
        cfg = fast_settings()
        cfg['filterwheel']['rehome_enabled'] = True
        connector = IrisCcdConnector(settings=cfg)
        await connector.put(self.wheel, 'position', Position=8)
        await connector.put(self.wheel, 'rehome')
        self.assertEqual(await connector.get(self.wheel, 'position'), -1)
        self.assertEqual(self.fake.received, ['filter 8', 'filter rehome'])

    async def test_bad_filters_configuration(self):
        wheel = StubComponent('filterwheel', filterwheel_options(self.fake.address, filters={'J': 12}))
        await self.assertCoded(self.wheel_connector.get(wheel, 'names'), TreeStructureError, 3001)


class TransportTest(IrisTestCase):

    async def test_forbidden_commands_are_never_sent(self):
        session = self.camera_connector._session(self.camera)
        for command in ('quit', 'q', 'QUI', 'dl', 'd', 'sync\nquit', ''):
            with self.assertRaises(ValueError):
                await session.command(command, 1.0)
        self.assertEqual(self.fake.received, [])

    async def test_packet_size(self):
        settings = ProtocolSettings(fast_settings())
        self.assertEqual(len(settings.encode('sync')), 1024)
        with self.assertRaises(ValueError):
            settings.encode('x' * 1024)

    async def test_reply_to_another_command_is_dropped(self):
        self.fake.stray_reply_before = 'go'
        self.assertTrue(await self.camera_connector.get(self.camera, 'connected'))

    async def test_camera_and_wheel_share_one_session(self):
        self.assertIs(self.camera_connector._session(self.camera), self.wheel_connector._session(self.wheel))

    async def test_unreachable_iriscom(self):
        address = self.fake.address
        self.fake.close()
        await asyncio.sleep(0.05)
        camera = StubComponent('camera', camera_options(address))
        error = await self.assertCoded(self.camera_connector.get(camera, 'connected'), TreeOtherError, 4005)
        self.assertEqual(error.severity, ResponseError.SEVERITY_NORMAL)

    async def test_missing_address(self):
        camera = StubComponent('camera', camera_options(None))
        await self.assertCoded(self.camera_connector.get(camera, 'connected'), TreeStructureError, 3001)
        camera = StubComponent('camera', camera_options('no-port'))
        await self.assertCoded(self.camera_connector.get(camera, 'connected'), TreeStructureError, 3001)

    async def test_other_kinds_are_not_served(self):
        focuser = StubComponent('focuser', {'address': self.fake.address})
        await self.assertCoded(self.camera_connector.get(focuser, 'position'), TreeStructureError, 3002)


class _FakeSubscriber:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.callback = None
        self.task = None
        self.closed = False

    async def subscribe(self, callback):
        if self.fail:
            raise ConnectionError('NATS not connected')
        self.callback = callback
        self.task = asyncio.get_running_loop().create_task(asyncio.Event().wait())
        return self

    async def close(self):
        self.closed = True
        if self.task is not None:
            self.task.cancel()


class TemperatureTest(IrisTestCase):

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.subscribers = []

        def factory(subject, deliver_policy):
            self.assertEqual(subject, 'tic.telemetry.iris.ccdtemperature')
            self.assertEqual(deliver_policy, 'last')
            subscriber = _FakeSubscriber(fail=getattr(self, 'fail_subscribe', False))
            self.subscribers.append(subscriber)
            return subscriber

        self.camera_connector.subscriber_factory = factory

    @staticmethod
    def message(temperature, ts):
        return {'ts': ts, 'measurements': {'temperature': temperature, 'other': 1}}, {}

    async def test_temperature(self):
        async def publish():
            await asyncio.sleep(0.05)
            now = time.gmtime()
            await self.subscribers[0].callback(*self.message(-195.25, list(now[:6]) + [0]))
        asyncio.get_running_loop().create_task(publish())
        self.assertEqual(await self.camera_connector.get(self.camera, 'ccdtemperature'), -195.25)
        self.assertEqual(len(self.subscribers), 1)
        self.assertEqual(self.fake.received, [])  # nothing asked from iriscom

    async def test_stale_temperature(self):
        task = asyncio.create_task(self.camera_connector.get(self.camera, 'ccdtemperature'))
        await asyncio.sleep(0.01)
        old = time.gmtime(time.time() - 120)
        await self.subscribers[0].callback(*self.message(-190.0, list(old[:6]) + [0]))
        await self.assertCoded(task, TreeOtherError, 4005)

    async def test_no_temperature(self):
        await self.assertCoded(self.camera_connector.get(self.camera, 'ccdtemperature'), TreeOtherError, 4005)

    async def test_subscription_failure(self):
        self.fail_subscribe = True
        await self.assertCoded(self.camera_connector.get(self.camera, 'ccdtemperature'), TreeOtherError, 4005)

    async def test_message_without_value_is_ignored(self):
        task = asyncio.create_task(self.camera_connector.get(self.camera, 'ccdtemperature'))
        await asyncio.sleep(0.01)
        await self.subscribers[0].callback({'ts': None, 'other': {}}, {})
        await self.assertCoded(task, TreeOtherError, 4005)


if __name__ == '__main__':
    unittest.main()
