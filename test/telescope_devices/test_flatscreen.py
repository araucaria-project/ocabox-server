"""Flat screen (dome flat) lamp, and ``driver`` in the device tree configuration.

``kind`` says what a component is, ``driver`` how TIC talks to it: ``flatscreen`` + ``asa-autoslew`` is the lamp
on the ASA mount's fan output, switched by the AutoSlew actions ``telescope:startfans`` / ``stopfans``. AutoSlew
cannot read that output (checked on jk15 2026-11-06), so ``lamp`` is 3002 and TIC remembers the last command it
sent in ``commanded``. A flat screen without a driver stays an inert :class:`OpticalElement`. The vendor kinds
from before ``driver`` (``tertiaryOCA``, ``mirrorcellACC``, ``covercalibratorOCA``) still build, with a
deprecation warning.
"""
import asyncio
import time
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from obcom.data_colection.coded_error import TreeOtherError, TreeStructureError
from obcom.data_colection.response_error import ResponseError
from obcom.data_colection.value_call import ValueRequest

from obsrv.telescope_devices.device_tree import (
    Component, ComponentConfigError, FlatScreen, Observatory, OpticalElement, Telescope,
)
from obsrv.telescope_devices.driver_registry import driver
from obsrv.telescope_devices.drivers.asa_autoslew import (
    CoverCalibratorAutoslew, FlatScreenAutoslew, TertiaryAutoslew,
)


def _device(cls):
    device = cls(sys_id='jk15.flatscreen', parent=None)
    device._connector = MagicMock(get=AsyncMock(), put=AsyncMock(return_value=''))
    return device


def _build(components: dict) -> Component:
    root = Component('jk15', None)
    root._connector = MagicMock(get=AsyncMock(), put=AsyncMock(return_value=''))
    root._setup({'components': components})
    return root


class FlatScreenContractTest(unittest.IsolatedAsyncioTestCase):

    async def test_every_method_raises_3002_critical_without_reaching_a_connector(self):
        device = _device(FlatScreen)
        for call in (device.lamp(), device.commanded(), device.lampon_put(), device.lampoff_put()):
            with self.assertRaises(TreeStructureError) as ctx:
                await call
            self.assertEqual(ctx.exception.code, 3002)
            self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_CRITICAL)
        device._connector.put.assert_not_awaited()


class FlatScreenAutoslewTest(unittest.IsolatedAsyncioTestCase):

    async def test_lamp_on_and_off_are_autoslew_actions_on_the_telescope(self):
        device = _device(FlatScreenAutoslew)
        await device.lampon_put()
        device._connector.put.assert_awaited_with(
            device, 'action', kind=Telescope.KIND, Action='telescope:startfans', Parameters='5')
        await device.lampoff_put()
        device._connector.put.assert_awaited_with(
            device, 'action', kind=Telescope.KIND, Action='telescope:stopfans', Parameters='')

    async def test_lamp_level_comes_from_the_configuration(self):
        device = _device(FlatScreenAutoslew)
        device.component_options = {'lamp_level': 7}
        await device.lampon_put()
        self.assertEqual(device._connector.put.call_args.kwargs['Parameters'], '7')

    async def test_lamp_cannot_be_read(self):
        with self.assertRaises(TreeStructureError) as ctx:
            await _device(FlatScreenAutoslew).lamp()
        self.assertEqual(ctx.exception.code, 3002)
        self.assertIn('commanded', ctx.exception.message)

    async def test_commanded_is_unknown_until_a_command(self):
        self.assertEqual(await _device(FlatScreenAutoslew).commanded(), {'lamp': None, 'at': None})

    async def test_commanded_remembers_the_last_command_with_its_utc_time(self):
        device = _device(FlatScreenAutoslew)
        await device.lampon_put()
        on = await device.commanded()
        self.assertEqual(on['lamp'], 'on')
        self.assertEqual(datetime.fromisoformat(on['at']).utcoffset().total_seconds(), 0)
        await device.lampoff_put()
        self.assertEqual((await device.commanded())['lamp'], 'off')

    async def test_a_refused_command_is_not_remembered(self):
        device = _device(FlatScreenAutoslew)
        await device.lampon_put()
        device._connector.put.side_effect = TreeOtherError(address=None, code=4005, message='tcu unreachable')
        with self.assertRaises(TreeOtherError):
            await device.lampoff_put()
        self.assertEqual((await device.commanded())['lamp'], 'on')

    async def test_commanded_follows_the_command_order_not_the_reply_order(self):
        device = _device(FlatScreenAutoslew)
        released = asyncio.Event()

        async def put(*args, Action, **kwargs):
            if Action == 'telescope:startfans':
                await released.wait()  # the ON reply is late
            return ''

        device._connector.put.side_effect = put
        on = asyncio.create_task(device.lampon_put())
        off = asyncio.create_task(device.lampoff_put())
        await asyncio.sleep(0)
        released.set()
        await asyncio.gather(on, off)
        self.assertEqual([c.kwargs['Action'] for c in device._connector.put.await_args_list],
                         ['telescope:startfans', 'telescope:stopfans'])
        self.assertEqual((await device.commanded())['lamp'], 'off')

    async def test_commanded_is_a_copy(self):
        device = _device(FlatScreenAutoslew)
        (await device.commanded())['lamp'] = 'on'
        self.assertIsNone((await device.commanded())['lamp'])


