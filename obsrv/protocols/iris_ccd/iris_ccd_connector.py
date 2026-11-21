"""IRIS-CCD connector: the IRIS camera and its filter wheel, both served by one ``iriscom`` process over UDP.

How iriscom talks (iriscom.c, version 3.5 AF4.43, verified against the instrument):

* a request is one UDP datagram of exactly ``packet_size`` bytes: the command text padded with NUL bytes;
  a datagram of any other length is dropped without an answer;
* the reply is one datagram of the same size: ``"<UTC time> <echo of the command>\\n<status> <message>"``,
  where the status is ``OKAY`` or ``E<code>`` (100 syntax, 201 file, 202 xfer, 300 hardware, 301 filter);
* iriscom serves one command at a time. While ``go`` (exposure) or ``filter`` (wheel move) runs, nothing else
  is answered; such a command waits in the socket until the running one ends;
* there is no status command: the state of the camera and the position of the filter wheel are known to TIC
  only from the commands TIC itself sent.

The connector forwards a client's command to iriscom, waits for the reply and returns it (the same model as the
ALPACA connector). One :class:`IrisSession` per iriscom address is shared by every component configured with
that address (camera and filter wheel), so the two never talk to iriscom at the same time:

* a short command (``sync``, ``et``, ``clear``, ``xfer``) waits for a running short command;
* while a long operation (exposure, filter move) runs, every request that needs iriscom gets ``4008`` (busy)
  at once; values that TIC keeps itself (``camerastate``, ``position`` ...) are answered at once;
* a long operation always runs to the end, also when the client stops waiting: iriscom finishes it anyway,
  and TIC keeps its result (``lastimage``, ``position``).

Protocol settings are in ``iris_ccd_config.yml``; settings of a device (address, filters, detector, the NATS
subject of the detector temperature) in the observatory configuration of its component.

Code (``obsrv/protocols/iris_ccd``):

* ``iris_ccd_connector.py``: :class:`IrisCcdConnector`: the Ocabox connector interface, the tables of handlers,
  IRIS CCD errors translated to Ocabox codes, the sessions;
* ``iris_ccd_session.py``: :class:`IrisSession`: replies of iriscom, the UDP endpoint, short commands and long
  operations, :class:`CameraState`;
* ``handlers/``: the handlers, one mixin of :class:`IrisCcdConnector` per device kind (``camera``,
  ``filterwheel``; ``base``: parameters, options, ``connected``);
* ``iris_ccd_telemetry.py``: :class:`TelemetryValue`, the detector temperature from NATS;
* ``iris_ccd_settings.py``: ``iris_ccd_config.yml`` read and checked; ``iris_ccd_exceptions.py``: the errors.
"""
import asyncio
import logging
from typing import Callable, Dict, Optional

from obcom.data_colection.address import AddressError
from obcom.data_colection.coded_error import BaseCodedError, TreeOtherError, TreeStructureError
from obcom.data_colection.value import TreeValueError
from serverish.messenger import get_callbacksubscriber

from obsrv.protocols.alpaca.alpaca_connector import Connector
from obsrv.protocols.iris_ccd.handlers import CameraHandlers, FilterWheelHandlers
from obsrv.protocols.iris_ccd.iris_ccd_exceptions import (IrisBusy, IrisConfigError, IrisDeviceError,
                                                          IrisProtocolError, IrisTimeout)
from obsrv.protocols.iris_ccd.iris_ccd_session import CameraState, IrisReply, IrisSession, parse_reply, split_reply
from obsrv.protocols.iris_ccd.iris_ccd_settings import _CAMERA, _FILTERWHEEL, ProtocolSettings, load_protocol_config
from obsrv.protocols.iris_ccd.iris_ccd_telemetry import TelemetryValue

#: what other modules and the tests import from here (also from before the split into modules)
__all__ = ['IrisCcdConnector', 'IrisSession', 'IrisReply', 'CameraState', 'TelemetryValue', 'ProtocolSettings',
           'load_protocol_config', 'parse_reply', 'split_reply',
           'IrisTimeout', 'IrisProtocolError', 'IrisDeviceError', 'IrisBusy', 'IrisConfigError']

logger = logging.getLogger(__name__.rsplit('.')[-1])

#: Failures of the link to iriscom (no answer, ICMP port unreachable, socket errors) -> 4005.
#: ``TimeoutError`` (and ``IrisTimeout``) is an ``OSError`` subclass.
_TRANSPORT_ERRORS = (OSError, asyncio.TimeoutError)


