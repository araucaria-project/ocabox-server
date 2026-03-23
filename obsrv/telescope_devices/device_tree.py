import logging
from datetime import datetime
from typing import Optional, Union, List, MutableMapping, Dict, Coroutine, Callable

from obsrv.protocols import create_connector
from obsrv.utils.coordinates import check_equatorial_coordinates, check_horizontal_coordinates
from obsrv.telescope_devices.standard_components import StandardTelescopeComponents
from obsrv.ob_config import SingletonConfig

logger = logging.getLogger(__name__.rsplit('.')[-1])


class Component:
    """
    Base class for all elements of device tree
    """
    KIND = "component"

    def __init__(self, sys_id: str, parent: Union['Component', None]) -> None:
        self.kind = self.KIND
        self.sys_id: str = sys_id
        self.parent: Component = parent
        self.component_options = {}
        self._connector = None
        self.children: Dict[str, Component] = {}

    def _setup(self, options: dict):
        self.component_options: MutableMapping = options.copy()
        if not self._connector:
            try:
                # Create connector for this component's protocol
                self._connector = create_connector(self.component_options['protocol'])
            except KeyError:
                pass
        try:
            child_options = self.component_options.pop('components')
        except KeyError:
            child_options = {}
        for cid, op in child_options.items():
            child = self._create_component(kind=op['kind'], sys_id=self.sys_id + '.' + cid, parent=self)
            self.children[cid] = child
            setattr(self, cid, child)  # allow easy navigation: `parent.child`
            child._setup(op)

    @property
    def device_nr(self) -> int:
        return int(self.component_options.get('device_number', 0))

    @property
    def connector(self):
        if self._connector is not None:
            return self._connector
        else:
            return self.parent.connector

    def get_option_recursive(self, option):
        try:
            return self.component_options[option]
        except KeyError:
            if self.parent is None:
                return None
            else:
                return self.parent.get_option_recursive(option)

    def children_tree_iter(self):
        """Generator yielding components tree, starting from self """
        yield self
        for c in self.children.values():
            yield from c.children_tree_iter()

    def children_count(self, recursively=True):
        """gets number of children"""
        n = len(self.children)
        if recursively:
            for c in self.children.values():
                n += c.children_count(recursively=True)
        return n

    def child_by_relative_sys_id(self, sys_id_rel: str):
        """Find child by relative sys_id path"""
        cid, *cpath = sys_id_rel.split('.', 1)
        c = self.children[cid]
        if cpath:
            return c.child_by_relative_sys_id(cpath[0])
        else:
            return c

    def component_by_absolute_sys_id(self, sys_id_abs: str):
        cid, *cpath = sys_id_abs.split('.', 1)
        root = self.root
        if root.sys_id != cid:
            raise IndexError('Absolute sys_id should start from root: %s', root.sys_id)
        if cpath:
            return root.child_by_relative_sys_id(cpath[0])
        else:
            return self

    @property
    def root(self):
        if self.parent is not None:
            return self.parent.root
        else:
            return self

    @classmethod
    def _create_component(cls, kind: str, sys_id: str, parent: 'Component') -> 'Component':
        return _component_classes[kind](sys_id=sys_id, parent=parent)


class Observatory(Component):
    """Observatory - root device in devices tree

    Attributes:
        configuration (Config): Optional configuration, by default configuration will be loaded from following files:
              ./ocabox.config.yaml
              ./ocabox.configuration.config.yaml
            Later overwrites former
    """
    KIND = "observatory"

    def __init__(self):
        configuration = SingletonConfig.get_config()
        self.config = configuration
        self.observatory_configuration_rare = {}
        self.preset: List[str] = ['default']
        super().__init__('obs', None)

    def connect(self, preset: List[str] or str = 'default', connector=None) -> None:
        """
        Connect to servers if needed, builds Devices tree
        Args:
            preset: name of the preset from config
            connector:
        """
        if connector:
            self._connector = connector
        if preset is None:
            preset = 'default'
        if not isinstance(preset, List):
            preset = [preset]
        self.preset = preset

        o = self.config
        for p in preset:
            o = o[p]

        # options = self.config.data[preset]['observatory']
        options = o['observatory'].get()
        self.observatory_configuration_rare = options
        self._setup(options)
    
    def add_component(self, sys_id: str, kind: str, **config) -> 'Component':
        """Add a component to the observatory
        
        Args:
            sys_id: System ID for the component
            kind: Type of component (telescope, camera, dome, etc.)
            **config: Configuration options for the component
            
        Returns:
            The created component
        """
        component = self._create_component(kind=kind, sys_id=sys_id, parent=self)
        component._setup(config)
        self.children[sys_id] = component
        setattr(self, sys_id, component)  # Allow direct access like obs.telescope
        return component
    
    def get_all_components(self) -> List['Component']:
        """Get all components in the observatory"""
        return list(self.children.values())


