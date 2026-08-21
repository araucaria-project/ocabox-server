"""Mirror cell (M1 support / active collimation) device contract.

The base ``MirrorCell`` declares the interface and must raise
``TreeStructureError(3002, CRITICAL)`` from every method — a tree configured
with the plain ``mirrorcell`` kind fails loudly instead of falling through to a
nonexistent ALPACA endpoint. ``MirrorCellACC`` implements the interface via the
ASA ACC vendor actions, which live on the *focuser* ALPACA device (the mirror
cell is not an ALPACA device of its own), with all reads served by
``mirrorcell_info`` and decomposed into individually GET-able fields.

Error-model contract:
  * motor fault  → ``TreeOtherError(4009, NORMAL)`` carrying ``device_errno``
    on every decomposed *value* read (``positions``, ``atsetpoint``,
    ``moving``, ``motor<N>position``), never a readable boolean; per-motor
    position reads fault only on their own motor;
  * *status* reads (``mirrorcellstatus``, ``motorstatuses``,
    ``motorstatustexts``, ``motor<N>status``, ``motor<N>statustext``) stay
    readable while faulted — the status enum is the fault channel, and it must
    reach the telemetry time-series exactly when an alert needs it;
  * subsystem absent (``Available: false``, e.g. wk06) → ``TreeValueError(2002,
    CRITICAL)``, i.e. permanent, so SERVICE-policy subscribers stop instead of
    retrying forever; ``available`` itself stays readable as a capability probe.

Per-motor scalar attributes (``motor0position`` … ``motor2statustext``) exist
so telemetry can subscribe to one value per subject; they are resolved by
``MirrorCell._find_attribute`` and therefore exercised through ``device.get()``.

Payloads below are real replies captured from zb08-tcu / jk15-tcu on
2026-08-04 (ASCOM Remote Server 6.6.8419, ACC Focuser device 0).
"""
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from obcom.data_colection.coded_error import TreeOtherError, TreeStructureError
from obcom.data_colection.response_error import ResponseError
from obcom.data_colection.value import TreeValueError

from obsrv.telescope_devices.device_tree import Focuser, MirrorCell, MirrorCellACC


def _motor(index, position, status_value, status_text):
    return {
        "Index": index,
        "Position": {"Value": position, "Unit": "m"},
        "Status": {"Value": status_value, "Text": status_text},
    }


#: zb08, all three motors stopped (µm-scale positions in metres).
ZB08_INFO = {
    "Available": True,
    "Motors": [
        _motor(0, 4.6999998092651369e-06, 9, "Stopped"),
        _motor(1, -1.4e-05, 9, "Stopped"),
        _motor(2, 1.2199999809265136e-05, 9, "Stopped"),
    ],
}

#: wk06 — actions exist, hardware does not.
WK06_INFO = {"Available": False, "Motors": None}


def _make_cell(cls=MirrorCellACC):
    device = cls(sys_id='test.mirrorcell', parent=None)
    device._connector = MagicMock()
    device._connector.put = AsyncMock()
    device._connector.get = AsyncMock()
    return device


def _reply(info):
    """ACC double-encodes: the ALPACA envelope's Value is a JSON *string*."""
    return json.dumps(info)


class MirrorCellBaseContractTest(unittest.IsolatedAsyncioTestCase):
    """Every base-class method raises 3002 CRITICAL and never touches the connector."""

    GETTERS = ['available', 'mirrorcellstatus', 'positions', 'motorstatuses',
               'motorstatustexts', 'atsetpoint', 'moving']
    PER_MOTOR_GETTERS = [f'motor{i}{field}' for i in range(MirrorCell.MOTOR_COUNT)
                         for field in ('position', 'status', 'statustext')]

    async def test_getters_raise_3002_critical(self):
        device = _make_cell(MirrorCell)
        for name in self.GETTERS:
            with self.subTest(method=name):
                with self.assertRaises(TreeStructureError) as ctx:
                    await getattr(device, name)()
                self.assertEqual(ctx.exception.code, 3002)
                self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_CRITICAL)

    async def test_per_motor_getters_raise_3002_critical(self):
        device = _make_cell(MirrorCell)
        for name in self.PER_MOTOR_GETTERS:
            with self.subTest(method=name):
                with self.assertRaises(TreeStructureError) as ctx:
                    await device.get(name)
                self.assertEqual(ctx.exception.code, 3002)
                self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_CRITICAL)

    async def test_commands_raise_3002_critical(self):
        device = _make_cell(MirrorCell)
        calls = [
            device.moveallmotorsto_put(Positions=[0.0, 0.0, 0.0]),
            device.moveallmotorsoffset_put(Offsets=[0.0, 0.0, 0.0]),
            device.moveonemotoroffset_put(Index=0, Offset=1e-06),
            device.stoponemotor_put(Index=0),
            device.stopallmotors_put(),
        ]
        for call in calls:
            with self.assertRaises(TreeStructureError) as ctx:
                await call
            self.assertEqual(ctx.exception.code, 3002)
            self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_CRITICAL)

    async def test_no_fallthrough_to_alpaca_connector(self):
        device = _make_cell(MirrorCell)
        for call in (device.positions(), device.atsetpoint(), device.get('motor0position')):
            with self.assertRaises(TreeStructureError):
                await call
        device._connector.get.assert_not_awaited()
        device._connector.put.assert_not_awaited()


