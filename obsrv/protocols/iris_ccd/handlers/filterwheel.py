"""Handlers of the filter wheel: filters and focus offsets from the configuration of the component, the position
(kept by TIC, iriscom reports nothing), moves and rehome. A move is a long operation of the session."""
import logging
from typing import Dict, Tuple

from obcom.data_colection.address import AddressError
from obcom.data_colection.coded_error import TreeStructureError

from obsrv.protocols.iris_ccd.iris_ccd_exceptions import IrisConfigError
from obsrv.protocols.iris_ccd.iris_ccd_session import IrisSession
from obsrv.protocols.iris_ccd.handlers.base import HandlerBase

logger = logging.getLogger('iris_ccd_connector')     # one logger for the whole IRIS CCD connector


class FilterWheelHandlers(HandlerBase):
    """Mixin of :class:`IrisCcdConnector`: handlers of the ``filterwheel`` component."""

    def _filters(self, component) -> Dict[str, int]:
        filters = self._option(component, 'filters')
        if not isinstance(filters, dict) or not filters:
            raise IrisConfigError(f"{component.sys_id}: 'filters' must map filter names to slot numbers")
        out = {}
        for name, slot in filters.items():
            if isinstance(slot, bool) or not isinstance(slot, int) or \
                    not self.settings.first_slot <= slot <= self.settings.last_slot:
                raise IrisConfigError(f"{component.sys_id}: filter {name!r} has slot {slot!r}, expected an integer "
                                      f"{self.settings.first_slot}..{self.settings.last_slot}")
            out[str(name)] = slot
        return out

    async def _get_names(self, component, data) -> Dict[str, int]:
        return self._filters(component)

    async def _get_focusoffsets(self, component, data) -> list:
        """Focus offsets indexed like ``position``, i.e. by the iriscom slot number: index 0 (no slot in iriscom)
        and slots without an entry in ``focus_offsets`` get ``default_focus_offset`` of the protocol settings."""
        filters = self._filters(component)
        configured = component.get_option_recursive('focus_offsets') or {}
        if not isinstance(configured, dict):
            raise IrisConfigError(f"{component.sys_id}: 'focus_offsets' must map filter names to offsets")
        offsets = [self.settings.default_focus_offset] * (self.settings.last_slot + 1)
        for name, offset in configured.items():
            if str(name) not in filters:
                raise IrisConfigError(f"{component.sys_id}: focus offset for {name!r}, which is not in 'filters'")
            if isinstance(offset, bool) or not isinstance(offset, int):
                raise IrisConfigError(f"{component.sys_id}: focus offset of {name!r} must be an integer, "
                                      f"got {offset!r}")
            offsets[filters[str(name)]] = offset
        return offsets

    async def _get_position(self, component, data) -> int:
        position = self._session(component).filter_position
        return self.settings.unknown_position if position is None else position

    def _slot(self, component, value) -> Tuple[int, str]:
        filters = self._filters(component)
        if isinstance(value, str) and value in filters:
            return filters[value], value
        slot = self._as_int(value, 'Position')
        for name, s in filters.items():
            if s == slot:
                return slot, name
        raise AddressError(code=1003, message=f"Slot {slot} has no filter in the configuration; available: "
                                              f"{', '.join(f'{n}={s}' for n, s in filters.items())}")

    async def _put_position(self, component, data) -> dict:
        slot, name = self._slot(component, self._param(data, 'Position'))
        session = self._session(component)
        if self.settings.skip_same_position and session.filter_position == slot:
            logger.info(f"IRIS CCD ({session.address}): filter {name} ({slot}) already in place, not moved")
            return {'position': slot, 'name': name, 'moved': False}
        return await session.operation('filter move', lambda: self._move_filter(session, slot, name))

    async def _move_filter(self, session: IrisSession, slot: int, name: str) -> dict:
        s = self.settings
        session.filter_position = None
        logger.info(f"IRIS CCD ({session.address}): moving filter wheel to {name} ({slot})")
        reply = await session.exchange(f"{s.commands['filter']} {slot}", s.timeouts['filter_move'])
        reply.require_ok()
        session.filter_position = slot
        logger.info(f"IRIS CCD ({session.address}): filter wheel at {name} ({slot})")
        return {'position': slot, 'name': name, 'moved': True}

    async def _put_rehome(self, component, data) -> dict:
        if not self.settings.rehome_enabled:
            raise TreeStructureError(code=3002, severity=TreeStructureError.SEVERITY_CRITICAL,
                                     message="Filter wheel rehome is disabled (filterwheel.rehome_enabled in "
                                             "iris_ccd_config.yml)")
        session = self._session(component)
        return await session.operation('filter rehome', lambda: self._rehome(session))

    async def _rehome(self, session: IrisSession) -> dict:
        s = self.settings
        session.filter_position = None
        logger.info(f"IRIS CCD ({session.address}): filter wheel rehome started")
        reply = await session.exchange(f"{s.commands['filter']} {s.commands['filter_rehome']}",
                                       s.timeouts['filter_rehome'])
        reply.require_ok()
        logger.info(f"IRIS CCD ({session.address}): filter wheel rehome done, no filter selected")
        return {'position': s.unknown_position, 'name': None, 'moved': True}