class Device(Component):
    """Common methods across all devices.

    Attributes:
        sys_id (str): system ID of device
        parent (Component): The parent component in devices tree
    """
    KIND = "device"
    CURRENT = 0
    PREVIOUS = 1
    READ_TIME = 2
    MODIFY_TIME = 3

    def __init__(self, sys_id: str, parent: Union['Component', None]) -> None:
        """Initialize Device object."""
        super().__init__(sys_id=sys_id, parent=parent)
        self._process_data_get = {}
        self._process_data_put = {}
        self._process_response_get = {}
        self._process_response_put = {}

    def _get(self, attribute: str, kind=None, **data) -> Coroutine:
        """Send request and check response for errors.

        Args:
            attribute (str): Attribute to get from server.
            **data: Data to send with request.

        """
        return self.connector.get(self, attribute, kind=kind, **data)

    def _put(self, attribute: str, kind=None, **data) -> Coroutine:
        """
        Send an HTTP PUT request to an Alpaca server and check response for errors.

        Args:
            attribute (str): Attribute to put to server.
            **data: Data to send with request.

        """
        return self.connector.put(self, attribute, kind=kind, **data)

    def _find_attribute(self, attribute):
        try:
            method = getattr(self, attribute)
        except AttributeError:
            method = None
        return method

    def add_alpaca_get_parameters_process(self, attribute: str, processor: Callable):
        self._process_data_get[attribute] = processor

    def add_alpaca_put_parameters_process(self, attribute: str, processor: Callable):
        self._process_data_put[attribute] = processor

    def add_alpaca_get_response_process(self, attribute: str, processor: Callable):
        self._process_response_get[attribute] = processor

    def add_alpaca_put_response_process(self, attribute: str, processor: Callable):
        self._process_response_put[attribute] = processor

    def _process_alpaca_get_parameters(self, attribute: str, **data):
        processor = self._process_data_get.get(attribute)
        if processor is not None:
            return processor(attribute, **data)
        else:
            return data

    def _process_alpaca_put_parameters(self, attribute: str, **data):
        processor = self._process_data_put.get(attribute)
        if processor is not None:
            return processor(attribute, **data)
        else:
            return data

    def _process_alpaca_get_result(self, attribute: str, ret):
        processor = self._process_response_get.get(attribute)
        if processor is not None:
            return processor(attribute, ret)
        else:
            return ret

    def _process_alpaca_put_result(self, attribute: str, ret):
        processor = self._process_response_put.get(attribute)
        if processor is not None:
            return processor(attribute, ret)
        else:
            return ret

    async def get(self, attribute: str, kind=None, **data):
        params = self._process_alpaca_get_parameters(attribute, **data)
        method = self._find_attribute(attribute)
        if method and callable(method):
            ret = await method(**params)
        else:
            ret = await self._get(attribute, kind=kind, **params)
        return self._process_alpaca_get_result(attribute, ret)

    async def put(self, attribute: str, kind=None, **data):
        params = self._process_alpaca_put_parameters(attribute, **data)
        method = self._find_attribute(attribute+'_put')
        if method and callable(method):
            ret = await method(**params)
        else:
            ret = await self._put(attribute, kind=kind, **params)
        return self._process_alpaca_put_result(attribute, ret)

    # async def get(self, attribute: str, **data):
    #     method = self._find_attribute(attribute)
    #     if method and callable(method):
    #         return await method(**data)
    #     return await self._get(attribute, **data)

    # async def put(self, attribute: str, **data):
    #     method = self._find_attribute(attribute+'_put')
    #     if method and callable(method):
    #         return await method(**data)
    #     return await self._put(attribute, **data)

    async def driverinfo(self) -> List[str]:
        """Get information of the device."""
        return [i.strip() for i in (await self._get("driverinfo")).split(",")]