class MirrorCellACCReadsTest(unittest.IsolatedAsyncioTestCase):
    """Reads come from a single mirrorcell_info action, routed via the focuser kind."""

    async def test_status_returns_raw_dict(self):
        device = _make_cell()
        device._connector.put.return_value = _reply(ZB08_INFO)
        self.assertEqual(await device.mirrorcellstatus(), ZB08_INFO)

    async def test_read_is_routed_to_focuser_device_with_native_units(self):
        device = _make_cell()
        device._connector.put.return_value = _reply(ZB08_INFO)
        await device.positions()
        _, kwargs = device._connector.put.call_args
        self.assertEqual(kwargs['kind'], Focuser.KIND)
        self.assertEqual(kwargs['Action'], 'mirrorcell_info')
        self.assertEqual(json.loads(kwargs['Parameters']),
                         {"ConvertPositionToAlpacaFocuserValue": False})

    async def test_positions_are_native_metres_in_index_order(self):
        device = _make_cell()
        device._connector.put.return_value = _reply(ZB08_INFO)
        self.assertEqual(await device.positions(),
                         [4.6999998092651369e-06, -1.4e-05, 1.2199999809265136e-05])

    async def test_motors_are_sorted_by_index_regardless_of_reply_order(self):
        device = _make_cell()
        shuffled = {"Available": True, "Motors": list(reversed(ZB08_INFO["Motors"]))}
        device._connector.put.return_value = _reply(shuffled)
        self.assertEqual(await device.positions(),
                         [4.6999998092651369e-06, -1.4e-05, 1.2199999809265136e-05])

    async def test_statuses_and_texts(self):
        device = _make_cell()
        device._connector.put.return_value = _reply(ZB08_INFO)
        self.assertEqual(await device.motorstatuses(), [9, 9, 9])
        self.assertEqual(await device.motorstatustexts(), ['Stopped', 'Stopped', 'Stopped'])

    async def test_status_text_falls_back_to_local_enum(self):
        device = _make_cell()
        info = {"Available": True, "Motors": [_motor(0, 0.0, 3, None),
                                             _motor(1, 0.0, 3, ""),
                                             _motor(2, 0.0, 3, "AtSetpoint")]}
        device._connector.put.return_value = _reply(info)
        self.assertEqual(await device.motorstatustexts(),
                         ['AtSetpoint', 'AtSetpoint', 'AtSetpoint'])

    async def test_atsetpoint_true_only_when_all_motors_at_setpoint(self):
        device = _make_cell()
        all_at = {"Available": True, "Motors": [_motor(i, 0.0, 3, "AtSetpoint") for i in range(3)]}
        device._connector.put.return_value = _reply(all_at)
        self.assertTrue(await device.atsetpoint())

        one_stopped = {"Available": True, "Motors": [_motor(0, 0.0, 3, "AtSetpoint"),
                                                     _motor(1, 0.0, 9, "Stopped"),
                                                     _motor(2, 0.0, 3, "AtSetpoint")]}
        device._connector.put.return_value = _reply(one_stopped)
        self.assertFalse(await device.atsetpoint())

    async def test_per_motor_scalars_via_get_dispatch(self):
        """motor<N>{position,status,statustext} resolve through device.get()."""
        device = _make_cell()
        device._connector.put.return_value = _reply(ZB08_INFO)
        self.assertEqual(await device.get('motor0position'), 4.6999998092651369e-06)
        self.assertEqual(await device.get('motor1position'), -1.4e-05)
        self.assertEqual(await device.get('motor2position'), 1.2199999809265136e-05)
        self.assertEqual(await device.get('motor1status'), 9)
        self.assertEqual(await device.get('motor1statustext'), 'Stopped')
        device._connector.get.assert_not_awaited()  # served by mirrorcell_info, no fallthrough

    async def test_per_motor_scalars_follow_vendor_index_not_list_order(self):
        device = _make_cell()
        shuffled = {"Available": True, "Motors": list(reversed(ZB08_INFO["Motors"]))}
        device._connector.put.return_value = _reply(shuffled)
        self.assertEqual(await device.get('motor0position'), 4.6999998092651369e-06)
        self.assertEqual(await device.get('motor2position'), 1.2199999809265136e-05)

    async def test_out_of_range_motor_index_is_4007_and_sends_nothing(self):
        device = _make_cell()
        for name in ('motor3position', 'motor12status'):
            with self.subTest(attribute=name):
                with self.assertRaises(TreeOtherError) as ctx:
                    await device.get(name)
                self.assertEqual(ctx.exception.code, 4007)
        device._connector.put.assert_not_awaited()
        device._connector.get.assert_not_awaited()

    async def test_missing_motor_index_in_reply_is_2002(self):
        device = _make_cell()
        two_motors = {"Available": True, "Motors": ZB08_INFO["Motors"][:2]}
        device._connector.put.return_value = _reply(two_motors)
        with self.assertRaises(TreeValueError) as ctx:
            await device.get('motor2position')
        self.assertEqual(ctx.exception.code, 2002)
        self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_NORMAL)

    async def test_moving_true_when_any_motor_moves(self):
        device = _make_cell()
        device._connector.put.return_value = _reply(ZB08_INFO)
        self.assertFalse(await device.moving())
        for code, text in ((1, 'MovingToSetpoint'), (4, 'SpeedCtrlActive'),
                           (7, 'MovingForward'), (8, 'MovingReverse')):
            with self.subTest(status=text):
                info = {"Available": True, "Motors": [_motor(0, 0.0, 9, "Stopped"),
                                                      _motor(1, 0.0, code, text),
                                                      _motor(2, 0.0, 9, "Stopped")]}
                device._connector.put.return_value = _reply(info)
                self.assertTrue(await device.moving())


