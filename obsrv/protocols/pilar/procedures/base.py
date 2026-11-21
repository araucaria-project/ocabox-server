"""What every procedure mixin of the Pilar connector uses: the settings, the link of a component, parameters of a
request, limits of an axis."""
import math
from typing import Optional, Tuple

from obcom.data_colection.address import AddressError

from obsrv.protocols.pilar.pilar_link import PilarLink
from obsrv.protocols.pilar.pilar_settings import PilarSettings

#: in a TPL2 variable of the mapping: replaced by the device number of the component
_DEVICE_NUMBER = '{device_number}'


class ProcedureBase:
    """Base of the procedure mixins; :class:`PilarConnector` gives ``settings`` and :meth:`_link`."""

    settings: PilarSettings

    def _link(self, component) -> PilarLink:
        raise NotImplementedError

    @staticmethod
    def _variable(template: str, component) -> str:
        return template.replace(_DEVICE_NUMBER, str(component.device_nr))

    # ------------------------------------------------------------------ parameters

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
    def _as_bool(value, name: str) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in ('true', 'false'):
            return value.strip().lower() == 'true'
        raise AddressError(code=1003, message=f"Parameter {name!r} must be true or false, got {value!r}")

    @staticmethod
    def _out_of_range(name: str, value: float, low: float, high: float, unit: str = '') -> AddressError:
        return AddressError(code=1003, message=f"Parameter {name!r} = {value:g}{unit} is outside "
                                               f"{low:g}..{high:g}{unit}")

    # ------------------------------------------------------------------ limits

    async def _limits(self, link: PilarLink, low_var: str, high_var: str) -> Optional[Tuple[float, float]]:
        """Limits of an axis: known, read now, or None when Pilar cannot be asked now (checked later)."""
        low, high = link.cached_limit(low_var), link.cached_limit(high_var)
        if low is not None and high is not None:
            return low, high
        if link.quiet():
            return None
        return await link.limit(low_var), await link.limit(high_var)
