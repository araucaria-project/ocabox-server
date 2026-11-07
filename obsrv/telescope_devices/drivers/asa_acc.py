"""Driver ``asa-acc``: ASA's ACC (Autoslew Control Center) focuser, whose vendor ALPACA ``Action`` commands also
drive the primary mirror cell.
"""
import json
import math
from typing import List

from obcom.data_colection.coded_error import TreeOtherError
from obcom.data_colection.value import TreeValueError

from obsrv.telescope_devices.device_tree import Focuser, MirrorCell
from obsrv.telescope_devices.driver_registry import driver
from obsrv.telescope_devices.standard_components import StandardTelescopeComponents

NAME = 'asa-acc'


@driver(StandardTelescopeComponents.MIRRORCELL, NAME, deprecated_kind='mirrorcellACC')
class MirrorCellACC(MirrorCell):
    """ASA ACC mirror cell, driven through the ACC *focuser*'s ALPACA action channel.

    All reads are served by the single vendor action ``mirrorcell_info``
    (``{"Available": bool, "Motors": [{"Index", "Position": {"Value", "Unit"},
    "Status": {"Value", "Text"}}]}``); movement and stops by the
    ``mirrorcell_move_*`` / ``mirrorcell_stop_*`` actions.

    Wiring notes (verified on OCM 2026-08-04, ASCOM Remote Server 6.6.8419):

    * The mirror cell is **not its own ALPACA device** — the actions live on the
      ACC focuser, so every request is routed with ``kind=Focuser.KIND`` (the
      same trick as ``CoverCalibratorAutoslew``). The component's own
      ``device_number`` selects which focuser; on OCM that is ``0``.
    * The action reply is **double-encoded**: the ALPACA envelope's ``Value`` is
      a *string* containing JSON, hence the explicit ``json.loads`` here.
    * Positions are metres and physically µm-scale (``-9.63e-05`` = −96.3 µm).
      They are passed through unconverted — scaling belongs in whatever
      publishes telemetry, not in the tree.
    * Reads use ``ConvertPositionToAlpacaFocuserValue: false`` (native hardware
      units). The ``true`` variant returns the same reading offset by +1 mm
      while still reporting ``Unit: "m"``, so the flag — not the unit field —
      decides the convention; the tree deliberately exposes one convention.
    * ``mirrorcell_stop_all_motors`` is spelled with the trailing ``s`` (the
      vendor doc drops it in one place; the driver's ``supportedactions`` has it).

    The image plane exposes a byte-identical API under the ``imageplane_``
    prefix, so it becomes a subclass overriding ``ACTION_PREFIX`` once hardware
    reports it available (on OCM it currently reports ``Available: false``).
    """

    #: Vendor action prefix; ``imageplane`` shares the identical payload contract.
    ACTION_PREFIX = 'mirrorcell'

    async def available(self, **kwargs):
        info = await self._info()
        return bool(info.get('Available'))

    async def mirrorcellstatus(self, **kwargs):
        return await self._info()

    async def positions(self, **kwargs):
        return [self._position(m) for m in await self._motors()]

    async def motorstatuses(self, **kwargs):
        return [self._status_code(m) for m in await self._motors_raw()]

    async def motorstatustexts(self, **kwargs):
        return [self._status_text(m) for m in await self._motors_raw()]

    async def atsetpoint(self, **kwargs):
        return all(self._status_code(m) == self.AT_SETPOINT_STATUS for m in await self._motors())

    async def moving(self, **kwargs):
        return any(self._status_code(m) in self.MOVING_STATUSES for m in await self._motors())

    async def _motor_position(self, index: int, **kwargs):
        motor = await self._motor(index)
        if self._status_code(motor) in self.FAULT_STATUSES:
            self._raise_fault(motor)
        return self._position(motor)

    async def _motor_status(self, index: int, **kwargs):
        return self._status_code(await self._motor(index))

    async def _motor_statustext(self, index: int, **kwargs):
        return self._status_text(await self._motor(index))

    async def moveallmotorsto_put(self, **kwargs):
        """Move all motors to absolute positions. Parameter: ``Positions`` (metres)."""
        positions = self._require_motor_values(kwargs.get('Positions'), 'Positions')
        return await self._command(
            'move_all_motors_to',
            PositionIsAlpacaFocuserValue=False,
            Positions=[{"Value": v, "Unit": "m"} for v in positions],
        )

    async def moveallmotorsoffset_put(self, **kwargs):
        """Move all motors by relative offsets. Parameter: ``Offsets`` (metres)."""
        offsets = self._require_motor_values(kwargs.get('Offsets'), 'Offsets')
        return await self._command(
            'move_all_motors_offset',
            Offsets=[{"Value": v, "Unit": "m"} for v in offsets],
        )

    async def moveonemotoroffset_put(self, **kwargs):
        """Move one motor by a relative offset. Parameters: ``Index`` (int), ``Offset`` (metres)."""
        index = self._require_index(kwargs.get('Index'))
        offset = self._require_float(kwargs.get('Offset'), 'Offset')
        return await self._command(
            'move_one_motor_offset',
            Index=index,
            Offset={"Value": offset, "Unit": "m"},
        )

    async def stoponemotor_put(self, **kwargs):
        """Stop one motor. Parameter: ``Index`` (int)."""
        index = self._require_index(kwargs.get('Index'))
        return await self._command('stop_one_motor', Index=index)

    async def stopallmotors_put(self, **kwargs):
        """Stop all motors. Takes no parameters (vendor expects an empty string)."""
        action = f'{self.ACTION_PREFIX}_stop_all_motors'
        return self._verify_ack(action, await self._action(action, ''))

    # --- vendor plumbing -----------------------------------------------------

    async def _action(self, action: str, parameters: str):
        """Send one ACC action on the focuser device and return its raw reply."""
        return await self._put("action", kind=Focuser.KIND, Action=action, Parameters=parameters)

    async def _command(self, suffix: str, **parameters):
        """Send an actuating action and verify the vendor acknowledgement."""
        action = f'{self.ACTION_PREFIX}_{suffix}'
        return self._verify_ack(action, await self._action(action, json.dumps(parameters)))

    def _verify_ack(self, action: str, ret):
        """The driver answers ``"ok"`` on success, an error message otherwise.

        A non-``ok`` reply is a device refusal, so it becomes 4009 instead of
        being returned as if the motion had been accepted.
        """
        if str(ret).strip().lower() != 'ok':
            raise TreeOtherError(
                address=None, code=4009,
                message=f"ACC refused {action} on {self.sys_id}: {ret!r}",
                severity=TreeOtherError.SEVERITY_NORMAL)
        return ret

    async def _info(self) -> dict:
        raw = await self._action(
            f'{self.ACTION_PREFIX}_info',
            json.dumps({"ConvertPositionToAlpacaFocuserValue": False}))
        try:
            info = json.loads(raw)
        except (TypeError, ValueError):
            raise TreeValueError(
                address=None, code=2002,
                message=f"Unparsable {self.ACTION_PREFIX}_info reply from ACC: {raw!r}",
                severity=TreeValueError.SEVERITY_NORMAL) from None
        if not isinstance(info, dict):
            raise TreeValueError(
                address=None, code=2002,
                message=f"Expected a JSON object from {self.ACTION_PREFIX}_info, got {info!r}",
                severity=TreeValueError.SEVERITY_NORMAL)
        return info

    async def _motors_raw(self) -> List[dict]:
        """Motors of an available mirror cell, ordered by motor index — no fault gate.

        Serves the status reads, which must stay readable while a motor is
        faulted. Raises 2002/CRITICAL when the subsystem is absent — permanent
        for that telescope, so a SERVICE-policy subscriber stops instead of
        retrying forever.
        """
        info = await self._info()
        if not info.get('Available'):
            raise TreeValueError(
                address=None, code=2002,
                message=f"Mirror cell is not available on {self.sys_id} "
                        f"(ACC reports Available=false); read 'available' to probe "
                        f"capability without raising",
                severity=TreeValueError.SEVERITY_CRITICAL)
        motors = info.get('Motors')
        if not isinstance(motors, list) or not motors:
            raise TreeValueError(
                address=None, code=2002,
                message=f"ACC reports the mirror cell available but sent no motors: {info!r}",
                severity=TreeValueError.SEVERITY_NORMAL)
        return sorted(motors, key=self._index)

    async def _motors(self) -> List[dict]:
        """Motors of a healthy, available mirror cell — the value-read gate.

        On top of ``_motors_raw``, raises 4009/NORMAL when any motor reports a
        fault: an aggregate value composed over a faulted motor would
        masquerade as healthy data.
        """
        motors = await self._motors_raw()
        faulted = [m for m in motors if self._status_code(m) in self.FAULT_STATUSES]
        if faulted:
            self._raise_fault(faulted[0], context=motors)
        return motors

    async def _motor(self, index: int) -> dict:
        """One motor by vendor ``Index`` — no fault gate (callers decide)."""
        index = self._require_index(index)
        for motor in await self._motors_raw():
            if self._index(motor) == index:
                return motor
        raise TreeValueError(
            address=None, code=2002,
            message=f"Mirror-cell motor {index} missing in ACC reply on {self.sys_id}",
            severity=TreeValueError.SEVERITY_NORMAL)

    def _raise_fault(self, motor: dict, context=None):
        raise TreeOtherError(
            address=None, code=4009,
            message=f"Mirror-cell motor {self._index(motor)} on {self.sys_id} reports "
                    f"{self._status_text(motor)!r}; status: {context if context is not None else motor!r}",
            severity=TreeOtherError.SEVERITY_NORMAL,
            device_errno=self._status_code(motor))

    def _index(self, motor: dict) -> int:
        return self._reply_number(self._require_field(motor, 'Index'), 'Index', int)

    def _position(self, motor: dict) -> float:
        return self._reply_number(
            self._require_field(self._require_field(motor, 'Position'), 'Value'),
            'Position.Value', float)

    def _status_code(self, motor: dict) -> int:
        return self._reply_number(
            self._require_field(self._require_field(motor, 'Status'), 'Value'),
            'Status.Value', int)

    def _reply_number(self, value, field: str, convert):
        """Convert a numeric field of the vendor reply, translating garbage to 2002."""
        try:
            return convert(value)
        except (TypeError, ValueError):
            raise TreeValueError(
                address=None, code=2002,
                message=f"Field {field!r} in ACC {self.ACTION_PREFIX} reply "
                        f"is not a number: {value!r}",
                severity=TreeValueError.SEVERITY_NORMAL) from None

    def _status_text(self, motor: dict) -> str:
        """Status name, preferring the vendor's own text over the local enum."""
        status = self._require_field(motor, 'Status')
        text = status.get('Text') if isinstance(status, dict) else None
        return str(text) if text else self.STATUS_TEXTS.get(self._status_code(motor), 'Unknown')

    def _require_field(self, source, field: str):
        if not isinstance(source, dict) or field not in source:
            raise TreeValueError(
                address=None, code=2002,
                message=f"Field {field!r} missing in ACC {self.ACTION_PREFIX} reply: {source!r}",
                severity=TreeValueError.SEVERITY_NORMAL)
        return source[field]

    def _require_index(self, index) -> int:
        try:
            # bool is an int subclass and float truncation would silently pick
            # the wrong motor (int(1.9) == 1) — both must fail, not convert.
            if isinstance(index, bool) or (isinstance(index, float) and not index.is_integer()):
                raise ValueError
            index = int(index)
        except (TypeError, ValueError):
            raise TreeOtherError(
                address=None, code=4007,
                message=f"Mirror-cell commands require an integer 'Index', got {index!r}",
                severity=TreeOtherError.SEVERITY_NORMAL) from None
        if not 0 <= index < self.MOTOR_COUNT:
            raise TreeOtherError(
                address=None, code=4007,
                message=f"Mirror-cell motor 'Index' must be 0..{self.MOTOR_COUNT - 1}, got {index}",
                severity=TreeOtherError.SEVERITY_NORMAL)
        return index

    def _require_float(self, value, name: str) -> float:
        try:
            # bool would convert to a 1-metre command on a µm-scale actuator;
            # NaN/±inf would even serialize as nonstandard JSON tokens.
            if isinstance(value, bool) or not math.isfinite(number := float(value)):
                raise ValueError
            return number
        except (TypeError, ValueError):
            raise TreeOtherError(
                address=None, code=4007,
                message=f"Mirror-cell commands require a number for {name!r} (metres), "
                        f"got {value!r}",
                severity=TreeOtherError.SEVERITY_NORMAL) from None

    def _require_motor_values(self, values, name: str) -> List[float]:
        if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
            raise TreeOtherError(
                address=None, code=4007,
                message=f"{name!r} must be a list of {self.MOTOR_COUNT} numbers (metres), "
                        f"got {values!r}",
                severity=TreeOtherError.SEVERITY_NORMAL)
        if len(values) != self.MOTOR_COUNT:
            raise TreeOtherError(
                address=None, code=4007,
                message=f"{name!r} must hold exactly {self.MOTOR_COUNT} values "
                        f"(one per motor), got {len(values)}",
                severity=TreeOtherError.SEVERITY_NORMAL)
        return [self._require_float(v, name) for v in values]
