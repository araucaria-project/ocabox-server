"""Pilar connector: mount, mirror covers, focuser and derotator of IRIS through Pilar 3.3 (TPL2 / OpenTSI).

What the connector does about the ways of Pilar (measured on the instrument 18-19.11.2026; numbers and the
mapping of Ocabox API methods are in ``pilar_config.yml``):

* one :class:`PilarLink` per Pilar address is shared by every component with that address (mount, covers,
  focuser, the rotator below the mount): one pool of connections (Pilar takes at most 10 clients) and one
  watcher;
* the watcher reads the state (``READY_STATE``, ``TRACK``, the mount position and every variable a client
  reads) in one GET every ``poll_interval``; client reads are answered from this snapshot;
* freezes: every 58-82 s Pilar answers nothing for 16-18 s, motion goes on, and at the end ``TRACK`` drops to 0.
  During a freeze a read fails at once with 4005 TEMPORARY and a command waits for its end; for
  ``post_freeze_guard`` after it ``FAILED`` and ``READY_STATE=0`` are taken as transient; tracking which the
  client asked for is resumed; a freeze longer than ``outage_after`` is an outage (4005 NORMAL, no resuming);
* ``TELESCOPE.STOP``, ``ABORT`` and ``TRACK=0`` do not stop a slew: ``abortslew`` sends a new target, the
  current position;
* the focuser moves only via the pointing: a ``SYNCMODE`` bit for the time of the move + ``POSITION`` +
  ``TRACK=4``;
* the mount has priority: a focuser or rotator move asked during a slew waits for its end; a slew asked during
  such a move goes at once, and the move goes on after it;
* long operations (park, unpark, covers, focuser, rotator, abortslew) run in the background: the request
  returns at once (``slewtoaltaz`` / ``slewtocoordinates`` wait for the end of the slew) and a second command for
  a busy device gets 4008.

Code (``obsrv/protocols/pilar``):

* ``pilar_connector.py``: :class:`PilarConnector`: the Ocabox connector interface, the mapping of
  ``pilar_config.yml`` (TPL2 variables and procedures), Pilar errors translated to Ocabox codes, the links;
* ``pilar_link.py``: :class:`PilarLink`: connections, the watcher and its snapshot, freezes, the motion of the
  mount, background operations;
* ``procedures/``: the procedures of the mapping, one mixin of :class:`PilarConnector` per device (``mount``,
  ``cover``, ``focuser``, ``rotator``; ``base``: parameters of requests, limits);
* ``pilar_settings.py``: ``pilar_config.yml`` read and checked; ``pilar_exceptions.py``: errors of the connector;
* ``pilar_tpl2.py``: the TPL2 client (lines, replies, the pool of connections).
"""
import asyncio
import logging
from typing import Callable, Dict, Iterable, Optional, Tuple

from obcom.data_colection.address import AddressError
from obcom.data_colection.coded_error import BaseCodedError, TreeOtherError, TreeStructureError
from obcom.data_colection.value import TreeValueError

from obsrv.protocols.alpaca.alpaca_connector import Connector
from obsrv.protocols.pilar.pilar_exceptions import (PilarBusy, PilarConfigError, PilarDeviceError, PilarError,
                                                    PilarReplyError, PilarTransient, PilarUnavailable,
                                                    PilarUnsupported)
from obsrv.protocols.pilar.pilar_link import PilarLink
from obsrv.protocols.pilar.pilar_settings import MappingEntry, PilarSettings, load_protocol_config
from obsrv.protocols.pilar.pilar_tpl2 import (Tpl2Busy, Tpl2CommandError, Tpl2LinkError, Tpl2ProtocolError,
                                              Tpl2Timeout)
from obsrv.protocols.pilar.procedures import (CoverProcedures, FocuserProcedures, MountProcedures,
                                              RotatorProcedures)

#: what other modules and the tests import from here (also from before the split into modules)
__all__ = ['PilarConnector', 'PilarLink', 'PilarSettings', 'load_protocol_config',
           'PilarError', 'PilarConfigError', 'PilarUnsupported', 'PilarTransient', 'PilarUnavailable', 'PilarBusy',
           'PilarDeviceError', 'PilarReplyError']

