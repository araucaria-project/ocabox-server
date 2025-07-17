import asyncio
import logging
from datetime import datetime
from typing import Optional, Union, List, Dict, Coroutine, Callable, Iterable

# Imports indicating modules within the iris_api package
from obsrv.data_colection.iris_api.connector import Connector
from obsrv.data_colection.iris_api.coo import check_equatorial_coordinates
from obsrv.data_colection.iris_api.standard_telescope_components import StandardTelescopeComponents

# Import configuration from the main project
from obsrv.ob_config import SingletonConfig

logger = logging.getLogger(__name__.rsplit('.', 1)[-1])


class Component:
    """Base class for all elements of the device tree."""
    KIND = "component"

    def __init__(self, sys_id: str, parent: Optional['Component']) -> None:
        self.kind = self.KIND
        self.sys_id: str = sys_id
        self.parent = parent
        self.component_options: dict = {}
        self._connector: Optional[Connector] = None
        self.children: Dict[str, Component] = {}

    def _setup(self, options: dict):
        """Recursively builds the component tree based on the configuration."""
        self.component_options = options.copy()
        if not self._connector and 'protocol' in self.component_options:
            protocol = self.component_options['protocol']
            if protocol == 'iris':
                host = self.component_options.get('host')
                port = int(self.component_options.get('port'))
                if not host or not port:
                    raise ValueError("Host and port are required for the 'iris' protocol.")
                self._connector = Connector.create_connector(protocol, host=host, port=port)

        child_options = self.component_options.pop('components', {})
        for cid, op in child_options.items():
            child = self._create_component(kind=op.get('kind', 'device'), sys_id=f"{self.sys_id}.{cid}", parent=self)
            self.children[cid] = child
            setattr(self, cid, child)
            child._setup(op)

    @property
    def connector(self) -> Connector:
        """Returns the appropriate connector, inheriting it from the parent."""
        if self._connector:
            return self._connector
        if self.parent:
            return self.parent.connector
        raise RuntimeError("No connector found in the component tree.")

    @property
    def root(self) -> 'Component':
        """Returns the root of the component tree."""
        return self.parent.root if self.parent else self

    @classmethod
    def _create_component(cls, kind: str, sys_id: str, parent: 'Component') -> 'Component':
        """Factory method that creates a component instance."""
        if kind not in _component_classes:
            raise ValueError(f"Unknown component kind: '{kind}'. Available kinds: {list(_component_classes.keys())}")
        return _component_classes[kind](sys_id=sys_id, parent=parent)


class Observatory(Component):
    """The root of the device tree - represents the entire observatory."""
    KIND = "observatory"

    def __init__(self):
        self.config = SingletonConfig.get_config()
        super().__init__('obs', None)

    async def connect(self, preset: Union[List[str], str] = 'default'):
        """Builds the device tree and establishes a connection with the server."""
        if not isinstance(preset, list):
            preset = [preset]
        config_level = self.config
        for p in preset:
            config_level = config_level[p]
        options = config_level['observatory'].get()
        self._setup(options)
        await self.connector.connect()
        logger.info("Observatory connected via IrisConnector.")
        await self.put("READY", kind="TELESCOPE", value=1)

    async def disconnect(self):
        """Safely disconnects the connector and powers down the telescope."""
        await self.put("READY", kind="TELESCOPE", value=0)
        await self.connector.close()
        logger.info("Observatory disconnected.")


class Device(Component):
    """Base class for all physical devices."""
    KIND = "device"

    async def get(self, variable: str, kind: str = None, **data):
        return await self.connector.get(self, variable, kind=kind, **data)

    async def put(self, variable: str, kind: str = None, **data):
        return await self.connector.put(self, variable, kind=kind, **data)


class Telescope(Device):
    """Methods for the telescope compliant with the high-level OpenTSI interface."""
    KIND = StandardTelescopeComponents.MOUNT

    async def slew_to_coordinates_put(self, ra: str, dec: str, **kwargs):
        """Points the telescope to the given coordinates and starts tracking."""
        logger.info(f"Commanding slew to RA={ra}, Dec={dec}")
        ra_deg, dec_deg = check_equatorial_coordinates(ra, dec)
        ra_hr = ra_deg / 15.0  # OpenTSI requires hours for RA
        
        await self.put("RA", kind="OBJECT.EQUATORIAL", value=ra_hr)
        await self.put("DEC", kind="OBJECT.EQUATORIAL", value=dec_deg)
        
        return await self.put("TRACK", kind="POINTING", value=1)

    async def park_put(self, **kwargs):
        """Parks the telescope by turning it off."""
        logger.info("Commanding telescope to park (by setting READY=0).")
        return await self.put("READY", kind="TELESCOPE", value=0)

    async def unpark_put(self, **kwargs):
        """Unparks the telescope."""
        logger.info("Commanding telescope to unpark (by setting READY=1).")
        return await self.put("READY", kind="TELESCOPE", value=1)
        
    async def abort_slew_put(self, **kwargs):
        """Aborts the telescope's movement."""
        logger.info("Commanding immediate stop.")
        return await self.put("STOP", kind="TELESCOPE", value=1)