class DriverTreeTest(unittest.IsolatedAsyncioTestCase):

    def test_kind_and_driver_choose_the_class(self):
        root = _build({
            'flatscreen': {'kind': 'flatscreen', 'driver': 'asa-autoslew'},
            'tertiary': {'kind': 'tertiary', 'driver': 'asa-autoslew'},
            'covercalibrator': {'kind': 'covercalibrator', 'driver': 'asa-autoslew'},
        })
        self.assertIsInstance(root.children['flatscreen'], FlatScreenAutoslew)
        self.assertIsInstance(root.children['tertiary'], TertiaryAutoslew)
        self.assertIsInstance(root.children['covercalibrator'], CoverCalibratorAutoslew)

    def test_add_component_takes_the_driver(self):
        observatory = Observatory()
        observatory._connector = MagicMock(get=AsyncMock(), put=AsyncMock(return_value=''))
        screen = observatory.add_component('flatscreen', 'flatscreen', driver='asa-autoslew')
        self.assertIsInstance(screen, FlatScreenAutoslew)

    def test_flat_screen_without_driver_stays_an_optical_element(self):
        self.assertIsInstance(_build({'flatscreen': {'kind': 'flatscreen'}}).children['flatscreen'], OpticalElement)

    def test_unknown_driver_skips_only_that_component(self):
        with self.assertLogs('device_tree', level='ERROR'):
            root = _build({
                'flatscreen': {'kind': 'flatscreen', 'driver': 'no-such-driver'},
                'sky': {'kind': 'sky'},
            })
        self.assertNotIn('flatscreen', root.children)
        self.assertIn('sky', root.children)
        with self.assertRaises(ComponentConfigError):
            Component._create_component(kind='flatscreen', sys_id='t.fs', parent=None, driver='no-such-driver')

    def test_vendor_kinds_still_build_with_a_deprecation_warning(self):
        with self.assertLogs('device_tree', level='WARNING') as logs:
            root = _build({'tertiary': {'kind': 'tertiaryOCA'}, 'covers': {'kind': 'covercalibratorOCA'}})
        self.assertIsInstance(root.children['tertiary'], TertiaryAutoslew)
        self.assertIsInstance(root.children['covers'], CoverCalibratorAutoslew)
        self.assertTrue(any('driver: asa-autoslew' in line for line in logs.output))

    async def test_mount_flat_lamp_commands_go_through_the_flat_screen(self):
        root = _build({
            'mount': {'kind': 'telescope'},
            'flatscreen': {'kind': 'flatscreen', 'driver': 'asa-autoslew'},
        })
        await root.children['mount'].domeflatlampon_put()
        self.assertEqual((await root.children['flatscreen'].commanded())['lamp'], 'on')
        await root.children['mount'].domeflatlampoff_put()
        self.assertEqual((await root.children['flatscreen'].commanded())['lamp'], 'off')

    async def test_mount_flat_lamp_without_flat_screen_device_is_3002(self):
        root = _build({'mount': {'kind': 'telescope'}, 'flatscreen': {'kind': 'flatscreen'}})
        with self.assertRaises(TreeStructureError) as raised:
            await root.children['mount'].domeflatlampon_put()
        self.assertEqual(raised.exception.code, 3002)
        self.assertIn('driver', raised.exception.message)
        root._connector.put.assert_not_awaited()


class ThroughTheTreeTest(unittest.IsolatedAsyncioTestCase):
    """A client sees the device's 3002 with its reason, not a routing 3001 (TreeStructureError with a code answers
    the request; only the bare one routes on)."""

    async def _request(self, components: dict, address: str, request_type: str = 'GET'):
        from obsrv.tree_components.specialized_components.tree_alpaca import TreeAlpacaObservatory
        with self.assertLogs('tree_alpaca', level='WARNING'):  # no 'tree.flat_test' in the configuration
            tree = TreeAlpacaObservatory('flat_test')
        tree._observatory = _build(components)
        request = ValueRequest(f'flat_test.{address}', request_timeout=time.time() + 5, request_type=request_type)
        request.index = 1
        return await tree.get_response(request)

    async def test_unreadable_lamp_is_3002_with_its_reason(self):
        response = await self._request({'flatscreen': {'kind': 'flatscreen', 'driver': 'asa-autoslew'}},
                                   'flatscreen.lamp')
        self.assertFalse(response.status)
        self.assertEqual(response.error.code, 3002)
        self.assertIn("see 'commanded'", response.error.message)

    async def test_mount_lamp_without_flat_screen_device_is_3002_with_its_reason(self):
        response = await self._request({'mount': {'kind': 'telescope'}}, 'mount.domeflatlampon', 'PUT')
        self.assertEqual(response.error.code, 3002)
        self.assertIn('flat screen device', response.error.message)


class DriverRegistryTest(unittest.TestCase):

    def test_a_driver_registers_once(self):
        with self.assertRaises(RuntimeError):
            driver('flatscreen', 'asa-autoslew')(type('Again', (FlatScreen,), {}))


if __name__ == '__main__':
    unittest.main()
