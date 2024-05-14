import asyncio
import logging
from abc import ABC

from pyaraucaria.coordinates import deg_to_decimal_deg

from obcom.comunication.comunication_error import CommunicationRuntimeError, CommunicationTimeoutError
from obsrv.comunication.internal_client_api import InternalClientAPI
from obcom.data_colection.address import Address
from obsrv.data_colection.alpaca_api.standard_telescope_components import StandardTelescopeComponents

logger = logging.getLogger(__name__.rsplit('.')[-1])


class Resource(ABC):
    RESOURCE_NAME = ""

    def __init__(self, source_name: str, resource_name: str, nr: int, target_request, properties: dict = None,
                 address_path: str = "", **kwargs):
        if properties is None:
            properties = {}
        self._source_name: str = source_name
        self._resource_name: str = resource_name
        self._nr: int = nr
        self._properties: dict = properties  # properties, from settings and more
        self._telescope_id = self._properties.get("observatory_name", "ID_UNDEFINED")  # telescope id/unique name
        self._lock = asyncio.Lock()
        if address_path:
            a = ".".join([address_path, self.source_name])
        else:
            a = ".".join([self.source_name])
        self._tree_address_path = Address.as_address(a)
        self._target = target_request
        self._correctly_load_data = True
        super().__init__(**kwargs)

    @property
    def telescope_id(self):
        return self._telescope_id

    @property
    def nr(self) -> int:
        """number resource, used in alpaca request"""
        return self._nr

    @property
    def name(self) -> str:
        """Name in alpaca standard, can repeat for example if telescope has two filter well"""
        return self.RESOURCE_NAME

    @property
    def adr(self) -> str:
        """tree full path address"""
        return self._tree_address_path.__str__()

    @property
    def source_name(self) -> str:
        """tree address name"""
        return self._source_name

    @property
    def id_name(self) -> str:
        """Custom name, is unique"""
        return self._resource_name

    @property
    def properties(self) -> dict:
        return self._properties

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    async def __aenter__(self):
        await self._lock.acquire()
        return None

    async def __aexit__(self, exc_type, exc, tb):
        self._lock.release()

    def __enter__(self):
        raise RuntimeError(
            '"yield from" should be used as context manager expression')

    def __exit__(self, *args):
        pass

    @staticmethod
    def _read_alt_az_coord(alt, az):
        """
        Method return alt, az after converting.

        :param alt: altitude
        :param az: azimuth
        :raise ValueError: if it can not convert values
        :return: alt az after converting
        """
        alt_ = None
        az_ = None
        if alt is not None and az is not None:
            try:
                az_ = deg_to_decimal_deg(az)
                alt_ = deg_to_decimal_deg(alt)
            except ValueError:
                raise ValueError
        return alt_, az_

    async def a_init(self):
        pass  # run always but nothing to do

    @property
    def ok(self) -> bool:
        return self._correctly_load_data


class DomeAlpaca(Resource):
    RESOURCE_NAME = StandardTelescopeComponents.DOME

    def __init__(self, source_name: str, resource_name: str, nr: int, target_request, properties: dict = None,
                 address_path: str = "", **kwargs) -> None:
        super().__init__(source_name=source_name, resource_name=resource_name, nr=nr, target_request=target_request,
                         properties=properties, address_path=address_path, **kwargs)


class MountAlpaca(Resource):
    RESOURCE_NAME = StandardTelescopeComponents.MOUNT

    def __init__(self, source_name: str, resource_name: str, nr: int, target_request, properties: dict = None,
                 address_path: str = "", **kwargs) -> None:
        super().__init__(source_name=source_name, resource_name=resource_name, nr=nr, target_request=target_request,
                         properties=properties, address_path=address_path, **kwargs)
        self.latitude = self._properties.get("lat", 0)
        self.longitude = self._properties.get("lon", 0)
        self.elevation = self._properties.get("elev", 0)
        self.epoch = str(self._properties.get("epoch", 2000))
        self.min_alt = self._properties.get("min_alt", 10)


class CameraAlpaca(Resource):
    RESOURCE_NAME = StandardTelescopeComponents.CAMERA


class FilterwheelAlpaca(Resource):
    RESOURCE_NAME = StandardTelescopeComponents.FILTERWHEEL

    def __init__(self, source_name: str, resource_name: str, nr: int, target_request, properties: dict = None,
                 address_path: str = "", **kwargs) -> None:
        super().__init__(source_name=source_name, resource_name=resource_name, nr=nr, target_request=target_request,
                         properties=properties, address_path=address_path, **kwargs)
        self.filters: dict = {}

    async def a_init(self):
        self._correctly_load_data = True
        if self._target is not None:
            api = InternalClientAPI(request_solver=self._target, user_name=f"{self.__class__.__name__}_init_client")
            await self._read_filters(api=api)

    async def _read_filters(self, api):
        """Method looking a filters definition in settings or try get them from alpaca"""
        f = self._properties.get("filters", 0)
        # if filters is defined in settings
        if f and isinstance(f, dict):
            logger.warning('Filters defined in config as a dict name->pos. This is obsolete, change for list of dicts')
            self.filters = f
            return
        elif f and isinstance(f, list):  # new convention, TODO: maybe store additional filter info? (or del this TODO)
            self.filters = {x['name']: x['position'] for x in f}
            logger.info(f'Filters loaded from conf: {self.filters}')

        # if filters is not defined in settings - get from alpaca
        try:
            result = await api.get_async(address=f"{self.adr}.names")
        except (CommunicationRuntimeError, CommunicationTimeoutError):
            logger.warning(f"Can not get list of filters when initialize resource")
            self._correctly_load_data = False
            result = None
        if result and result.status and result.value and isinstance(result.value.v, list):
            f = result.value.v
            self.filters = {x: count for count, x in enumerate(f)}
        else:
            self._correctly_load_data = False


    async def get_filters(self, no_init=False) -> dict:
        """
        This method return filters and check if resource is correctly initialized, if not method try to initialize again

        :param no_init: no check if resource is correctly initialized
        :return: list of filters
        """
        if not self.ok and not no_init:
            logger.info(f"trying to finish initialize resource {self.RESOURCE_NAME}")
            await self.a_init()
        return self.filters


class FocuserAlpaca(Resource):
    RESOURCE_NAME = StandardTelescopeComponents.FOCUSER

    def __init__(self, source_name: str, resource_name: str, nr: int, target_request, properties: dict = None,
                 address_path: str = "", **kwargs) -> None:
        super().__init__(source_name=source_name, resource_name=resource_name, nr=nr, target_request=target_request,
                         properties=properties, address_path=address_path, **kwargs)
        self.focus_tolerance: float = self._properties.get("focus_tolerance", 5)


class RotatorAlpaca(Resource):
    RESOURCE_NAME = StandardTelescopeComponents.ROTATOR


class SwitchAlpaca(Resource):
    RESOURCE_NAME = StandardTelescopeComponents.SWITCH


class SafetymonitorAlpaca(Resource):
    RESOURCE_NAME = StandardTelescopeComponents.SAFETYMONITOR


class CovercalibratorAlpaca(Resource):
    RESOURCE_NAME = StandardTelescopeComponents.COVERCALIBRATOR
    COVER_STATUS_MAP = {'notpresent': 0, 'closed': 1, 'moving': 2, 'open': 3, 'unknown': 4, 'error': 5}


class TertiaryAlpaca(Resource):
    RESOURCE_NAME = StandardTelescopeComponents.TERTIARY

