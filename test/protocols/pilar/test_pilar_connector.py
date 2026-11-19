"""The Pilar connector against the fake Pilar: mapping, mount, freezes, covers, focuser, rotator, error codes."""
import asyncio
import time
import unittest

from obcom.data_colection.address import AddressError
from obcom.data_colection.coded_error import TreeOtherError, TreeStructureError
from obcom.data_colection.response_error import ResponseError

from obsrv.protocols.pilar.pilar_connector import (PilarBusy, PilarConnector, PilarLink, PilarTransient,
                                                   PilarUnsupported, load_protocol_config)
from obsrv.protocols.pilar.pilar_tpl2 import Tpl2Item, Tpl2Special
from test.protocols.pilar.fake_pilar import FakePilar
from test.protocols.pilar.helpers import component, fast_settings


class PilarTestCase(unittest.IsolatedAsyncioTestCase):
    ready = True
    changes = {}

    async def asyncSetUp(self):
        PilarConnector.close_all()
        self.fake = await FakePilar.start(ready=self.ready)
        self.connector = PilarConnector(settings=fast_settings(**self.changes))
        a = self.fake.address
        self.mount = component('telescope', a)
        self.cover = component('covercalibrator', a)
        self.focuser = component('focuser', a)
        self.rotator = component('rotator', a, device_number=2)

    async def asyncTearDown(self):
        await PilarConnector.aclose_all()
        await self.fake.close()

    async def get(self, comp, name, **data):
        return await self.connector.get(comp, name, **data)

    async def put(self, comp, name, **data):
        return await self.connector.put(comp, name, **data)

    async def until(self, condition, timeout=5.0, what='condition'):
        deadline = time.monotonic() + timeout
        while True:
            value = condition()
            if asyncio.iscoroutine(value):
                value = await value
            if value:
                return
            if time.monotonic() > deadline:
                self.fail(f"{what} not met in {timeout} s")
            await asyncio.sleep(0.02)

    async def slewing(self):
        try:
            return await self.get(self.mount, 'slewing')
        except TreeOtherError as e:
            return e.severity == ResponseError.SEVERITY_TEMPORARY   # frozen: unknown, keep waiting

    async def wait_slew_end(self, timeout=5.0):
        await asyncio.sleep(0.05)
        deadline = time.monotonic() + timeout
        while await self.slewing():
            if time.monotonic() > deadline:
                self.fail("slew not ended")
            await asyncio.sleep(0.03)

    def link(self) -> PilarLink:
        return PilarConnector._links[self.fake.address]


class SettingsTest(unittest.TestCase):

    def test_default_configuration_loads(self):
        connector = PilarConnector()
        self.assertEqual(set(connector.settings.components),
                         {'telescope', 'covercalibrator', 'focuser', 'rotator'})
        self.assertIn('telescope:motoron', connector.settings.components['telescope'].actions)

    def test_unknown_procedure_is_refused(self):
        cfg = load_protocol_config()
        cfg['components']['telescope']['put']['park'] = {'procedure': 'no_such_one'}
        with self.assertRaises(RuntimeError) as ctx:
            PilarConnector(settings=cfg)
        self.assertIn('no_such_one', str(ctx.exception))

    def test_yaml_boolean_option_name_is_refused(self):
        cfg = load_protocol_config()
        cfg['components']['telescope']['put']['park'] = {'procedure': 'power', True: False}
        with self.assertRaises(RuntimeError):
            PilarConnector(settings=cfg)

    def test_get_procedure_in_put_is_refused(self):
        cfg = load_protocol_config()
        cfg['components']['telescope']['put']['park'] = {'procedure': 'slewing'}
        with self.assertRaises(RuntimeError):
            PilarConnector(settings=cfg)

    def test_pool_too_small(self):
        cfg = load_protocol_config()
        cfg['link']['pool_size'] = 2
        with self.assertRaises(RuntimeError):
            PilarConnector(settings=cfg)