logger = logging.getLogger(__name__.rsplit('.')[-1])


#: procedure name -> (method of PilarConnector, 'get' or 'put')
_PROCEDURES: Dict[str, Tuple[str, str]] = {
    'connected': ('_get_connected', 'get'),
    'tracking': ('_get_tracking', 'get'),
    'slewing': ('_get_slewing', 'get'),
    'at_park': ('_get_at_park', 'get'),
    'cover_state': ('_get_cover_state', 'get'),
    'focuser_position': ('_get_focuser_position', 'get'),
    'focuser_moving': ('_get_focuser_moving', 'get'),
    'focuser_maxstep': ('_get_focuser_maxstep', 'get'),
    'rotator_target': ('_get_rotator_target', 'get'),
    'rotator_moving': ('_get_rotator_moving', 'get'),
    'motor_status': ('_get_motor_status', 'get'),
    'connect': ('_put_connect', 'put'),
    'power': ('_put_power', 'put'),
    'slew_altaz': ('_put_slew_altaz', 'put'),
    'slew_equatorial': ('_put_slew_equatorial', 'put'),
    'abort_slew': ('_put_abort_slew', 'put'),
    'set_tracking': ('_put_tracking', 'put'),
    'cover': ('_put_cover', 'put'),
    'focuser_move': ('_put_focuser_move', 'put'),
    'rotator_move': ('_put_rotator_move', 'put'),
}


#: requests of the device tree which name what to do in a parameter (put('action', Action=...),
#: put('commandstring', Command=...)): looked up in ``actions`` / ``fixed`` of the mapping by that name
_ACTION_REQUESTS = {'action': 'Action', 'commandstring': 'Command', 'commandblind': 'Command',
                    'commandbool': 'Command'}


