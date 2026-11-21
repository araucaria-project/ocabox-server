"""Procedures of the derotator below the mount (``rotator``). Without tracking: ``TARGETPOS``; in tracking (Pilar
compensates the field rotation): ``SYNCMODE=3`` + ``OFFSET`` + ``TRACK=1``, and ``SYNCMODE`` and ``OFFSET`` come back
after tracking (``PilarLink.restore_derotator``). The mount has priority, as for the focuser."""
import asyncio
import logging
import time
from typing import Optional, Tuple

from obsrv.protocols.pilar.pilar_exceptions import PilarDeviceError, PilarTransient
from obsrv.protocols.pilar.pilar_link import PilarLink
from obsrv.protocols.pilar.procedures.base import ProcedureBase

logger = logging.getLogger('pilar_connector')     # one logger for the whole Pilar connector


class RotatorProcedures(ProcedureBase):
    """Mixin of :class:`PilarConnector`: procedures of the derotator."""

    async def _get_rotator_target(self, component, data) -> float:
        link = self._link(component)
        target = link.rotator_targets.get(component.device_nr)
        if target is not None:
            return target
        var = self._variable(self.settings.derotator.str('position'), component)
        return await link.read_number(var)

    async def _get_rotator_moving(self, component, data) -> bool:
        return self._link(component).operation_active(f'rotator{component.device_nr}') is not None

    async def _put_rotator_move(self, component, data, relative: bool = False) -> None:
        link = self._link(component)
        d = self.settings.derotator
        position_var = self._variable(d.str('position'), component)
        value = self._as_float(self._param(data, 'Position'), 'Position')
        target = value + await link.read_number(position_var) if relative else value
        limit_vars = (self._variable(d.str('minimum'), component), self._variable(d.str('maximum'), component))
        limits = await self._limits(link, *limit_vars)
        if limits is not None and not limits[0] <= target <= limits[1]:
            raise self._out_of_range('Position', target, limits[0], limits[1], ' deg')
        key = f'rotator{component.device_nr}'
        previous = link.operation_active(key)
        if previous is not None:
            previous.task.cancel()
        link.rotator_targets[component.device_nr] = target
        link.start_operation(key, f'rotator move to {target:.3f} deg',
                             self._rotator_move(link, component, target, previous.task if previous else None,
                                                limit_vars if limits is None else None), target)
        return None

    async def _rotator_move(self, link: PilarLink, component, target: float, previous: Optional[asyncio.Task] = None,
                            check_limits: Optional[Tuple[str, str]] = None) -> None:
        """Without tracking: TARGETPOS. In tracking (Pilar compensates the field rotation): SYNCMODE=3 + OFFSET +
        TRACK=1; SYNCMODE and OFFSET come back after tracking. The mount has priority, as for the focuser."""
        d = self.settings.derotator
        s = self.settings
        position_var = self._variable(d.str('position'), component)
        if previous is not None:
            await asyncio.wait({previous})
        if check_limits is not None:
            await link.wait_settled(s.outage_after)
            low, high = await link.limit(check_limits[0]), await link.limit(check_limits[1])
            if not low <= target <= high:
                raise self._out_of_range('Position', target, low, high, ' deg')
        attempts = d.int('attempts')
        for attempt in range(1, attempts + 1):
            await link.wait_mount_idle(s.queue_wait)
            await link.require_ready()
            tracking = await link.read(s.state_track, fresh=True)
            if tracking.ok and tracking.value == s.tracking_value:
                rate = await self._rotator_rate(link, position_var)
                syncmode = await link.read_number(d.str('syncmode'), fresh=True, watch=False)
                offset = await link.read_number(d.str('offset'), fresh=True, watch=False)
                if link.derotator_saved is None:
                    link.derotator_saved = (int(syncmode), offset)
                read_at, current = time.monotonic(), await link.read_number(position_var, fresh=True)
                await link.set(d.str('syncmode'), d.num('tracking_syncmode'))
                await link.set(d.str('offset'), offset + (target - current))
                await link.set(d.str('track'), d.num('track_value'))
                if await self._rotator_settle(link, position_var, rate, target, read_at):
                    return
            else:
                await link.set(self._variable(d.str('target'), component), target, purpose='long',
                               timeout=d.num('timeout'))
                if await self._rotator_arrive(link, position_var, target):
                    return
            logger.info(f"Pilar {link.address}: rotator move interrupted by the mount, sent again after the "
                        f"slew (attempt {attempt})")
        raise PilarDeviceError(f"rotator move to {target:.3f} deg interrupted by the mount {attempts} times")

    async def _rotator_rate(self, link: PilarLink, var: str) -> float:
        """The field rotation [deg/s] seen on the derotator just before a move in tracking."""
        t0, p0 = time.monotonic(), await link.read_number(var, fresh=True)
        await asyncio.sleep(self.settings.derotator.num('still_time'))
        t1, p1 = time.monotonic(), await link.read_number(var, fresh=True)
        return (p1 - p0) / max(t1 - t0, 1e-3)

    async def _rotator_settle(self, link: PilarLink, var: str, rate: float, target: float, since: float) -> bool:
        """In tracking: wait until the derotator is at ``target`` carried on by the field rotation (TRACK=1 starts
        3-4 s after the command) and moves only with the field rotation again."""
        d = self.settings.derotator
        s = self.settings
        deadline = time.monotonic() + d.num('timeout')
        last = None
        still_since = None
        while True:
            await asyncio.sleep(s.poll_interval)
            if link.mount_commanded_busy():
                return False
            try:
                now, position = time.monotonic(), await link.read_number(var, fresh=True)
            except PilarTransient:
                deadline += s.poll_interval
                still_since, last = None, None
                continue
            expected = target + rate * (now - since)
            if last is not None and abs(position - expected) <= d.num('tracking_tolerance'):
                speed = (position - last[1]) / max(now - last[0], 1e-3)
                if abs(speed - rate) < d.num('still_speed'):
                    still_since = still_since if still_since is not None else now
                    if now - still_since >= d.num('still_time'):
                        return True
                else:
                    still_since = None
            else:
                still_since = None
            last = (now, position)
            if now > deadline:
                raise PilarDeviceError(f"rotator not at {expected:.3f} deg (target {target:.3f} + field rotation) "
                                       f"in {d.num('timeout'):.0f} s (at {position:.3f} deg)")

    async def _rotator_arrive(self, link: PilarLink, var: str, target: float) -> bool:
        """Without tracking: wait until the derotator is at ``target`` and stands."""
        d = self.settings.derotator
        s = self.settings
        deadline = time.monotonic() + d.num('timeout')
        in_since = None
        while True:
            if link.mount_commanded_busy():
                return False
            try:
                position = await link.read_number(var, fresh=True)
            except PilarTransient:
                deadline += s.poll_interval
                in_since = None
                await asyncio.sleep(s.poll_interval)
                continue
            now = time.monotonic()
            if abs(position - target) <= d.num('tolerance'):
                in_since = in_since if in_since is not None else now
                if now - in_since >= d.num('still_time'):
                    return True
            else:
                in_since = None
            if now > deadline:
                raise PilarDeviceError(f"rotator not at {target:.3f} deg after {d.num('timeout'):.0f} s "
                                       f"(at {position:.3f} deg)")
            await asyncio.sleep(s.poll_interval)