class MappingTest(PilarTestCase):

    async def test_plain_variables_and_fixed_values(self):
        self.assertEqual(await self.get(self.mount, 'azimuth'), 180.0)
        self.assertEqual(await self.get(self.mount, 'altitude'), 45.0)
        self.assertTrue(0 <= await self.get(self.mount, 'rightascension') < 24)
        self.assertTrue(await self.get(self.mount, 'canpark'))
        self.assertFalse(await self.get(self.mount, 'athome'))
        self.assertEqual(await self.get(self.cover, 'calibratorstate'), 0)
        self.assertTrue(await self.get(self.mount, 'connected'))

    async def test_not_mapped_is_3002(self):
        for comp, op, name, data in ((self.mount, 'get', 'sideofpier', {}),
                                     (self.mount, 'put', 'findhome', {}),
                                     (self.mount, 'put', 'synctoaltaz', {'Azimuth': 1, 'Altitude': 40}),
                                     (self.cover, 'put', 'haltcover', {}),
                                     (self.focuser, 'put', 'halt', {}),
                                     (self.rotator, 'put', 'halt', {}),
                                     (self.mount, 'put', 'commandstring', {'Command': 'MotStat', 'Raw': 'True'})):
            with self.subTest(name=name):
                with self.assertRaises(TreeStructureError) as ctx:
                    await getattr(self.connector, op)(comp, name, **data)
                self.assertEqual(ctx.exception.code, 3002)
                self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_CRITICAL)

    async def test_actions_of_the_device_tree(self):
        self.assertEqual(await self.put(self.focuser, 'action', Action='fansstatus', Parameters=''), 0)
        text = await self.put(self.mount, 'action', Action='telescope:errorstring', Parameters='')
        self.assertIn('ERR_USV_BatteryAlarm', text)
        for action in ('telescope:motoroff', 'telescope:reportmaxalt'):
            with self.assertRaises(TreeStructureError) as ctx:
                await self.put(self.mount, 'action', Action=action, Parameters='')
            self.assertEqual(ctx.exception.code, 3002)
        with self.assertRaises(TreeStructureError):
            await self.put(self.focuser, 'action', Action='fansturnon', Parameters='')

    async def test_variable_missing_in_pilar_is_3002(self):
        self.connector.settings.components['telescope'].get['azimuth'].variable = 'POSITION.HORIZONTAL.NOPE'
        with self.assertRaises(TreeStructureError) as ctx:
            await self.get(self.mount, 'azimuth')
        self.assertEqual(ctx.exception.code, 3002)

    async def test_components_share_one_link(self):
        other = PilarConnector(settings=fast_settings())
        await self.get(self.mount, 'azimuth')
        await other.get(self.focuser, 'position')
        await other.get(self.cover, 'coverstate')
        self.assertEqual(len(PilarConnector._links), 1)
        await asyncio.gather(*(self.get(self.mount, 'azimuth') for _ in range(20)))
        self.assertLessEqual(self.fake.clients, 4)

    async def test_wrong_parameters_are_1003(self):
        for name, data in (('slewtoaltazasync', {'Azimuth': 10}),
                           ('slewtoaltazasync', {'Azimuth': 10, 'Altitude': 'high'}),
                           ('slewtoaltazasync', {'Azimuth': 10, 'Altitude': 2.0}),      # below 90 - ZD limit
                           ('slewtocoordinatesasync', {'RightAscension': 25, 'Declination': 0}),
                           ('tracking', {'Tracking': 'maybe'})):
            with self.subTest(name=name, data=data):
                with self.assertRaises(AddressError) as ctx:
                    await self.put(self.mount, name, **data)
                self.assertEqual(ctx.exception.code, 1003)

    async def test_no_epoch_is_3001(self):
        mount = component('telescope', self.fake.address, epoch=None)
        with self.assertRaises(TreeStructureError) as ctx:
            await self.put(mount, 'slewtocoordinatesasync', RightAscension=20, Declination=-30)
        self.assertEqual(ctx.exception.code, 3001)

    async def test_special_values(self):
        link = PilarLink.__new__(PilarLink)
        link.settings = self.connector.settings
        link._unanswered_since, link._guard_until = None, 0.0
        with self.assertRaises(PilarBusy):
            link.value(Tpl2Item('X', None, Tpl2Special('LOCKEDBY', '31')), 'X')
        with self.assertRaises(PilarUnsupported):
            link.value(Tpl2Item('X', None, Tpl2Special('DIMENSION')), 'X')
        link._guard_until = time.monotonic() + 1
        with self.assertRaises(PilarTransient):
            link.value(Tpl2Item('X', None, Tpl2Special('FAILED')), 'X')


