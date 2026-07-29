"""Tertiary (M3) device contract.

The base ``Tertiary`` declares the interface and must raise
``TreeStructureError(3002, CRITICAL)`` from every method — a tree configured
with the plain ``tertiary`` kind fails loudly instead of falling through to a
nonexistent ALPACA endpoint. ``TertiaryOCA`` implements the interface via the
ASA AutoSlew vendor actions on the *telescope* ALPACA device: all reads come
from ``tertiarystatus`` (decomposed into individually GET-able fields),
movement from ``selectnasmythport``.

A controller fault (``ErrorRaised``) must surface through the standard error
model as ``TreeOtherError(4009, NORMAL)`` on every decomposed read — never as
a readable boolean — while ``tertiarystatus`` stays the raw diagnostic view.

Port numbers are physical AutoSlew ports (jk15: 1=ADR6/beso, 2=ADR10/andor),
verified live on jk15-tcu 2026-07-29.
"""
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from obcom.data_colection.coded_error import TreeOtherError, TreeStructureError
from obcom.data_colection.response_error import ResponseError
from obcom.data_colection.value import TreeValueError

from obsrv.telescope_devices.device_tree import Telescope, Tertiary, TertiaryOCA

JK15_STATUS = {
    "Moving": False,
    "MotorOn": True,
    "ErrorRaised": False,
    "Angle": 134.78500116616488,
    "NasmythPort": 2,
    "PortName": "ADR10",
}


def _make_tertiary(cls):
    device = cls(sys_id='test.tertiary', parent=None)
    device._connector = MagicMock()
    device._connector.put = AsyncMock()
    device._connector.get = AsyncMock()
    return device


class TertiaryBaseContractTest(unittest.IsolatedAsyncioTestCase):
    """Every base-class method raises 3002 CRITICAL."""

    async def test_all_interface_methods_raise_3002_critical(self):
        device = _make_tertiary(Tertiary)
        getters = ['nasmythport', 'tertiarystatus', 'angle', 'moving',
                   'motoron', 'portname']
        for name in getters:
            with self.subTest(method=name):
                with self.assertRaises(TreeStructureError) as ctx:
                    await getattr(device, name)()
                self.assertEqual(ctx.exception.code, 3002)
                self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_CRITICAL)
        with self.assertRaises(TreeStructureError) as ctx:
            await device.selectnasmythport_put(Position=2)
        self.assertEqual(ctx.exception.code, 3002)
        self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_CRITICAL)

    async def test_no_fallthrough_to_alpaca_connector(self):
        """Base-class methods must not reach the connector at all."""
        device = _make_tertiary(Tertiary)
        for call in (device.nasmythport(), device.angle()):
            with self.assertRaises(TreeStructureError):
                await call
        device._connector.get.assert_not_awaited()
        device._connector.put.assert_not_awaited()