class MirrorCellACCAvailabilityTest(unittest.IsolatedAsyncioTestCase):
    """Available=false is a capability answer, not a device fault."""

    async def test_available_is_readable_and_false_on_wk06(self):
        device = _make_cell()
        device._connector.put.return_value = _reply(WK06_INFO)
        self.assertIs(await device.available(), False)

    async def test_available_true_on_zb08(self):
        device = _make_cell()
        device._connector.put.return_value = _reply(ZB08_INFO)
        self.assertIs(await device.available(), True)

    async def test_status_still_readable_when_unavailable(self):
        """The raw diagnostic view must not raise either."""
        device = _make_cell()
        device._connector.put.return_value = _reply(WK06_INFO)
        self.assertEqual(await device.mirrorcellstatus(), WK06_INFO)

    async def test_decomposed_reads_raise_2002_critical_when_unavailable(self):
        device = _make_cell()
        device._connector.put.return_value = _reply(WK06_INFO)
        for name in ('positions', 'motorstatuses', 'motorstatustexts', 'atsetpoint', 'moving',
                     'motor0position', 'motor0status', 'motor0statustext'):
            with self.subTest(method=name):
                with self.assertRaises(TreeValueError) as ctx:
                    await device.get(name)
                self.assertEqual(ctx.exception.code, 2002)
                self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_CRITICAL)

    async def test_available_but_no_motors_is_2002_normal(self):
        device = _make_cell()
        device._connector.put.return_value = _reply({"Available": True, "Motors": []})
        with self.assertRaises(TreeValueError) as ctx:
            await device.positions()
        self.assertEqual(ctx.exception.code, 2002)
        self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_NORMAL)