class MountTest(PilarTestCase):

    async def test_slew_altaz(self):
        await self.put(self.mount, 'slewtoaltazasync', Azimuth=210.0, Altitude=60.0)
        self.assertTrue(await self.get(self.mount, 'slewing'))
        await self.wait_slew_end()
        self.assertAlmostEqual(self.fake.az, 210.0, places=1)
        self.assertAlmostEqual(self.fake.alt, 60.0, places=1)
        self.assertFalse(await self.get(self.mount, 'tracking'))
        self.assertEqual(self.fake.count('SET POINTING.TRACK=2'), 1)

    async def test_synchronous_slew_waits(self):
        await self.put(self.mount, 'slewtoaltaz', Azimuth=120.0, Altitude=50.0)
        self.assertFalse(self.fake.moving())
        self.assertAlmostEqual(self.fake.az, 120.0, places=1)

    async def test_slew_equatorial_and_tracking(self):
        await self.put(self.mount, 'slewtocoordinatesasync', RightAscension=20.5, Declination=-30.0)
        for body in ('SET OBJECT.EQUATORIAL.EPOCH=2000', 'SET OBJECT.EQUATORIAL.EQUINOX=2000',
                     'SET OBJECT.EQUATORIAL.RA_PM=0', 'SET OBJECT.EQUATORIAL.RA=20.5', 'SET POINTING.TRACK=1'):
            self.assertIn(body, self.fake.received)
        self.assertTrue(await self.get(self.mount, 'slewing'))     # TRACK=1 starts after a delay
        await self.wait_slew_end()
        self.assertTrue(await self.get(self.mount, 'tracking'))
        await self.put(self.mount, 'tracking', Tracking=False)
        self.assertFalse(await self.get(self.mount, 'tracking'))

    async def test_tracking_after_an_altaz_slew_tracks_the_current_position(self):
        await self.put(self.mount, 'slewtoaltaz', Azimuth=150.0, Altitude=55.0)
        await self.put(self.mount, 'tracking', Tracking=True)
        self.assertEqual(self.fake.count('SET OBJECT.EQUATORIAL.RA='), 1)
        self.assertTrue(await self.get(self.mount, 'tracking'))
        await self.wait_slew_end()
        self.assertAlmostEqual(self.fake.az, 150.0, delta=0.2)

    async def test_abortslew_redirects_to_the_current_position(self):
        self.fake.vmax, self.fake.acc = 10.0, 20.0
        await self.put(self.mount, 'slewtoaltazasync', Azimuth=300.0, Altitude=45.0)
        await asyncio.sleep(0.6)
        await self.put(self.mount, 'abortslew')
        self.assertTrue(await self.get(self.mount, 'slewing'))
        await self.wait_slew_end(10.0)
        self.assertLess(self.fake.az, 250.0)                       # stopped well before the target
        self.assertEqual(self.fake.count('SET TELESCOPE.STOP'), 0)
        self.assertEqual(self.fake.count('SET POINTING.TRACK=2'), 2)

    async def test_abortslew_without_a_slew_does_nothing(self):
        await self.put(self.mount, 'abortslew')
        await asyncio.sleep(0.1)
        self.assertEqual(self.fake.count('SET'), 0)

    async def test_park_and_unpark(self):
        await self.put(self.mount, 'park')
        self.assertTrue(await self.get(self.mount, 'slewing'))
        self.assertFalse(await self.get(self.mount, 'atpark'))
        with self.assertRaises(TreeOtherError) as ctx:
            await self.put(self.mount, 'slewtoaltazasync', Azimuth=100.0, Altitude=40.0)
        self.assertEqual(ctx.exception.code, 4008)
        await self.wait_slew_end(10.0)
        self.assertTrue(await self.get(self.mount, 'atpark'))
        self.assertEqual(await self.get(self.cover, 'coverstate'), 4)     # covers read FAILED when off
        with self.assertRaises(TreeOtherError) as ctx:
            await self.put(self.mount, 'slewtoaltazasync', Azimuth=100.0, Altitude=40.0)
        self.assertEqual(ctx.exception.code, 4009)
        await self.put(self.mount, 'unpark')
        await self.until(lambda: self.fake.powered, what='powered')
        await self.wait_slew_end()
        self.assertFalse(await self.get(self.mount, 'atpark'))
        self.assertEqual(await self.get(self.cover, 'coverstate'), 3)

    async def test_motoron_action_unparks(self):
        self.fake.powered, self.fake.ready_frac = False, 0.0
        await self.put(self.mount, 'action', Action='telescope:motoron', Parameters='')
        await self.until(lambda: self.fake.powered, what='powered')