class PilarConnector(MountProcedures, CoverProcedures, FocuserProcedures, RotatorProcedures, Connector):
    """Connector of components with ``protocol: pilar`` (kinds ``telescope``, ``covercalibrator``, ``focuser``,
    ``rotator``). What a method does is given by the mapping in ``pilar_config.yml``."""

    #: Pilar address -> link, shared by every connector (mount, covers and focuser are separate components with
    #: separate connectors, but one Pilar).
    _links: Dict[str, PilarLink] = {}

    def __init__(self, settings: Optional[dict] = None, **kwargs):
        super().__init__(**kwargs)
        cfg = settings if settings is not None else load_protocol_config()
        try:
            self.settings = PilarSettings(cfg, _PROCEDURES)
        except PilarConfigError as e:
            raise RuntimeError(f"Pilar connector configuration: {e}") from e
        logger.info('PilarConnector created')

    # ------------------------------------------------------------------ Connector interface

    async def get(self, component: 'Component', variable: str, kind=None, **data):
        return await self._serve('GET', component, variable, data)

    async def put(self, component: 'Component', variable: str, kind=None, **data):
        return await self._serve('PUT', component, variable, data)

    async def call(self, component: 'Component', function: str, **data):
        return await self.put(component, function, **data)

    async def subscribe(self, variables: Iterable[Tuple[str, str]], callback: Callable):
        raise NotImplementedError("Pilar protocol does not support subscriptions")

    def _not_implemented(self, operation: str, component, variable: str) -> TreeStructureError:
        return TreeStructureError(code=3002,
                                  message=f"{operation} {variable!r} is not implemented on {component.kind} "
                                          f"({component.sys_id}) by the Pilar protocol",
                                  severity=TreeStructureError.SEVERITY_CRITICAL)

    async def _serve(self, operation: str, component, variable: str, data: dict):
        where = f"{operation} {component.kind}.{variable}"
        try:
            mapping = self.settings.components.get(component.kind)
            if mapping is None:
                raise self._not_implemented(operation, component, variable)
            name = variable.lower()
            mode = operation.lower()
            if operation == 'GET':
                if name in mapping.fixed:
                    return mapping.fixed[name]
                entry = mapping.get.get(name)
            elif name in _ACTION_REQUESTS:
                action = str(self._param(data, _ACTION_REQUESTS[name]))
                if action.lower() in mapping.fixed:
                    return mapping.fixed[action.lower()]
                entry = mapping.actions.get(action.lower())
                if entry is None:
                    raise self._not_implemented(operation, component, f'action {action}')
                mode = 'get' if entry.variable is not None else _PROCEDURES[entry.procedure][1]
                data = {}
            else:
                entry = mapping.put.get(name)
            if entry is None:
                raise self._not_implemented(operation, component, variable)
            return await self._run(entry, mode, component, data)
        except (BaseCodedError, asyncio.CancelledError):
            raise
        except (PilarBusy, Tpl2Busy) as e:
            raise TreeOtherError(code=4008, severity=TreeOtherError.SEVERITY_TEMPORARY,
                                 message=f"Pilar busy, {where} refused: {e}")
        except PilarDeviceError as e:
            logger.warning(f"Pilar device error on {where}: {e}")
            raise TreeOtherError(code=4009, severity=TreeOtherError.SEVERITY_NORMAL,
                                 message=f"Pilar error on {where}: {e}")
        except PilarUnsupported as e:
            raise TreeStructureError(code=3002, severity=TreeStructureError.SEVERITY_CRITICAL,
                                     message=f"{where}: {e}")
        except PilarConfigError as e:
            logger.error(f"Pilar configuration error on {where}: {e}")
            raise TreeStructureError(code=3001, message=str(e), severity=TreeStructureError.SEVERITY_CRITICAL)
        except (PilarTransient, Tpl2Timeout) as e:
            logger.debug(f"Pilar transient failure on {where}: {e}")
            raise TreeOtherError(code=4005, severity=TreeOtherError.SEVERITY_TEMPORARY,
                                 message=f"Pilar did not answer on {where}: {e}")
        except (PilarUnavailable, Tpl2LinkError, OSError) as e:
            raise TreeOtherError(code=4005, severity=TreeOtherError.SEVERITY_NORMAL,
                                 message=f"Pilar unreachable on {where}: {e}")
        except (Tpl2ProtocolError, Tpl2CommandError, PilarReplyError) as e:
            logger.error(f"Pilar reply not usable on {where}: {e}")
            raise TreeValueError(code=2002, message=f"Pilar reply not usable on {where}: {e}")
        except (TypeError, ValueError) as e:
            raise AddressError(code=1003, message=f"Wrong parameters for {where}: {e}")

    async def _run(self, entry: MappingEntry, mode: str, component, data: dict):
        if entry.variable is not None:
            link = self._link(component)
            var = self._variable(entry.variable, component)
            if mode == 'get':
                return link.value(await link.read(var), var)
            if len(data) != 1:
                raise AddressError(code=1003, message=f"SET {var} needs exactly one parameter, got {len(data)}")
            value = next(iter(data.values()))
            if link.quiet():
                async def later():
                    await link.wait_settled(self.settings.outage_after)
                    await link.set(var, value)
                link.start_operation(f'set:{var}', f'SET {var} (after a Pilar freeze)', later())
            else:
                await link.set(var, value)
            return None
        method_name, _ = _PROCEDURES[entry.procedure]
        return await getattr(self, method_name)(component, data, **entry.options)

    # ------------------------------------------------------------------ links

    def _link(self, component) -> PilarLink:
        address = component.get_option_recursive('address')
        if not address:
            raise PilarConfigError(f"{component.sys_id}: no 'address' (host:port of Pilar) in the configuration")
        host, sep, port = str(address).rpartition(':')
        if not sep or not host or not port.isdigit():
            raise PilarConfigError(f"{component.sys_id}: address {address!r} is not host:port")
        key = f'{host}:{int(port)}'
        link = self._links.get(key)
        if link is None or link.closed or link.loop is not asyncio.get_running_loop():
            if link is not None:
                link.close()
            link = PilarLink(host, int(port), self.settings)
            self._links[key] = link
        return link

    @classmethod
    def close_all(cls) -> None:
        """Close every link (server stop, tests)."""
        for link in cls._links.values():
            link.close()
        cls._links.clear()

    @classmethod
    async def aclose_all(cls) -> None:
        links = list(cls._links.values())
        cls._links.clear()
        for link in links:
            try:
                await link.aclose()
            except Exception as e:
                logger.debug(f"Closing Pilar link {link.address} failed: {e}")
