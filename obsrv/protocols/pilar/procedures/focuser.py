"""Procedures of the focuser. Pilar moves it only via the pointing: a ``SYNCMODE`` bit for the time of the move +
``POSITION`` + ``TRACK=4``. The mount has priority: during its slew a move waits and is sent again after it."""
import asyncio
import logging
import math
import time
from typing import Optional, Tuple

from obsrv.protocols.pilar.pilar_exceptions import PilarDeviceError, PilarTransient
from obsrv.protocols.pilar.pilar_link import PilarLink
from obsrv.protocols.pilar.procedures.base import ProcedureBase

logger = logging.getLogger('pilar_connector')     # one logger for the whole Pilar connector


class FocuserProcedures(ProcedureBase):
    """Mixin of :class:`PilarConnector`: procedures of the focuser."""

    async def _get_focuser_position(self, component, data) -> int:
        """Pilar keeps the focuser within +-0.004 mm of the target, not at it: after a move of the connector the
        position is the target while the focuser stays within ``tolerance`` of it."""
        link = self._link(component)
        p = self.settings.focuser
        position = await link.read_number(p.str('position'))
        target = link.focus_target
        if target is not None and link.operation_active('focuser') is None \
                and abs(position - target) <= p.num('tolerance'):
            position = target
        return int(round(position * p.num('steps_per_mm')))

    async def _get_focuser_moving(self, component, data) -> bool:
        return self._link(component).operation_active('focuser') is not None

    async def _get_focuser_maxstep(self, component, data) -> int:
        link = self._link(component)
        p = self.settings.focuser
        return int(math.floor(await link.limit(p.str('maximum')) * p.num('steps_per_mm')))

    async def _put_focuser_move(self, component, data) -> None:
        link = self._link(component)
        p = self.settings.focuser
        steps = self._as_float(self._param(data, 'Position'), 'Position')
        target = round(steps) / p.num('steps_per_mm')
        limits = await self._limits(link, p.str('minimum'), p.str('maximum'))
        if limits is not None:
            self._check_focuser_target(steps, target, limits)
        previous = link.operation_active('focuser')
        if previous is not None:
            previous.task.cancel()                        # a new target replaces the move in progress
        link.focus_target = None
        link.start_operation('focuser', f'focuser move to {target:.4f} mm',
                             self._focuser_move(link, target, previous.task if previous else None,
                                                check_limits=limits is None, steps=steps), target)
        return None

    def _check_focuser_target(self, steps: float, target: float, limits: Tuple[float, float]) -> None:
        low, high = limits
        if not low <= target <= high:
            k = self.settings.focuser.num('steps_per_mm')
            raise self._out_of_range('Position', steps, math.ceil(low * k), math.floor(high * k), ' steps')

    async def _focuser_move(self, link: PilarLink, target: float, previous: Optional[asyncio.Task] = None,
                            check_limits: bool = False, steps: float = 0.0) -> None:
        """Bit of SYNCMODE for the time of the move, POSITION, TRACK=4; the mount has priority: during its slew the
        move waits (Pilar switches the focuser motor off) and is sent again after it."""
        p = self.settings.focuser
        s = self.settings
        bit = p.int('syncmode_bit')
        if previous is not None:
            await asyncio.wait({previous})                # the replaced move gives SYNCMODE back first
        if check_limits:
            await link.wait_settled(s.outage_after)
            self._check_focuser_target(steps, target, await self._limits(link, p.str('minimum'), p.str('maximum')))
        try:
            attempts = p.int('attempts')
            for attempt in range(1, attempts + 1):
                await link.wait_mount_idle(s.queue_wait)
                await link.require_ready()
                if link.focus_syncmode_saved is None:
                    saved = int(await link.read_number(p.str('syncmode'), fresh=True))
                    if saved & bit:
                        logger.warning(f"Pilar {link.address}: focuser SYNCMODE {saved} already has bit {bit}; "
                                       f"it will be left as found")
                    link.focus_syncmode_saved = saved
                await link.set(p.str('syncmode'), link.focus_syncmode_saved | bit)
                await link.set(p.str('target'), target)
                await link.set(p.str('track'), p.num('track_value'))
                if await self._focuser_wait(link, target):
                    link.focus_target = target
                    return
                logger.info(f"Pilar {link.address}: focuser move interrupted by the mount, sent again "
                            f"after the slew (attempt {attempt})")
            raise PilarDeviceError(f"focuser move to {target:.4f} mm interrupted by the mount {attempts} times")
        finally:
            saved = link.focus_syncmode_saved
            if saved is not None:
                try:
                    await asyncio.shield(link.set(p.str('syncmode'), saved))
                    link.focus_syncmode_saved = None
                except BaseException as e:
                    logger.warning(f"Pilar {link.address}: focuser SYNCMODE not restored to {saved}: {e}")

    async def _focuser_wait(self, link: PilarLink, target: float) -> bool:
        """True when the focuser is at ``target``; False when the mount started a slew meanwhile."""
        p = self.settings.focuser
        s = self.settings
        start = await link.read_number(p.str('position'), fresh=True)
        allowed = abs(target - start) / p.num('speed') + p.num('timeout_margin')
        deadline = time.monotonic() + allowed
        in_since = None
        while True:
            await asyncio.sleep(s.poll_interval)
            if link.mount_commanded_busy():
                return False
            try:
                position = await link.read_number(p.str('position'), fresh=True)
            except PilarTransient:
                deadline += s.poll_interval              # a freeze: the move goes on, the time is not counted
                in_since = None
                continue
            now = time.monotonic()
            if abs(position - target) <= p.num('tolerance'):
                in_since = in_since if in_since is not None else now
                if now - in_since >= p.num('still_time'):
                    return True
            else:
                in_since = None
            if now > deadline:
                raise PilarDeviceError(f"focuser not at {target:.4f} mm after {allowed:.0f} s "
                                       f"(at {position:.4f} mm)")