class FreezeTest(PilarTestCase):

    async def test_read_during_a_freeze_gives_the_last_value(self):
        self.assertEqual(await self.get(self.mount, 'azimuth'), 180.0)
        self.fake.freeze(0.8)
        await asyncio.sleep(0.5)
        self.assertTrue(self.link().frozen())
        self.assertEqual(await self.get(self.mount, 'azimuth'), 180.0)
        self.assertFalse(await self.get(self.mount, 'atpark'))
        await asyncio.sleep(0.45)                                  # end of the freeze: READY_STATE 0, FAILED
        self.assertTrue(self.link().in_guard())
        self.assertFalse(await self.get(self.mount, 'atpark'))     # the value from before the freeze
        self.assertEqual(await self.get(self.mount, 'azimuth'), 180.0)
        await asyncio.sleep(0.5)
        self.assertTrue(self.link().settled())
        self.assertEqual(await self.get(self.mount, 'azimuth'), 180.0)

    async def test_command_during_a_freeze_is_done_after_it(self):
        await self.get(self.mount, 'azimuth')
        self.fake.freeze(0.8)
        await asyncio.sleep(0.4)
        started = time.monotonic()
        await self.put(self.mount, 'slewtoaltazasync', Azimuth=190.0, Altitude=50.0)
        self.assertLess(time.monotonic() - started, 0.1)           # accepted at once
        self.assertTrue(await self.get(self.mount, 'slewing'))
        self.assertNotIn('SET OBJECT.HORIZONTAL.AZ=190', self.fake.received)
        await self.wait_slew_end()
        self.assertAlmostEqual(self.fake.az, 190.0, places=1)

    async def test_abortslew_during_a_freeze_drops_a_waiting_slew(self):
        await self.get(self.mount, 'azimuth')
        self.fake.freeze(0.8)
        await asyncio.sleep(0.4)
        await self.put(self.mount, 'slewtoaltazasync', Azimuth=250.0, Altitude=50.0)
        await self.put(self.mount, 'abortslew')
        await self.wait_slew_end()
        self.assertNotIn('SET OBJECT.HORIZONTAL.AZ=250', self.fake.received)
        self.assertEqual(self.fake.count('SET POINTING.TRACK=2'), 1)   # the abortslew only
        self.assertAlmostEqual(self.fake.az, 180.0, places=1)

    async def test_abortslew_during_a_freeze_keeps_tracking(self):
        await self.put(self.mount, 'slewtocoordinatesasync', RightAscension=20.5, Declination=-30.0)
        await self.wait_slew_end()
        self.fake.freeze(0.6)
        await asyncio.sleep(0.4)
        await self.put(self.mount, 'abortslew')                    # not slewing: nothing to stop
        await asyncio.sleep(1.5)
        self.assertEqual(self.fake.count('SET POINTING.TRACK=2'), 0)
        self.assertTrue(await self.get(self.mount, 'tracking'))

    async def test_tracking_off_during_a_freeze(self):
        await self.put(self.mount, 'slewtocoordinatesasync', RightAscension=20.5, Declination=-30.0)
        await self.wait_slew_end()
        self.fake.freeze(0.6)
        await asyncio.sleep(0.4)
        await self.put(self.mount, 'tracking', Tracking=False)
        await asyncio.sleep(1.5)
        self.assertEqual(self.fake.count('SET POINTING.TRACK=1'), 1)   # not resumed
        self.assertIn('SET POINTING.TRACK=0', self.fake.received)

    async def test_new_connection_during_a_freeze(self):
        """A frozen Pilar does not greet new connections: wait for the greeting, no breaker."""
        await PilarConnector.aclose_all()
        self.fake.freeze(0.6)
        await asyncio.sleep(0.05)
        try:
            await self.get(self.mount, 'azimuth')                  # answered after the freeze: FAILED at first
        except TreeOtherError as e:
            self.assertEqual(e.severity, ResponseError.SEVERITY_TEMPORARY)
        self.assertFalse(self.link().pool.down)
        await asyncio.sleep(0.6)
        self.assertEqual(await self.get(self.mount, 'azimuth'), 180.0)

    async def test_tracking_is_resumed_after_a_freeze(self):
        await self.put(self.mount, 'slewtocoordinatesasync', RightAscension=20.5, Declination=-30.0)
        await self.wait_slew_end()
        self.fake.freeze(0.5)
        await asyncio.sleep(0.6)
        self.assertEqual(self.fake.track, 0)                       # Pilar dropped tracking
        await self.until(lambda: self.fake.track == 1, what='tracking resumed')
        self.assertEqual(self.fake.count('SET POINTING.TRACK=1'), 2)
        await self.wait_slew_end()
        self.assertTrue(await self.get(self.mount, 'tracking'))

    async def test_tracking_not_wanted_is_not_resumed(self):
        await self.put(self.mount, 'slewtocoordinatesasync', RightAscension=20.5, Declination=-30.0)
        await self.wait_slew_end()
        await self.put(self.mount, 'tracking', Tracking=False)
        self.fake.freeze(0.4)
        await asyncio.sleep(1.5)
        self.assertEqual(self.fake.count('SET POINTING.TRACK=1'), 1)

    async def test_long_freeze_is_an_outage(self):
        await self.put(self.mount, 'slewtocoordinatesasync', RightAscension=20.5, Declination=-30.0)
        await self.wait_slew_end()
        self.fake.freeze(2.6)
        await asyncio.sleep(2.4)
        with self.assertRaises(TreeOtherError) as ctx:
            await self.get(self.mount, 'azimuth')
        self.assertEqual(ctx.exception.code, 4005)
        self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_NORMAL)
        self.assertFalse(self.link().tracking_wanted)
        await self.until(lambda: not self.link().outage and self.link().settled(), 5.0, 'link back')
        await asyncio.sleep(0.5)
        self.assertEqual(self.fake.count('SET POINTING.TRACK=1'), 1)   # not resumed after an outage
        self.assertEqual(await self.get(self.mount, 'altitude'), self.fake.alt)

    async def test_cover_command_during_a_freeze(self):
        await self.get(self.cover, 'coverstate')
        self.fake.freeze(0.6)
        await asyncio.sleep(0.4)
        await self.put(self.cover, 'closecover')
        self.assertEqual(await self.get(self.cover, 'coverstate'), 2)
        await self.until(lambda: self.link().operation_active('cover') is None, what='cover closed')
        self.assertEqual(await self.get(self.cover, 'coverstate'), 1)

    async def test_unreachable_pilar(self):
        await self.fake.close()
        mount = component('telescope', '127.0.0.1:1')
        self.assertFalse(await self.get(mount, 'connected'))
        with self.assertRaises(TreeOtherError) as ctx:
            await self.get(mount, 'azimuth')
        self.assertEqual(ctx.exception.code, 4005)
        self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_NORMAL)