class TertiaryOCAActionsTest(unittest.IsolatedAsyncioTestCase):
    """TertiaryOCA maps attributes onto AutoSlew actions routed via the telescope kind."""

    async def test_tertiarystatus_parses_autoslew_json(self):
        device = _make_tertiary(TertiaryOCA)
        device._connector.put.return_value = json.dumps(JK15_STATUS)
        result = await device.tertiarystatus()
        self.assertEqual(result, JK15_STATUS)
        device._connector.put.assert_awaited_once_with(
            device, "action", kind=Telescope.KIND,
            Action='tertiarystatus', Parameters='')

    async def test_tertiarystatus_unparsable_reply_raises_2002_normal(self):
        device = _make_tertiary(TertiaryOCA)
        device._connector.put.return_value = "not json"
        with self.assertRaises(TreeValueError) as ctx:
            await device.tertiarystatus()
        self.assertEqual(ctx.exception.code, 2002)
        self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_NORMAL)

    async def test_decomposed_status_fields(self):
        """All reads, including nasmythport, decompose the single status action."""
        device = _make_tertiary(TertiaryOCA)
        device._connector.put.return_value = json.dumps(JK15_STATUS)
        for method, expected in [('nasmythport', 2),
                                 ('angle', JK15_STATUS['Angle']),
                                 ('moving', False),
                                 ('motoron', True),
                                 ('portname', 'ADR10')]:
            with self.subTest(method=method):
                device._connector.put.reset_mock()
                self.assertEqual(await getattr(device, method)(), expected)
                device._connector.put.assert_awaited_once_with(
                    device, "action", kind=Telescope.KIND,
                    Action='tertiarystatus', Parameters='')

    async def test_missing_status_field_raises_2002_normal(self):
        device = _make_tertiary(TertiaryOCA)
        device._connector.put.return_value = json.dumps({"Moving": False})
        with self.assertRaises(TreeValueError) as ctx:
            await device.angle()
        self.assertEqual(ctx.exception.code, 2002)
        self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_NORMAL)

    async def test_error_raised_translates_to_4009_on_every_read(self):
        """Controller fault → standard device error, not a readable boolean."""
        device = _make_tertiary(TertiaryOCA)
        faulted = dict(JK15_STATUS, ErrorRaised=True)
        device._connector.put.return_value = json.dumps(faulted)
        for method in ['nasmythport', 'angle', 'moving', 'motoron', 'portname']:
            with self.subTest(method=method):
                with self.assertRaises(TreeOtherError) as ctx:
                    await getattr(device, method)()
                self.assertEqual(ctx.exception.code, 4009)
                self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_NORMAL)

    async def test_error_raised_keeps_tertiarystatus_readable(self):
        """The raw status stays available as the diagnostic view of a faulted controller."""
        device = _make_tertiary(TertiaryOCA)
        faulted = dict(JK15_STATUS, ErrorRaised=True)
        device._connector.put.return_value = json.dumps(faulted)
        self.assertEqual(await device.tertiarystatus(), faulted)

    async def test_selectnasmythport_sends_position_as_parameters(self):
        device = _make_tertiary(TertiaryOCA)
        device._connector.put.return_value = "true"
        await device.selectnasmythport_put(Position=1)
        device._connector.put.assert_awaited_once_with(
            device, "action", kind=Telescope.KIND,
            Action='selectnasmythport', Parameters='1')

    async def test_selectnasmythport_accepts_int_like_string(self):
        device = _make_tertiary(TertiaryOCA)
        device._connector.put.return_value = "true"
        await device.selectnasmythport_put(Position="2")
        device._connector.put.assert_awaited_once_with(
            device, "action", kind=Telescope.KIND,
            Action='selectnasmythport', Parameters='2')

    async def test_selectnasmythport_missing_position_raises_4007(self):
        """No silent empty-Parameters action call for a movement command."""
        device = _make_tertiary(TertiaryOCA)
        for bad_kwargs in ({}, {"Position": None}, {"Position": "beso"}):
            with self.subTest(kwargs=bad_kwargs):
                with self.assertRaises(TreeOtherError) as ctx:
                    await device.selectnasmythport_put(**bad_kwargs)
                self.assertEqual(ctx.exception.code, 4007)
                self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_NORMAL)
        device._connector.put.assert_not_awaited()

    async def test_selectnasmythport_refusal_raises_4009(self):
        """A non-'true' acknowledgement from AutoSlew is a device-reported error."""
        device = _make_tertiary(TertiaryOCA)
        device._connector.put.return_value = "false"
        with self.assertRaises(TreeOtherError) as ctx:
            await device.selectnasmythport_put(Position=5)
        self.assertEqual(ctx.exception.code, 4009)
        self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_NORMAL)


class TertiaryDeviceDispatchTest(unittest.IsolatedAsyncioTestCase):
    """Device.get()/put() dispatch resolves tertiary attributes to the methods."""

    async def test_get_dispatches_to_method_not_connector_get(self):
        device = _make_tertiary(TertiaryOCA)
        device._connector.put.return_value = json.dumps(JK15_STATUS)
        result = await device.get('angle')
        self.assertEqual(result, JK15_STATUS['Angle'])
        device._connector.get.assert_not_awaited()

    async def test_put_dispatches_to_selectnasmythport_put(self):
        device = _make_tertiary(TertiaryOCA)
        device._connector.put.return_value = "true"
        await device.put('selectnasmythport', Position=2)
        device._connector.put.assert_awaited_once_with(
            device, "action", kind=Telescope.KIND,
            Action='selectnasmythport', Parameters='2')


if __name__ == '__main__':
    unittest.main()