class IrisCcdConnector(CameraHandlers, FilterWheelHandlers, Connector):
    """Connector of components with ``protocol: iris_ccd`` (kinds ``camera`` and ``filterwheel``)."""

    #: iriscom address -> session, shared by every connector (camera and filter wheel are separate components
    #: with separate connectors, but one iriscom).
    _sessions: Dict[str, IrisSession] = {}
    #: NATS subject -> its last value.
    _telemetry: Dict[str, TelemetryValue] = {}
    #: Creates a serverish callback subscriber; replaced in tests.
    subscriber_factory = staticmethod(get_callbacksubscriber)

    def __init__(self, settings: Optional[dict] = None, **kwargs):
        super().__init__(**kwargs)
        cfg = settings if settings is not None else load_protocol_config()
        try:
            self.settings = ProtocolSettings(cfg)
        except IrisConfigError as e:
            raise RuntimeError(str(e)) from e
        self._get_handlers: Dict[str, Dict[str, Callable]] = {
            _CAMERA: {
                'name': self._info_getter(_CAMERA, 'name'),
                'description': self._info_getter(_CAMERA, 'description'),
                'connected': self._get_connected,
                'camerastate': self._get_camerastate,
                'imageready': self._get_imageready,
                'percentcompleted': self._get_percentcompleted,
                'lastexposureduration': self._get_lastexposureduration,
                'lastexposurestarttime': self._get_lastexposurestarttime,
                'lastimage': self._get_lastimage,
                'ccdtemperature': self._get_ccdtemperature,
                'sensorname': self._option_getter('sensorname'),
                'cameraxsize': self._option_getter('cameraxsize'),
                'cameraysize': self._option_getter('cameraysize'),
                'exposuremin': self._option_getter('exposuremin'),
                'exposuremax': self._option_getter('exposuremax'),
                'numx': self._get_numx,
                'numy': self._get_numy,
                'xfer': self._get_xfer,
                **{name: self._fixed_getter(name) for name in self.settings.camera_fixed},
            },
            _FILTERWHEEL: {
                'name': self._info_getter(_FILTERWHEEL, 'name'),
                'description': self._info_getter(_FILTERWHEEL, 'description'),
                'connected': self._get_connected,
                'position': self._get_position,
                'names': self._get_names,
                'focusoffsets': self._get_focusoffsets,
            },
        }
        self._put_handlers: Dict[str, Dict[str, Callable]] = {
            _CAMERA: {
                'connected': self._put_connected,
                'startexposure': self._put_startexposure,
                'clear': self._put_clear,
                'xfer': self._put_xfer,
                'binx': self._frame_setter('binx'),
                'biny': self._frame_setter('biny'),
                'startx': self._frame_setter('startx'),
                'starty': self._frame_setter('starty'),
                'numx': self._frame_setter('numx'),
                'numy': self._frame_setter('numy'),
            },
            _FILTERWHEEL: {
                'connected': self._put_connected,
                'position': self._put_position,
                'rehome': self._put_rehome,
            },
        }
        logger.info('IrisCcdConnector created')

    # ------------------------------------------------------------------ Connector interface

    async def get(self, component: 'Component', variable: str, kind=None, **data):
        return await self._serve('GET', self._get_handlers, component, variable, data)

    async def put(self, component: 'Component', variable: str, kind=None, **data):
        return await self._serve('PUT', self._put_handlers, component, variable, data)

    async def call(self, component: 'Component', function: str, **data):
        return await self.put(component, function, **data)

    async def _serve(self, operation: str, handlers: Dict[str, Dict[str, Callable]], component, variable: str,
                     data: dict):
        handler = handlers.get(component.kind, {}).get(variable.lower())
        if handler is None:
            raise TreeStructureError(code=3002,
                                     message=f"{operation} {variable!r} is not implemented on {component.kind} "
                                             f"({component.sys_id}) by the IRIS CCD protocol",
                                     severity=TreeStructureError.SEVERITY_CRITICAL)
        where = f"{operation} {component.kind}.{variable}"
        try:
            return await handler(component, data)
        except (BaseCodedError, asyncio.CancelledError):
            raise
        except IrisBusy as e:
            raise TreeOtherError(code=4008, severity=TreeOtherError.SEVERITY_TEMPORARY,
                                 message=f"IRIS CCD busy ({e.operation} in progress), {where} refused")
        except IrisDeviceError as e:
            logger.warning(f"IRIS CCD device error on {where}: {e.message}")
            raise TreeOtherError(code=4009, severity=TreeOtherError.SEVERITY_NORMAL,
                                 message=f"IRIS CCD device error on {where}: {e.message}", device_errno=e.code)
        except IrisProtocolError as e:
            logger.error(f"IRIS CCD reply not understood on {where}: {e}")
            raise TreeValueError(code=2002, message=f"IRIS CCD reply not understood on {where}: {e}")
        except IrisConfigError as e:
            logger.error(f"IRIS CCD configuration error on {where}: {e}")
            raise TreeStructureError(code=3001, message=str(e), severity=TreeStructureError.SEVERITY_CRITICAL)
        except (TypeError, ValueError) as e:
            raise AddressError(code=1003, message=f"Wrong parameters for {where}: {e}")
        except _TRANSPORT_ERRORS as e:
            logger.warning(f"IRIS CCD unreachable on {where}: {e}")
            raise TreeOtherError(code=4005, severity=TreeOtherError.SEVERITY_NORMAL,
                                 message=f"IRIS CCD unreachable on {where}: {e}")

    # ------------------------------------------------------------------ sessions

    def _session(self, component) -> IrisSession:
        address = component.get_option_recursive('address')
        if not address:
            raise IrisConfigError(f"{component.sys_id}: no 'address' (host:port of iriscom) in the configuration")
        host, sep, port = str(address).rpartition(':')
        if not sep or not host or not port.isdigit():
            raise IrisConfigError(f"{component.sys_id}: address {address!r} is not host:port")
        key = f'{host}:{int(port)}'
        session = self._sessions.get(key)
        if session is None or session.loop is not asyncio.get_running_loop():
            if session is not None:
                session.close()
            session = IrisSession(host, int(port), self.settings)
            self._sessions[key] = session
        return session

    @classmethod
    def close_all(cls) -> None:
        """Close every UDP endpoint and forget sessions and telemetry (server stop, tests)."""
        for session in cls._sessions.values():
            session.close()
        cls._sessions.clear()
        for telemetry in cls._telemetry.values():
            if telemetry.reading_task is not None and not telemetry.reading_task.done():
                telemetry.reading_task.cancel()
        cls._telemetry.clear()

    @classmethod
    async def aclose_all(cls) -> None:
        subscribers = [t.subscriber for t in cls._telemetry.values() if t.subscriber is not None]
        cls.close_all()
        for subscriber in subscribers:
            try:
                await subscriber.close()
            except Exception as e:
                logger.debug(f"Closing a NATS subscriber failed: {e}")