class ShortSnapshotTest(PilarTestCase):
    changes = {'watcher.freeze_snapshot_max_age': 0.4, 'timeouts.greeting': 0.2}

    async def test_read_late_in_a_long_freeze_is_temporary(self):
        await self.get(self.mount, 'azimuth')
        self.fake.freeze(1.2)
        await asyncio.sleep(0.8)
        with self.assertRaises(TreeOtherError) as ctx:
            await self.get(self.mount, 'azimuth')
        self.assertEqual(ctx.exception.code, 4005)
        self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_TEMPORARY)

    async def test_no_greeting_is_temporary_and_opens_no_breaker(self):
        await PilarConnector.aclose_all()
        self.fake.freeze(0.8)
        await asyncio.sleep(0.05)
        with self.assertRaises(TreeOtherError) as ctx:
            await self.get(self.mount, 'azimuth')
        self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_TEMPORARY)
        self.assertFalse(self.link().pool.down)
        await asyncio.sleep(1.2)
        self.assertEqual(await self.get(self.mount, 'azimuth'), 180.0)


class CoverTest(PilarTestCase):

    async def test_close_and_open(self):
        self.assertEqual(await self.get(self.cover, 'coverstate'), 3)
        await self.put(self.cover, 'closecover')
        self.assertEqual(await self.get(self.cover, 'coverstate'), 2)
        with self.assertRaises(TreeOtherError) as ctx:
            await self.put(self.cover, 'opencover')
        self.assertEqual(ctx.exception.code, 4008)
        await self.until(lambda: self.link().operation_active('cover') is None, what='cover closed')
        self.assertEqual(await self.get(self.cover, 'coverstate'), 1)
        await self.put(self.cover, 'opencover')
        await self.until(lambda: self.link().operation_active('cover') is None, what='cover open')
        self.assertEqual(await self.get(self.cover, 'coverstate'), 3)


