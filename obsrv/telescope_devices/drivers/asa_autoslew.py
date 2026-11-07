"""Driver ``asa-autoslew``: ASA mounts under AutoSlew, whose vendor ALPACA ``Action`` commands on the telescope
device also drive the covers, the tertiary (M3) and the mount's fan output, on which OCM's flat screen lamp hangs.

The actions and their quirks: vault ``Domain/Instruments/ASA Autoslew Alpaca Actions``.
"""
import asyncio
import json
from datetime import UTC, datetime
from typing import Optional

from obcom.data_colection.coded_error import TreeOtherError
from obcom.data_colection.value import TreeValueError

from obsrv.telescope_devices.device_tree import Component, CoverCalibrator, FlatScreen, Telescope, Tertiary
from obsrv.telescope_devices.driver_registry import driver
from obsrv.telescope_devices.standard_components import StandardTelescopeComponents

NAME = 'asa-autoslew'


@driver(StandardTelescopeComponents.COVERCALIBRATOR, NAME, deprecated_kind='covercalibratorOCA')
class CoverCalibratorAutoslew(CoverCalibrator):
    """ASA AutoSlew covers (mirror covers), opened and closed by the AutoSlew vendor actions
    ``telescope:opencover`` / ``telescope:closecover`` on the telescope ALPACA device."""

    async def closecover_put(self, **kwargs):
        return await self._put("action", kind=Telescope.KIND, Action='telescope:closecover', Parameters='')

    async def opencover_put(self, **kwargs):
        return await self._put("action", kind=Telescope.KIND, Action='telescope:opencover', Parameters='')


@driver(StandardTelescopeComponents.FLATSCREEN, NAME)
class FlatScreenAutoslew(FlatScreen):
    """ASA (driver ``asa-autoslew``): the flat screen lamp hangs on the mount's fan output, switched by the
    AutoSlew vendor actions ``telescope:startfans <level>`` / ``telescope:stopfans`` on the telescope ALPACA
    device (option ``lamp_level``, default 5).

    AutoSlew cannot read that output (checked on jk15 2026-11-06; the mirror-fan and dome-fan readbacks are
    other outputs; ASA asked for a getter), so ``lamp`` stays 3002 and TIC remembers the last command it sent in
    ``commanded`` — lost on a TIC restart, blind to a switch made outside TIC. Switching is serialised, so
    ``commanded`` follows the order the commands reached AutoSlew, not the order their replies came back."""
    DEFAULT_LEVEL = 5

    def __init__(self, sys_id: str, parent: Optional[Component]) -> None:
        super().__init__(sys_id=sys_id, parent=parent)
        self._commanded = {'lamp': None, 'at': None}
        self._switching = asyncio.Lock()

    @property
    def lamp_level(self) -> int:
        return int(self.component_options.get('lamp_level', self.DEFAULT_LEVEL))

    async def lamp(self, **kwargs):
        self._not_implemented('lamp', why="AutoSlew cannot read the mount's fan output; see 'commanded'")

    async def commanded(self, **kwargs):
        return dict(self._commanded)

    async def lampon_put(self, **kwargs):
        return await self._switch('on', Action='telescope:startfans', Parameters=str(self.lamp_level))

    async def lampoff_put(self, **kwargs):
        return await self._switch('off', Action='telescope:stopfans', Parameters='')

    async def _switch(self, lamp: str, **action):
        async with self._switching:
            ret = await self._put("action", kind=Telescope.KIND, **action)
            self._remember(lamp)
            return ret

    def _remember(self, lamp: str) -> None:
        """Only a command AutoSlew accepted is remembered: a failed PUT raised before this."""
        self._commanded = {'lamp': lamp, 'at': datetime.now(UTC).isoformat(timespec='seconds')}


@driver(StandardTelescopeComponents.TERTIARY, NAME, deprecated_kind='tertiaryOCA')
class TertiaryAutoslew(Tertiary):
    """ASA AutoSlew tertiary, driven through the mount's ALPACA action channel.

    All reads are served by the AutoSlew vendor action ``tertiarystatus``
    (JSON: ``{"Moving", "MotorOn", "ErrorRaised", "Angle", "NasmythPort",
    "PortName"}``); movement by ``selectnasmythport``. Port numbers are the
    physical AutoSlew ports (jk15: 1=ADR6/beso, 2=ADR10/andor) — they are NOT
    0-based; the observatory config maps them to instruments.

    A controller fault (``ErrorRaised`` in the status) is translated to the
    standard error model: every decomposed read raises ``TreeOtherError(4009,
    NORMAL)`` ("device reported an error"), so subscribers and command flows
    see it through their normal error handling instead of polling a
    vendor-specific boolean. ``tertiarystatus`` itself always returns the raw
    dict — the diagnostic view of a faulted controller.
    """

    async def nasmythport(self, **kwargs):
        return await self._status_field('NasmythPort')

    async def tertiarystatus(self, **kwargs):
        return await self._status()

    async def angle(self, **kwargs):
        return await self._status_field('Angle')

    async def moving(self, **kwargs):
        return await self._status_field('Moving')

    async def motoron(self, **kwargs):
        return await self._status_field('MotorOn')

    async def portname(self, **kwargs):
        return await self._status_field('PortName')

    async def selectnasmythport_put(self, **kwargs):
        """Rotate M3 to a physical AutoSlew port. Parameter: ``Position`` (int or int-like str)."""
        position = kwargs.get("Position", None)
        try:
            position = int(position)
        except (TypeError, ValueError):
            raise TreeOtherError(
                address=None, code=4007,
                message=f"selectnasmythport requires an integer 'Position' parameter, got {position!r}",
                severity=TreeOtherError.SEVERITY_NORMAL) from None
        ret = await self._put("action", kind=Telescope.KIND,
                              Action='selectnasmythport', Parameters=str(position))
        if str(ret).strip().lower() != 'true':
            # AutoSlew acknowledges an accepted movement with "true"
            raise TreeOtherError(
                address=None, code=4009,
                message=f"AutoSlew refused selectnasmythport {position}: {ret!r}",
                severity=TreeOtherError.SEVERITY_NORMAL)
        return ret

    async def _status(self) -> dict:
        raw = await self._put("action", kind=Telescope.KIND,
                              Action='tertiarystatus', Parameters='')
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            raise TreeValueError(
                address=None, code=2002,
                message=f"Unparsable tertiarystatus reply from AutoSlew: {raw!r}",
                severity=TreeValueError.SEVERITY_NORMAL) from None

    async def _status_field(self, field: str):
        """Extract one field from ``tertiarystatus``, translating a controller
        fault into the standard 4009 device error."""
        status = await self._status()
        if status.get('ErrorRaised'):
            raise TreeOtherError(
                address=None, code=4009,
                message=f"M3 controller reports an error (AutoSlew ErrorRaised); "
                        f"status: {status!r}",
                severity=TreeOtherError.SEVERITY_NORMAL)
        try:
            return status[field]
        except KeyError:
            raise TreeValueError(
                address=None, code=2002,
                message=f"Field {field!r} missing in AutoSlew tertiarystatus reply: {status!r}",
                severity=TreeValueError.SEVERITY_NORMAL) from None
