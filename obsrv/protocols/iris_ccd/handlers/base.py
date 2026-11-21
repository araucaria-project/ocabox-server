"""What every handler mixin of the IRIS CCD connector uses: the settings, the session of a component, parameters
of a request, options of a component; and the handlers common to the camera and the filter wheel (``connected``)."""
import logging
import math

from obcom.data_colection.address import AddressError

from obsrv.protocols.iris_ccd.iris_ccd_exceptions import IrisConfigError
from obsrv.protocols.iris_ccd.iris_ccd_session import IrisSession
from obsrv.protocols.iris_ccd.iris_ccd_settings import ProtocolSettings

logger = logging.getLogger('iris_ccd_connector')     # one logger for the whole IRIS CCD connector


class HandlerBase:
    """Base of the handler mixins; :class:`IrisCcdConnector` gives ``settings`` and :meth:`_session`."""

    settings: ProtocolSettings

    def _session(self, component) -> IrisSession:
        raise NotImplementedError

    # ------------------------------------------------------------------ parameters and options

    @staticmethod
    def _param(data: dict, name: str, required: bool = True):
        for key, value in (data or {}).items():
            if str(key).lower() == name.lower():
                return value
        if required:
            raise AddressError(code=1003, message=f"Missing parameter {name!r}")
        return None

    @staticmethod
    def _as_float(value, name: str) -> float:
        try:
            if isinstance(value, bool):
                raise TypeError
            result = float(value)
        except (TypeError, ValueError):
            raise AddressError(code=1003, message=f"Parameter {name!r} must be a number, got {value!r}")
        if not math.isfinite(result):
            raise AddressError(code=1003, message=f"Parameter {name!r} must be finite, got {value!r}")
        return result

    @staticmethod
    def _as_int(value, name: str) -> int:
        try:
            if isinstance(value, bool):
                raise TypeError
            if isinstance(value, float) and not value.is_integer():
                raise ValueError
            return int(value)
        except (TypeError, ValueError):
            raise AddressError(code=1003, message=f"Parameter {name!r} must be an integer, got {value!r}")

    @staticmethod
    def _as_bool(value, name: str) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in ('true', 'false'):
            return value.strip().lower() == 'true'
        raise AddressError(code=1003, message=f"Parameter {name!r} must be true or false, got {value!r}")

    @staticmethod
    def _option(component, name: str):
        value = component.get_option_recursive(name)
        if value is None:
            raise IrisConfigError(f"{component.sys_id}: no {name!r} in the configuration of the component")
        return value

    def _option_getter(self, name: str):
        async def getter(component, data):
            return self._option(component, name)
        return getter

    def _info_getter(self, kind: str, key: str):
        async def getter(component, data):
            return self.settings.info[kind][key]
        return getter

    # ------------------------------------------------------------------ common

    async def _get_connected(self, component, data) -> bool:
        """``sync``: iriscom answers and the camera electronics respond. While a long operation runs iriscom is
        evidently alive, and it would not answer before the operation ends."""
        session = self._session(component)
        if session.busy_with is not None:
            return True
        reply = await session.command(self.settings.commands['sync'], self.settings.timeouts['command'])
        if not reply.ok:
            logger.warning(f"IRIS CCD ({session.address}): sync answered E{reply.code}: {reply.message}")
        return reply.ok

    async def _put_connected(self, component, data) -> None:
        if not self._as_bool(self._param(data, 'Connected'), 'Connected'):
            return None
        session = self._session(component)
        if session.busy_with is not None:
            return None
        reply = await session.command(self.settings.commands['sync'], self.settings.timeouts['command'])
        reply.require_ok()
        return None