class CoverOffTest(PilarTestCase):
    ready = False

    async def test_cover_when_the_telescope_is_off(self):
        self.assertEqual(await self.get(self.cover, 'coverstate'), 4)
        with self.assertRaises(TreeOtherError) as ctx:
            await self.put(self.cover, 'opencover')
        self.assertEqual(ctx.exception.code, 4009)
        self.assertTrue(await self.get(self.mount, 'atpark'))


class FocuserTest(PilarTestCase):

    async def focuser_idle(self):
        await self.until(lambda: self.link().operation_active('focuser') is None, 15.0, 'focuser idle')

    async def test_move(self):
        self.assertEqual(await self.get(self.focuser, 'position'), 27000)
        self.assertEqual(await self.get(self.focuser, 'maxstep'), 50000)
        await self.put(self.focuser, 'move', Position=28500)
        self.assertTrue(await self.get(self.focuser, 'ismoving'))
        await self.focuser_idle()
        self.assertEqual(await self.get(self.focuser, 'position'), 28500)
        self.assertEqual(self.fake.S['POINTING.SETUP.FOCUS.SYNCMODE'], 16)   # the bit only for the move
        self.assertIn('SET POINTING.SETUP.FOCUS.SYNCMODE=17', self.fake.received)

    async def test_position_is_the_target_within_the_tolerance(self):
        await self.put(self.focuser, 'move', Position=28000)
        await self.focuser_idle()
        focus = 'POSITION.INSTRUMENTAL.FOCUS.REALPOS'
        self.fake.S[focus] = 28.004                                # Pilar keeps it within +-0.004 mm
        await asyncio.sleep(0.2)
        self.assertEqual(await self.get(self.focuser, 'position'), 28000)
        self.fake.S[focus] = 27.996
        await asyncio.sleep(0.2)
        self.assertEqual(await self.get(self.focuser, 'position'), 28000)
        self.fake.S[focus] = 28.05                                 # drift after a mount move: shown as it is
        await asyncio.sleep(0.2)
        self.assertEqual(await self.get(self.focuser, 'position'), 28050)

    async def test_out_of_range(self):
        with self.assertRaises(AddressError) as ctx:
            await self.put(self.focuser, 'move', Position=60000)
        self.assertEqual(ctx.exception.code, 1003)

    async def test_move_asked_during_a_slew_waits_for_its_end(self):
        self.fake.vmax, self.fake.acc = 20.0, 40.0
        await self.put(self.mount, 'slewtoaltazasync', Azimuth=260.0, Altitude=45.0)
        await asyncio.sleep(0.1)
        await self.put(self.focuser, 'move', Position=27500)
        await asyncio.sleep(0.3)
        self.assertNotIn('SET POINTING.TRACK=4', self.fake.received)
        self.assertTrue(await self.get(self.focuser, 'ismoving'))
        await self.focuser_idle()
        self.assertFalse(self.fake.moving())
        self.assertEqual(await self.get(self.focuser, 'position'), 27500)

    async def test_slew_during_a_move_goes_at_once_and_the_move_continues(self):
        self.fake.focus_speed = 0.5
        await self.put(self.focuser, 'move', Position=29000)
        await self.until(lambda: 'SET POINTING.TRACK=4' in self.fake.received, what='focuser started')
        await self.put(self.mount, 'slewtoaltazasync', Azimuth=200.0, Altitude=50.0)
        self.assertEqual(self.fake.S['POSITION.INSTRUMENTAL.FOCUS.POWER_STATE'], 0.0)   # Pilar stopped it
        await self.focuser_idle()
        self.assertEqual(await self.get(self.focuser, 'position'), 29000)
        self.assertEqual(self.fake.count('SET POINTING.TRACK=4'), 2)
        self.assertEqual(self.fake.S['POINTING.SETUP.FOCUS.SYNCMODE'], 16)

    async def test_new_target_replaces_the_move(self):
        self.fake.focus_speed = 0.5
        await self.put(self.focuser, 'move', Position=30000)
        await asyncio.sleep(0.2)
        await self.put(self.focuser, 'move', Position=26000)
        await self.focuser_idle()
        self.assertEqual(await self.get(self.focuser, 'position'), 26000)
        self.assertEqual(self.fake.S['POINTING.SETUP.FOCUS.SYNCMODE'], 16)