class Dome(Device):
    """Methods for the dome compliant with OpenTSI."""
    KIND = StandardTelescopeComponents.DOME

    async def slew_to_azimuth_put(self, azimuth: float, **kwargs):
        """Slews the dome to the specified azimuth."""
        logger.info(f"Commanding dome to azimuth {azimuth}")
        return await self.put("TARGETPOS", kind="DOME[0]", value=azimuth)

    async def open_shutter_put(self, **kwargs):
        """Opens the dome shutter."""
        logger.info("Commanding dome shutter to open.")
        return await self.put("TARGETPOS", kind="AUXILIARY.DOME", value=1)

    async def close_shutter_put(self, **kwargs):
        """Closes the dome shutter."""
        logger.info("Commanding dome shutter to close.")
        return await self.put("TARGETPOS", kind="AUXILIARY.DOME", value=0)


class Mirror(Device):
    """Handles the M3 mirror (flip mirror). Corresponds to the 'MIRROR' module in OpenTCI."""
    KIND = StandardTelescopeComponents.MIRROR

    async def select_port_put(self, port_index: int):
        """Sets the M3 mirror to direct light to the selected optical port."""
        logger.info(f"Setting M3 mirror to select optical port {port_index}")
        return await self.put("TARGETPOS", value=port_index)


class Cover(Device):
    """Handles covers (e.g., for the main mirror). Corresponds to 'AUXILIARY.COVER' in OpenTSI."""
    KIND = StandardTelescopeComponents.COVER

    async def open_put(self, **kwargs):
        """Opens the cover."""
        logger.info("Commanding cover to open.")
        return await self.put("TARGETPOS", kind="AUXILIARY.COVER", value=1)

    async def close_put(self, **kwargs):
        """Closes the cover."""
        logger.info("Commanding cover to close.")
        return await self.put("TARGETPOS", kind="AUXILIARY.COVER", value=0)

    async def get_status(self) -> str:
        """Gets the cover status (0.0: closed, 1.0: open)."""
        status = await self.get("REALPOS", kind="AUXILIARY.COVER")
        return "Open" if float(status) == 1.0 else "Closed"


class Sensor(Device):
    """Handles a single sensor. Corresponds to an element of the 'AUXILIARY.SENSOR[]' array."""
    KIND = StandardTelescopeComponents.SENSOR

    @property
    def index(self) -> int:
        """Gets the sensor index from the configuration options (required in YAML)."""
        return int(self.component_options.get('index', 0))

    async def get_name(self) -> str:
        """Gets the name/description of the sensor."""
        return await self.get("DESCRIPTION", kind=f"AUXILIARY.SENSOR[{self.index}]")

    async def get_value(self) -> float:
        """Reads the current value of the sensor."""
        value = await self.get("VALUE", kind=f"AUXILIARY.SENSOR[{self.index}]")
        return float(value)

    async def get_unit(self) -> str:
        """Gets the unit in which the sensor's value is reported."""
        return await self.get("UNIT", kind=f"AUXILIARY.SENSOR[{self.index}]")


# Simple placeholder classes for other devices
class Camera(Device):
    KIND = StandardTelescopeComponents.CAMERA

class FilterWheel(Device):
    KIND = StandardTelescopeComponents.FILTERWHEEL

class Focuser(Device):
    KIND = StandardTelescopeComponents.FOCUSER
    
class Rotator(Device):
    KIND = StandardTelescopeComponents.ROTATOR

class Switch(Device):
    KIND = StandardTelescopeComponents.SWITCH

class SafetyMonitor(Device):
    KIND = StandardTelescopeComponents.SAFETYMONITOR

class CoverCalibrator(Device):
    KIND = StandardTelescopeComponents.COVERCALIBRATOR


# Central component registry, used by the Component._create_component factory method
_component_classes = {
    "observatory": Observatory,
    StandardTelescopeComponents.MOUNT: Telescope,
    StandardTelescopeComponents.DOME: Dome,
    StandardTelescopeComponents.CAMERA: Camera,
    StandardTelescopeComponents.FILTERWHEEL: FilterWheel,
    StandardTelescopeComponents.FOCUSER: Focuser,
    StandardTelescopeComponents.ROTATOR: Rotator,
    StandardTelescopeComponents.MIRROR: Mirror,
    StandardTelescopeComponents.COVER: Cover,
    StandardTelescopeComponents.SENSOR: Sensor,
    StandardTelescopeComponents.SWITCH: Switch,
    StandardTelescopeComponents.SAFETYMONITOR: SafetyMonitor,
    StandardTelescopeComponents.COVERCALIBRATOR: CoverCalibrator,
}
