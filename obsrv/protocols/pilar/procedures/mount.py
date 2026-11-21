"""Procedures of the mount (``telescope``): connection, state (tracking, slewing, at park, motors), power (park,
unpark), slews, abortslew, tracking.

A mount command asked during a Pilar freeze is done right after it (:meth:`MountProcedures._mount_command`).
``TELESCOPE.STOP``, ``ABORT`` and ``TRACK=0`` do not stop a slew: ``abortslew`` sends the current position as a new
target.
"""
import asyncio
import logging
import time
from typing import Optional

from obsrv.protocols.pilar.pilar_exceptions import (PilarBusy, PilarConfigError, PilarDeviceError, PilarReplyError,
                                                    PilarUnavailable, PilarUnsupported)
from obsrv.protocols.pilar.pilar_link import PilarLink
from obsrv.protocols.pilar.procedures.base import ProcedureBase

logger = logging.getLogger('pilar_connector')     # one logger for the whole Pilar connector


class MountProcedures(ProcedureBase):
    """Mixin of :class:`PilarConnector`: procedures of the mount."""

    async def _get_connected(self, component, data) -> bool:
        link = self._link(component)
        s = self.settings
        try:
            await asyncio.wait_for(link.first_poll.wait(), timeout=s.command_timeout)
        except asyncio.TimeoutError:
            pass
        return link.polled_at is not None and not link.unavailable()

    async def _put_connect(self, component, data) -> None:
        if not self._as_bool(self._param(data, 'Connected'), 'Connected'):
            return None
        if not await self._get_connected(component, data):
            raise PilarUnavailable(f"Pilar at {self._link(component).address} not reachable")
        return None

    async def _get_tracking(self, component, data) -> bool:
        link = self._link(component)
        s = self.settings
        return link.value(await link.read(s.state_track), s.state_track) == s.tracking_value

    async def _get_slewing(self, component, data) -> bool:
        link = self._link(component)
        await link.read(self.settings.state_azimuth)     # raises when no value is known (e.g. a long freeze)
        return link.slewing()

    async def _get_at_park(self, component, data) -> bool:
        link = self._link(component)
        s = self.settings
        ready = await link.read_number(s.state_ready)        # during a freeze: the value from before it
        op = link.operation_active('mount')
        if op is not None and op.kind == 'power':
            return False
        return ready == s.off_value

    async def _get_motor_status(self, component, data) -> str:
        """Power of the mount axes (OpenTSI POWER_STATE), e.g. 'AZ on, ZD on'."""
        link = self._link(component)
        p = self.settings.motor_status
        states = p.section('states').data
        parts = []
        for axis, var in p.section('axes').data.items():
            item = await link.read(var)
            if not item.ok:
                parts.append(f'{axis} {item.special}')
                continue
            value = link.number(item, var)
            name = next((text for state, text in states.items() if abs(float(state) - value) < 1e-6), None)
            parts.append(f'{axis} {name if name is not None else f"{value:g}"}')
        return ', '.join(parts)

    def _check_mount_free(self, link: PilarLink) -> None:
        op = link.operation_active('mount')
        if op is not None and op.kind == 'power':
            raise PilarBusy(f"{op.name} in progress")

    def _queue_mount(self, link: PilarLink, name: str, run) -> None:
        """A mount command asked during a freeze is done right after it; the request returns at once. A newer
        mount command replaces one which still waits."""
        op = link.operation_active('mount')
        if op is not None:
            op.task.cancel()

        async def later():
            await link.wait_settled(self.settings.outage_after)
            await run(True)

        link.start_operation('mount', f'{name} (after a Pilar freeze)', later(), kind='queued')

    async def _mount_command(self, link: PilarLink, name: str, run) -> None:
        """``run(queued)`` now, or right after a freeze."""
        if link.quiet():
            self._queue_mount(link, name, run)
        else:
            await run(False)

    async def _before_slew(self, link: PilarLink, queued: bool) -> None:
        self._check_mount_free(link)
        await link.require_ready()
        if not queued:
            await link.cancel_operation('mount')   # a pending abortslew must not override the new target
        await link.restore_derotator()

    async def _min_altitude(self, link: PilarLink, may_ask: bool) -> Optional[float]:
        """90 - the zenith distance limit of Pilar; None when it is not known yet and Pilar cannot be asked now."""
        var = self.settings.slew_altaz.str('zd_limit')
        cached = link.cached_limit(var)
        if cached is not None:
            return 90.0 - cached
        if not may_ask and link.quiet():
            return None
        try:
            return 90.0 - await link.limit(var)
        except (PilarUnsupported, PilarDeviceError, PilarReplyError) as e:
            logger.warning(f"Pilar: no zenith distance limit ({e}); altitude not checked")
            return -90.0

    def _check_altitude(self, alt: float, min_alt: Optional[float]) -> None:
        low = min_alt if min_alt is not None else -90.0
        if not low <= alt <= 90.0:
            raise self._out_of_range('Altitude', alt, low, 90.0, ' deg')

    async def _put_slew_altaz(self, component, data, wait: bool = False) -> None:
        link = self._link(component)
        p = self.settings.slew_altaz
        az = self._as_float(self._param(data, 'Azimuth'), 'Azimuth') % 360.0
        alt = self._as_float(self._param(data, 'Altitude'), 'Altitude')
        min_alt = await self._min_altitude(link, may_ask=False)
        self._check_altitude(alt, min_alt)
        self._check_mount_free(link)

        async def run(queued: bool):
            if min_alt is None:
                self._check_altitude(alt, await self._min_altitude(link, may_ask=True))
            await self._before_slew(link, queued)
            link.tracking_wanted = False
            await link.set(p.str('azimuth'), az)
            await link.set(p.str('altitude'), alt)
            await link.set(p.str('track'), p.num('track_value'))
            link.note_motion('altaz', (az, alt))

        await self._mount_command(link, f'slew to az {az:.3f} alt {alt:.3f}', run)
        if wait:
            await self._wait_slew(link)
        return None

    async def _put_slew_equatorial(self, component, data, wait: bool = False) -> None:
        link = self._link(component)
        ra = self._as_float(self._param(data, 'RightAscension'), 'RightAscension')
        dec = self._as_float(self._param(data, 'Declination'), 'Declination')
        if not 0.0 <= ra <= 24.0:
            raise self._out_of_range('RightAscension', ra, 0.0, 24.0, ' h')
        if not -90.0 <= dec <= 90.0:
            raise self._out_of_range('Declination', dec, -90.0, 90.0, ' deg')
        epoch = self._epoch(component)
        self._check_mount_free(link)
        p = self.settings.slew_equatorial

        async def run(queued: bool):
            await self._before_slew(link, queued)
            await self._set_equatorial_target(link, ra % 24.0, dec, epoch)
            await link.set(p.str('track'), p.num('track_value'))
            link.tracking_wanted = True
            link.note_motion('track')

        await self._mount_command(link, f'slew to RA {ra:.5f} h Dec {dec:.4f}', run)
        if wait:
            await self._wait_slew(link)
        return None

    def _epoch(self, component) -> float:
        epoch = component.get_option_recursive('epoch')
        try:
            return float(epoch)
        except (TypeError, ValueError):
            raise PilarConfigError(f"{component.sys_id}: no numeric 'epoch' in the observatory configuration "
                                   f"(got {epoch!r})") from None

    async def _set_equatorial_target(self, link: PilarLink, ra: float, dec: float, epoch: float) -> None:
        p = self.settings.slew_equatorial
        for var in p.value('epoch'):
            await link.set(var, epoch)
        for var, value in (p.data.get('set_before') or {}).items():
            await link.set(var, value)
        await link.set(p.str('ra'), ra)
        await link.set(p.str('dec'), dec)
        link.equatorial_target = True

    async def _wait_slew(self, link: PilarLink) -> None:
        deadline = time.monotonic() + self.settings.sync_slew_timeout
        while link.slewing() or link.quiet():
            if link.unavailable():
                link.raise_unavailable()
            if time.monotonic() > deadline:
                raise PilarDeviceError(f"slew not ended in {self.settings.sync_slew_timeout:.0f} s")
            await asyncio.sleep(self.settings.poll_interval / 2)

    async def _put_abort_slew(self, component, data) -> None:
        link = self._link(component)
        op = link.operation_active('mount')
        if op is not None and op.kind == 'power':
            raise PilarBusy(f"{op.name} in progress")
        if op is not None and op.kind == 'abort':
            return None                                   # an abortslew is already on its way
        if not link.slewing():
            return None                                   # nothing to stop (ALPACA: no-op; tracking goes on)
        if op is not None:
            op.task.cancel()                              # a slew still waiting for the end of a freeze
        s = self.settings
        p = s.abort_slew

        async def abort():
            await link.wait_settled(s.outage_after)       # the position is known only after a freeze
            items = await link.read_many([s.state_azimuth, s.state_altitude], purpose='priority')
            az = link.number(items[s.state_azimuth.upper()], s.state_azimuth)
            alt = link.number(items[s.state_altitude.upper()], s.state_altitude)
            await link.set(p.str('azimuth'), az % 360.0, purpose='priority')
            await link.set(p.str('altitude'), alt, purpose='priority')
            await link.set(p.str('track'), p.num('track_value'), purpose='priority')
            link.tracking_wanted = False
            link.note_motion('abort', (az % 360.0, alt))
            logger.info(f"Pilar {link.address}: slew aborted, new target = current position "
                        f"az {az:.3f} alt {alt:.3f}")
            while link.motion_active():
                await asyncio.sleep(s.poll_interval / 2)

        link.tracking_wanted = False
        link.start_operation('mount', 'abortslew', abort(), kind='abort')
        return None

    async def _put_tracking(self, component, data) -> None:
        link = self._link(component)
        p = self.settings.tracking
        s = self.settings
        on = self._as_bool(self._param(data, 'Tracking'), 'Tracking')
        self._check_mount_free(link)
        if not on:
            link.tracking_wanted = False                  # also: no resuming after the freeze in progress

        async def run(queued: bool):
            if not on:
                await link.set(p.str('track'), p.num('off_value'))
                await link.restore_derotator()
                return
            await link.require_ready()
            track = await link.read(s.state_track, fresh=True)
            if track.ok and track.value == s.tracking_value:
                link.tracking_wanted = True
                return
            if not link.equatorial_target:
                # after an az/alt slew Pilar's target is horizontal: track the current position
                items = await link.read_many([p.str('current_ra'), p.str('current_dec')])
                ra = link.number(items[p.str('current_ra').upper()], p.str('current_ra'))
                dec = link.number(items[p.str('current_dec').upper()], p.str('current_dec'))
                await self._set_equatorial_target(link, ra, dec, self._epoch(component))
            await link.set(p.str('track'), p.num('on_value'))
            link.tracking_wanted = True
            link.note_motion('track')

        await self._mount_command(link, 'tracking on' if on else 'tracking off', run)
        return None

    async def _put_power(self, component, data, park: bool) -> None:
        link = self._link(component)
        s = self.settings
        on = not park
        wanted = s.ready_value if on else s.off_value
        op = link.operation_active('mount')
        cover = link.operation_active('cover')
        if op is not None and op.kind == 'power':
            raise PilarBusy(f"{op.name} in progress")
        if cover is not None:
            raise PilarBusy(f"{cover.name} in progress")
        if link.settled() and await link.read_number(s.state_ready, fresh=True) == wanted:
            return None
        if op is not None:
            op.task.cancel()                              # replaces a slew waiting for a freeze end, an abortslew
        name = 'power on (unpark)' if on else 'power off (park)'
        p = s.power

        async def power():
            await link.wait_settled(s.outage_after)
            if await link.read_number(s.state_ready, fresh=True) == wanted:
                return
            if not on:
                link.tracking_wanted = False
                try:
                    await link.restore_derotator()
                except Exception as e:
                    logger.warning(f"Pilar {link.address}: derotator settings not restored before park: {e}")
            try:
                await link.set(p.str('ready'), 1 if on else 0, purpose='long',
                               timeout=p.num('timeout_on' if on else 'timeout_off'))
            finally:
                link.forget(s.state_ready, s.state_track, s.state_azimuth, s.state_altitude,
                            self.settings.cover.str('position'))
            link.motion = None

        link.start_operation('mount', name, power(), kind='power')
        return None