class RotatorTest(PilarTestCase):

    async def rotator_idle(self):
        await self.until(lambda: self.link().operation_active('rotator2') is None, what='rotator idle')

    async def test_move_without_tracking(self):
        self.assertEqual(await self.get(self.rotator, 'position'), 10.0)
        await self.put(self.rotator, 'moveabsolute', Position=40.0)
        self.assertTrue(await self.get(self.rotator, 'ismoving'))
        await self.rotator_idle()
        self.assertAlmostEqual(await self.get(self.rotator, 'mechanicalposition'), 40.0, places=2)
        self.assertEqual(await self.get(self.rotator, 'targetposition'), 40.0)
        await self.put(self.rotator, 'move', Position=-5.0)
        await self.rotator_idle()
        self.assertAlmostEqual(await self.get(self.rotator, 'position'), 35.0, places=2)

    async def test_move_in_tracking_and_restore_after_it(self):
        await self.put(self.mount, 'slewtocoordinatesasync', RightAscension=20.5, Declination=-30.0)
        await self.wait_slew_end()
        before = await self.get(self.rotator, 'position')
        await self.put(self.rotator, 'movemechanical', Position=before + 30.0)
        await self.rotator_idle()
        self.assertEqual(self.fake.S['POINTING.SETUP.DEROTATOR.SYNCMODE'], 3)
        self.assertAlmostEqual(await self.get(self.rotator, 'position'), before + 30.0, delta=2.0)
        await self.put(self.mount, 'tracking', Tracking=False)
        self.assertEqual(self.fake.S['POINTING.SETUP.DEROTATOR.SYNCMODE'], 2)
        self.assertEqual(self.fake.S['POINTING.SETUP.DEROTATOR.OFFSET'], 0.0)

    async def test_out_of_range_and_missing_derotator(self):
        with self.assertRaises(AddressError) as ctx:
            await self.put(self.rotator, 'moveabsolute', Position=300.0)
        self.assertEqual(ctx.exception.code, 1003)
        other = component('rotator', self.fake.address, device_number=0)
        with self.assertRaises(TreeOtherError) as ctx:
            await self.get(other, 'position')
        self.assertEqual(ctx.exception.code, 4009)
        third = component('rotator', self.fake.address, device_number=3)
        with self.assertRaises(TreeStructureError) as ctx:
            await self.get(third, 'position')
        self.assertEqual(ctx.exception.code, 3002)


if __name__ == '__main__':
    unittest.main()
