import asyncio
import logging
from datetime import datetime
from typing import Optional, Union, List, Dict, Coroutine, Callable, Iterable

from obsrv.data_colection.iris_api.connector import IrisConnector
from obsrv.data_colection.iris_api.coo import check_equatorial_coordinates
from obsrv.data_colection.iris_api.standard_telescope_components import StandardTelescopeComponents
from obsrv.ob_config import SingletonConfig

logger = logging.getLogger(__name__.rsplit('.', 1)[-1])


class Component:
    KIND = "component"

    def __init__(self, sys_id: str, parent: Optional['Component']) -> None:
        self.kind = self.KIND
        self.sys_id: str = sys_id
        self.parent = parent
        self.component_options: dict = {}
        self._connector: Optional[IrisConnector] = None
        self.children: Dict[str, Component] = {}

    def _setup(self, options: dict):
        self.component_options = options.copy()
        if not self._connector and 'protocol' in self.component_options:
            protocol = self.component_options['protocol']
            if protocol == 'iris':
                host = self.component_options.get('host')
                port = int(self.component_options.get('port'))
                if not host or not port:
                    raise ValueError("Host and port are required for the 'iris' protocol.")
                self._connector = IrisConnector(host=host, port=port)

        child_options = self.component_options.pop('components', {})
        for cid, op in child_options.items():
            child = self._create_component(kind=op.get('kind', 'device'), sys_id=f"{self.sys_id}.{cid}", parent=self)
            self.children[cid] = child
            setattr(self, cid, child)
            child._setup(op)

    @property
    def connector(self) -> IrisConnector:
        if self._connector:
            return self._connector
        if self.parent:
            return self.parent.connector
        raise RuntimeError("No connector found in the component tree.")

    @property
    def root(self) -> 'Component':
        return self.parent.root if self.parent else self

    @classmethod
    def _create_component(cls, kind: str, sys_id: str, parent: 'Component') -> 'Component':
        if kind not in _component_classes:
            raise ValueError(f"Unknown component kind: '{kind}'. Available kinds: {list(_component_classes.keys())}")
        return _component_classes[kind](sys_id=sys_id, parent=parent)


class Device(Component):
    KIND = "device"

    async def get(self, variable: str, kind: str = None, **data):
        return await self.connector.get(self, variable, kind=kind, **data)

    async def put(self, variable: str, kind: str = None, **data):
        return await self.connector.put(self, variable, kind=kind, **data)


class Observatory(Device):
    KIND = "observatory"

    def __init__(self):
        self.config = SingletonConfig.get_config()
        super().__init__('obs', None)

    async def connect(self, preset: Union[List[str], str] = 'default'):
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
        await self.put("READY", kind="TELESCOPE", value=0)
        await self.connector.close()
        logger.info("Observatory disconnected.")


class Telescope(Device):
    KIND = StandardTelescopeComponents.MOUNT

    async def slew_to_coordinates_put(self, ra: str, dec: str, **kwargs):
        logger.info(f"Commanding slew to RA={ra}, Dec={dec}")
        ra_deg, dec_deg = check_equatorial_coordinates(ra, dec)
        ra_hr = ra_deg / 15.0
        
        await self.put("RA", kind="OBJECT.EQUATORIAL", value=ra_hr)
        await self.put("DEC", kind="OBJECT.EQUATORIAL", value=dec_deg)
        
        return await self.put("TRACK", kind="POINTING", value=1)

    async def park_put(self, **kwargs):
        logger.info("Commanding telescope to park (by setting READY=0).")
        return await self.put("READY", kind="TELESCOPE", value=0)

    async def unpark_put(self, **kwargs):
        logger.info("Commanding telescope to unpark (by setting READY=1).")
        return await self.put("READY", kind="TELESCOPE", value=1)
        
    async def abort_slew_put(self, **kwargs):
        logger.info("Commanding immediate stop.")
        return await self.put("STOP", kind="TELESCOPE", value=1)


class Dome(Device):
    KIND = StandardTelescopeComponents.DOME

    async def slew_to_azimuth_put(self, azimuth: float, **kwargs):
        logger.info(f"Commanding dome to azimuth {azimuth}")
        return await self.put("TARGETPOS", kind="DOME[0]", value=azimuth)

    async def open_shutter_put(self, **kwargs):
        logger.info("Commanding dome shutter to open.")
        return await self.put("TARGETPOS", kind="DOME[1]", value=1)

    async def close_shutter_put(self, **kwargs):
        logger.info("Commanding dome shutter to close.")
        return await self.put("TARGETPOS", kind="DOME[1]", value=0)


class Mirror(Device):
    KIND = StandardTelescopeComponents.MIRROR

    async def select_port_put(self, port_index: int):
        logger.info(f"Setting M3 mirror to select optical port {port_index}")
        return await self.put("TARGETPOS", value=port_index)


class Cover(Device):
    KIND = StandardTelescopeComponents.COVER

    async def open_put(self, **kwargs):
        logger.info("Commanding cover to open.")
        return await self.put("TARGETPOS", kind="COVER[0]", value=1)

    async def close_put(self, **kwargs):
        logger.info("Commanding cover to close.")
        return await self.put("TARGETPOS", kind="COVER[0]", value=0)

    async def get_status(self) -> str:
        status = await self.get("REALPOS", kind="COVER[0]")
        return "Open" if float(status) == 1.0 else "Closed"


class Sensor(Device):
    KIND = StandardTelescopeComponents.SENSOR

    @property
    def index(self) -> int:
        return int(self.component_options.get('index', 0))

    async def get_name(self) -> str:
        return await self.get("NAME", kind=f"SENSOR[{self.index}]")

    async def get_value(self) -> float:
        value = await self.get("VALUE", kind=f"SENSOR[{self.index}]")
        return float(value)

    async def get_unit(self) -> str:
        return await self.get("UNIT", kind=f"SENSOR[{self.index}]")


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
