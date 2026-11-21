"""Procedures of the mirror covers (``covercalibrator``): the state read from the position, open and close in the
background."""
from obsrv.protocols.pilar.pilar_exceptions import PilarBusy, PilarTransient
from obsrv.protocols.pilar.procedures.base import ProcedureBase


class CoverProcedures(ProcedureBase):
    """Mixin of :class:`PilarConnector`: procedures of the mirror covers."""

    async def _get_cover_state(self, component, data) -> int:
        link = self._link(component)
        p = self.settings.cover
        states = p.section('states')
        if link.operation_active('cover') is not None:
            return states.int('moving')
        item = await link.read(p.str('position'))
        if not item.ok and item.special.kind == 'FAILED':
            if link.quiet():
                raise PilarTransient("cover position FAILED just after a Pilar freeze")
            return states.int('unknown')
        position = link.number(item, p.str('position'))
        tolerance = p.num('tolerance')
        if abs(position - p.num('open_value')) <= tolerance:
            return states.int('open')
        if abs(position - p.num('closed_value')) <= tolerance:
            return states.int('closed')
        return states.int('moving')

    async def _put_cover(self, component, data, open: bool) -> None:
        link = self._link(component)
        p = self.settings.cover
        busy = link.operation_active('cover')
        mount = link.operation_active('mount')
        if busy is not None:
            raise PilarBusy(f"{busy.name} in progress")
        if mount is not None and mount.kind == 'power':
            raise PilarBusy(f"{mount.name} in progress")
        checked = link.settled()
        if checked:
            await link.require_ready()                    # 4009 now; during a freeze: checked after it
        target = p.num('open_value' if open else 'closed_value')
        name = 'open cover' if open else 'close cover'

        async def move():
            if not checked:
                await link.wait_settled(self.settings.outage_after)
                await link.require_ready()
            try:
                await link.set(p.str('target'), target, purpose='long', timeout=p.num('timeout'))
            finally:
                link.forget(p.str('position'))

        link.start_operation('cover', name, move(), target)
        return None