class MirrorCellACCFaultTest(unittest.IsolatedAsyncioTestCase):
    """A faulted motor surfaces as 4009 on value reads; status reads report it."""

    FAULTS = [(0, 'Invalid'), (2, 'TimeoutPositionControl'), (5, 'MovingError'),
              (6, 'TemperatureMotorAboveLimit'),
              (11, 'NoPositionInfo'), (12, 'PositionLimitViolation')]

    def _faulted(self, code, text):
        return {"Available": True, "Motors": [_motor(0, 0.0, 9, "Stopped"),
                                              _motor(1, 0.0, code, text),
                                              _motor(2, 0.0, 9, "Stopped")]}

    async def test_every_fault_status_raises_4009_with_device_errno(self):
        device = _make_cell()
        for code, text in self.FAULTS:
            with self.subTest(status=text):
                device._connector.put.return_value = _reply(self._faulted(code, text))
                with self.assertRaises(TreeOtherError) as ctx:
                    await device.positions()
                self.assertEqual(ctx.exception.code, 4009)
                self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_NORMAL)
                self.assertIn(text, str(ctx.exception.message))

    async def test_fault_hits_all_value_reads(self):
        device = _make_cell()
        device._connector.put.return_value = _reply(self._faulted(5, 'MovingError'))
        for name in ('positions', 'atsetpoint', 'moving', 'motor1position'):
            with self.subTest(method=name):
                with self.assertRaises(TreeOtherError) as ctx:
                    await device.get(name)
                self.assertEqual(ctx.exception.code, 4009)

    async def test_hbridge_open_is_normal_idle_not_a_fault(self):
        """All motors sit in HbridgeOpen (10) during routine operation.

        Verified live 2026-08-19 on jk15 and zb08: drive bridge disengaged,
        encoder positions valid and continuous with earlier readings. Every
        read must work; the motors are neither moving nor actively holding.
        """
        device = _make_cell()
        idle = {"Available": True,
                "Motors": [_motor(0, -9.7e-05, 10, "HbridgeOpen"),
                           _motor(1, 0.00022660000610351562, 10, "HbridgeOpen"),
                           _motor(2, 0.0002075, 10, "HbridgeOpen")]}
        device._connector.put.return_value = _reply(idle)
        self.assertEqual(await device.positions(),
                         [-9.7e-05, 0.00022660000610351562, 0.0002075])
        self.assertEqual(await device.get('motor0position'), -9.7e-05)
        self.assertEqual(await device.motorstatuses(), [10, 10, 10])
        self.assertIs(await device.moving(), False)
        self.assertIs(await device.atsetpoint(), False)

    async def test_per_motor_position_fault_is_isolated(self):
        """Motor 1 faulting must not blind telemetry for motors 0 and 2."""
        device = _make_cell()
        device._connector.put.return_value = _reply(self._faulted(6, 'TemperatureMotorAboveLimit'))
        self.assertEqual(await device.get('motor0position'), 0.0)
        self.assertEqual(await device.get('motor2position'), 0.0)
        with self.assertRaises(TreeOtherError) as ctx:
            await device.get('motor1position')
        self.assertEqual(ctx.exception.code, 4009)

    async def test_status_reads_stay_readable_while_faulted(self):
        """The status enum is the fault channel — it must reach the time-series."""
        device = _make_cell()
        faulted = self._faulted(12, 'PositionLimitViolation')
        device._connector.put.return_value = _reply(faulted)
        self.assertEqual(await device.mirrorcellstatus(), faulted)
        self.assertIs(await device.available(), True)
        self.assertEqual(await device.motorstatuses(), [9, 12, 9])
        self.assertEqual(await device.motorstatustexts(),
                         ['Stopped', 'PositionLimitViolation', 'Stopped'])
        self.assertEqual(await device.get('motor1status'), 12)
        self.assertEqual(await device.get('motor1statustext'), 'PositionLimitViolation')


class MirrorCellACCMalformedReplyTest(unittest.IsolatedAsyncioTestCase):
    """Garbage from the driver becomes 2002, never an AttributeError/TypeError."""

    async def test_unparsable_json_is_2002(self):
        device = _make_cell()
        device._connector.put.return_value = "not json at all"
        with self.assertRaises(TreeValueError) as ctx:
            await device.mirrorcellstatus()
        self.assertEqual(ctx.exception.code, 2002)

    async def test_non_object_json_is_2002(self):
        device = _make_cell()
        device._connector.put.return_value = "[1, 2, 3]"
        with self.assertRaises(TreeValueError) as ctx:
            await device.mirrorcellstatus()
        self.assertEqual(ctx.exception.code, 2002)

    async def test_missing_motor_field_is_2002(self):
        device = _make_cell()
        device._connector.put.return_value = _reply(
            {"Available": True, "Motors": [{"Index": 0, "Status": {"Value": 9, "Text": "Stopped"}}]})
        with self.assertRaises(TreeValueError) as ctx:
            await device.positions()
        self.assertEqual(ctx.exception.code, 2002)