class Switch(Device):
    """Switch specific methods."""
    KIND = StandardTelescopeComponents.SWITCH


class CoverCalibrator(Device):
    """CoverCalibrator specific methods."""
    KIND = StandardTelescopeComponents.COVERCALIBRATOR


class SafetyMonitor(Device):
    """Safety monitor specific methods."""
    KIND = StandardTelescopeComponents.SAFETYMONITOR


class Dome(Device):
    """Dome specific methods."""
    KIND = StandardTelescopeComponents.DOME

    async def domefansrunning(self, **kwargs):
        return await self._put("commandbool", Command='DomeFansRunning', Raw='False')

    async def domefansturnon_put(self, **kwargs):
        return await self._put("commandblind", Command='DomeFansTurnOn', Raw='False')

    async def domefansturnoff_put(self, **kwargs):
        return await self._put("commandblind", Command='DomeFansTurnOff', Raw='False')


class Camera(Device):
    """Camera specific methods."""
    KIND = StandardTelescopeComponents.CAMERA


class FilterWheel(Device):
    """Filter wheel specific methods."""
    KIND = StandardTelescopeComponents.FILTERWHEEL


class Telescope(Device):
    """Telescope specific methods."""
    KIND = StandardTelescopeComponents.MOUNT

    def __init__(self, sys_id: str, parent: Union['Component', None]) -> None:
        super().__init__(sys_id=sys_id, parent=parent)
        self.add_alpaca_get_response_process('rightascension',
                                             lambda at, res: self._hourangle_to_deg_processor(at, res))
        self.add_alpaca_put_parameters_process('targetdeclination',
                                               lambda at, **data: self._target_declination_processor(at, **data))
        self.add_alpaca_get_response_process('targetrightascension',
                                             lambda at, res: self._hourangle_to_deg_processor(at, res))
        self.add_alpaca_put_parameters_process('targetrightascension',
                                               lambda at, **data: self._target_rightascension_processor(at, **data))
        self.add_alpaca_put_parameters_process('utcdate',
                                               lambda at, **data: self._utcdate_processor(at, **data))
        self.add_alpaca_get_parameters_process('destinationsideofpier',
                                               lambda at, **data: self._check_equatorial_coordinates_processor(at,
                                                                                                               **data))
        self.add_alpaca_put_parameters_process('slewtoaltaz',
                                               lambda at, **data: self._check_horizontal_coordinates_processor(at,
                                                                                                               **data))
        self.add_alpaca_put_parameters_process('slewtoaltazasync',
                                               lambda at, **data: self._check_horizontal_coordinates_processor(at,
                                                                                                               **data))
        self.add_alpaca_put_parameters_process('slewtocoordinates',
                                               lambda at, **data: self._check_equatorial_coordinates_processor(at,
                                                                                                               **data))
        self.add_alpaca_put_parameters_process('slewtocoordinatesasync',
                                               lambda at, **data: self._check_equatorial_coordinates_processor(at,
                                                                                                               **data))
        self.add_alpaca_put_parameters_process('synctoaltaz',
                                               lambda at, **data: self._check_horizontal_coordinates_processor(at,
                                                                                                               **data))
        self.add_alpaca_put_parameters_process('synctocoordinates',
                                               lambda at, **data: self._check_equatorial_coordinates_processor(at,
                                                                                                               **data))

    async def reportmaxalt(self, **kwargs):
        return await self._put("action", Action="telescope:reportmaxalt", Parameters="")

    async def motoron_put(self, **kwargs):
        return await self._put("action", Action="telescope:motoron", Parameters="")

    async def motoroff_put(self, **kwargs):
        return await self._put("action", Action="telescope:motoroff", Parameters="")

    async def domeflatlampon_put(self, **kwargs):
        return await self._put("action", Action="telescope:startfans", Parameters="5")  # lamp is connected under fans

    async def domeflatlampoff_put(self, **kwargs):
        return await self._put("action", Action="telescope:stopfans", Parameters="")  # lamp is connected under fans

    async def motorstatus(self, **kwargs):
        return await self._put("commandstring", Command="MotStat", Raw="True")

    async def errorstring(self, **kwargs):
        return await self._put("action", Action="telescope:errorstring", Parameters="")

    @staticmethod
    def _hourangle_to_deg_processor(attribute, res):
        return res / 24 * 360

    @staticmethod
    def _target_declination_processor(attribute, TargetDeclination: Optional[Union[float, str]]):
        _, TargetDeclination = check_equatorial_coordinates(0.0, TargetDeclination)
        return {"TargetDeclination": TargetDeclination}

    @staticmethod
    def _target_rightascension_processor(attribute, TargetRightAscension: Optional[Union[float, str]]):
        TargetRightAscension, _ = check_equatorial_coordinates(TargetRightAscension, 0.0)
        TargetRightAscension = TargetRightAscension / 360 * 24  # deg -> hour angle
        return {"TargetRightAscension": TargetRightAscension}

    @staticmethod
    def _utcdate_processor(attribute, UTCDate: Optional[Union[str, datetime]]):
        if type(UTCDate) is str:
            data = UTCDate
        elif type(UTCDate) is datetime:
            data = UTCDate.isoformat()
        else:
            raise TypeError()
        return {"UTCDate": data}

    @staticmethod
    def _check_equatorial_coordinates_processor(attribute, RightAscension: Union[float, str],
                                                Declination: Union[float, str]):
        RightAscension, Declination = check_equatorial_coordinates(RightAscension, Declination)
        RightAscension = RightAscension / 360 * 24
        return {"RightAscension": RightAscension, 'Declination': Declination}

    @staticmethod
    def _check_horizontal_coordinates_processor(attribute, Azimuth: Union[float, str], Altitude: Union[float, str]):
        Azimuth, Altitude = check_horizontal_coordinates(Azimuth, Altitude)
        return {"Azimuth": Azimuth, 'Altitude': Altitude}