class MirrorCellACCCommandsTest(unittest.IsolatedAsyncioTestCase):
    """Movement/stop actions: payload shape, acknowledgement and validation.

    NOTE: these paths are unit-tested only — no move has been commanded on real
    hardware. See the PR description before wiring any client to them.
    """

    async def test_move_all_motors_to_payload(self):
        device = _make_cell()
        device._connector.put.return_value = "ok"
        await device.moveallmotorsto_put(Positions=[1e-06, -2e-06, 3e-06])
        _, kwargs = device._connector.put.call_args
        self.assertEqual(kwargs['kind'], Focuser.KIND)
        self.assertEqual(kwargs['Action'], 'mirrorcell_move_all_motors_to')
        self.assertEqual(json.loads(kwargs['Parameters']), {
            "PositionIsAlpacaFocuserValue": False,
            "Positions": [{"Value": 1e-06, "Unit": "m"},
                          {"Value": -2e-06, "Unit": "m"},
                          {"Value": 3e-06, "Unit": "m"}],
        })

    async def test_move_one_motor_offset_payload(self):
        device = _make_cell()
        device._connector.put.return_value = "ok"
        await device.moveonemotoroffset_put(Index=2, Offset=-5e-06)
        _, kwargs = device._connector.put.call_args
        self.assertEqual(kwargs['Action'], 'mirrorcell_move_one_motor_offset')
        self.assertEqual(json.loads(kwargs['Parameters']),
                         {"Index": 2, "Offset": {"Value": -5e-06, "Unit": "m"}})

    async def test_stop_all_motors_sends_empty_parameters(self):
        device = _make_cell()
        device._connector.put.return_value = "ok"
        await device.stopallmotors_put()
        _, kwargs = device._connector.put.call_args
        self.assertEqual(kwargs['Action'], 'mirrorcell_stop_all_motors')
        self.assertEqual(kwargs['Parameters'], '')

    async def test_stop_one_motor_payload(self):
        device = _make_cell()
        device._connector.put.return_value = "ok"
        await device.stoponemotor_put(Index=1)
        _, kwargs = device._connector.put.call_args
        self.assertEqual(kwargs['Action'], 'mirrorcell_stop_one_motor')
        self.assertEqual(json.loads(kwargs['Parameters']), {"Index": 1})

    async def test_non_ok_acknowledgement_is_4009(self):
        device = _make_cell()
        device._connector.put.return_value = "Error: axis not referenced"
        with self.assertRaises(TreeOtherError) as ctx:
            await device.stopallmotors_put()
        self.assertEqual(ctx.exception.code, 4009)
        self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_NORMAL)

    async def test_ok_acknowledgement_is_case_and_space_tolerant(self):
        device = _make_cell()
        for reply in ("ok", "OK", " ok\n"):
            with self.subTest(reply=reply):
                device._connector.put.return_value = reply
                self.assertEqual(await device.stopallmotors_put(), reply)

    async def test_bad_index_is_4007_and_sends_nothing(self):
        device = _make_cell()
        for bad in ('x', None, -1, 3, 99):
            with self.subTest(index=bad):
                device._connector.put.reset_mock()
                with self.assertRaises(TreeOtherError) as ctx:
                    await device.stoponemotor_put(Index=bad)
                self.assertEqual(ctx.exception.code, 4007)
                device._connector.put.assert_not_awaited()

    async def test_wrong_number_of_positions_is_4007_and_sends_nothing(self):
        device = _make_cell()
        for bad in ([], [0.0], [0.0, 0.0], [0.0] * 4, "0.0", None, 0.0):
            with self.subTest(positions=bad):
                device._connector.put.reset_mock()
                with self.assertRaises(TreeOtherError) as ctx:
                    await device.moveallmotorsto_put(Positions=bad)
                self.assertEqual(ctx.exception.code, 4007)
                device._connector.put.assert_not_awaited()

    async def test_non_numeric_offset_is_4007(self):
        device = _make_cell()
        with self.assertRaises(TreeOtherError) as ctx:
            await device.moveonemotoroffset_put(Index=0, Offset='nudge')
        self.assertEqual(ctx.exception.code, 4007)
        device._connector.put.assert_not_awaited()

    async def test_int_like_strings_are_accepted(self):
        """TIC receives parameters over the wire, so int-like strings must work."""
        device = _make_cell()
        device._connector.put.return_value = "ok"
        await device.moveonemotoroffset_put(Index='1', Offset='1e-06')
        _, kwargs = device._connector.put.call_args
        self.assertEqual(json.loads(kwargs['Parameters']),
                         {"Index": 1, "Offset": {"Value": 1e-06, "Unit": "m"}})


class MirrorCellKindRegistrationTest(unittest.TestCase):
    """Both kinds are resolvable from the tree builder."""

    def test_kinds_registered(self):
        from obsrv.telescope_devices.device_tree import _component_classes
        self.assertIs(_component_classes['mirrorcell'], MirrorCell)
        self.assertIs(_component_classes['mirrorcellACC'], MirrorCellACC)

    def test_acc_is_a_mirror_cell(self):
        self.assertTrue(issubclass(MirrorCellACC, MirrorCell))
        self.assertEqual(MirrorCellACC.KIND, 'mirrorcell')


if __name__ == '__main__':
    unittest.main()