class Focuser(Device):
    """Focuser specific methods."""
    KIND = StandardTelescopeComponents.FOCUSER

    async def fansturnon_put(self, **kwargs):
        return await self._put("action", Action="fansturnon", Parameters="")

    async def fansturnoff_put(self, **kwargs):
        return await self._put("action", Action="fansturnoff", Parameters="")

    async def fansstatus(self, **kwargs):
        return await self._put("action", Action="fansstatus", Parameters="")


class Rotator(Device):
    """Rotator specific methods."""
    KIND = StandardTelescopeComponents.ROTATOR


class CoverCalibratorOCA(CoverCalibrator):
    """CoverCalibrator OCA specific methods."""

    async def closecover_put(self, **kwargs):
        return await self._put("action", kind=Telescope.KIND, Action='telescope:closecover', Parameters='')

    async def opencover_put(self, **kwargs):
        return await self._put("action", kind=Telescope.KIND, Action='telescope:opencover', Parameters='')


class Tertiary(Device):
    """Tertiary specific method"""
    KIND = StandardTelescopeComponents.TERTIARY

    # TODO ponieważ takiego czegoś niema w alpace i to jest specyficzne dla OCA. (specyficzne jak narazie)


class TertiaryOCA(Tertiary):
    """Tertiary OCA specific methods"""

    async def selectnasmythport_put(self, **kwargs):
        """Name parameters to set 'Position' """
        parameters = ""
        position = kwargs.get("Position", None)
        if position is None:
            pass
        elif isinstance(position, int):
            parameters = "" + str(position)
        return await self._put("action", kind=Telescope.KIND, Action='selectnasmythport', Parameters=parameters)


_component_classes = {
    Telescope.KIND: Telescope,
    Dome.KIND: Dome,
    Camera.KIND: Camera,
    FilterWheel.KIND: FilterWheel,
    Focuser.KIND: Focuser,
    Rotator.KIND: Rotator,
    Switch.KIND: Switch,
    SafetyMonitor.KIND: SafetyMonitor,
    CoverCalibrator.KIND: CoverCalibrator,
    Tertiary.KIND: Tertiary,  # here is a custom key, normally shou by Tertiary but its specific kind only for OCA!
    "tertiaryOCA": TertiaryOCA,  # here is a custom key !
    'covercalibratorOCA': CoverCalibratorOCA,  # here is a custom key !
}
