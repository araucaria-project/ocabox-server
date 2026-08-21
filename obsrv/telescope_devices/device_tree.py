import functools
import json
import logging
import math
import re
from datetime import datetime
from typing import Optional, Union, List, MutableMapping, Dict, Coroutine, Callable

from obcom.data_colection.coded_error import TreeOtherError, TreeStructureError
from obcom.data_colection.value import TreeValueError

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
    """Tertiary (M3) mirror device — interface contract.

    ALPACA has no tertiary device type, so every implementation is
    vendor-specific (see ``TertiaryOCA`` for ASA AutoSlew). The base class
    declares the interface; each method raises ``3002`` so that a tree
    configured with a plain ``tertiary`` kind fails loudly instead of falling
    through to a nonexistent ALPACA endpoint.

    Attributes (GET): ``nasmythport``, ``tertiarystatus`` and its decomposed
    fields ``angle``, ``moving``, ``motoron``, ``portname``.
    Attributes (PUT): ``selectnasmythport`` (``Position``: physical port number).

    A controller-reported fault must surface through the standard error model
    (``TreeOtherError(4009)``, "device reported an error"), not as a readable
    boolean — except in ``tertiarystatus``, the raw diagnostic view.
    """
    KIND = StandardTelescopeComponents.TERTIARY

    def _not_implemented(self, method: str):
        raise TreeStructureError(
            code=3002,
            message=f"Method {method!r} is not implemented on {type(self).__name__} "
                    f"({self.sys_id}); use a vendor-specific tertiary kind (e.g. 'tertiaryOCA')",
            severity=TreeStructureError.SEVERITY_CRITICAL,
        )

    async def nasmythport(self, **kwargs):
        """Current physical port number (int)."""
        self._not_implemented('nasmythport')

    async def tertiarystatus(self, **kwargs):
        """Full status as dict: Moving, MotorOn, ErrorRaised, Angle, NasmythPort, PortName."""
        self._not_implemented('tertiarystatus')

    async def angle(self, **kwargs):
        """Current M3 rotation angle (deg, float)."""
        self._not_implemented('angle')

    async def moving(self, **kwargs):
        """True while M3 is rotating (bool)."""
        self._not_implemented('moving')

    async def motoron(self, **kwargs):
        """True when the M3 motor is powered (bool)."""
        self._not_implemented('motoron')

    async def portname(self, **kwargs):
        """Vendor name of the current port, e.g. 'ADR10' (str)."""
        self._not_implemented('portname')

    async def selectnasmythport_put(self, **kwargs):
        """Rotate M3 to a physical port. Parameter: ``Position`` (int)."""
        self._not_implemented('selectnasmythport')


class TertiaryOCA(Tertiary):
    """ASA AutoSlew tertiary (OCA), driven through the mount's ALPACA action channel.

    All reads are served by the AutoSlew vendor action ``tertiarystatus``
    (JSON: ``{"Moving", "MotorOn", "ErrorRaised", "Angle", "NasmythPort",
    "PortName"}``); movement by ``selectnasmythport``. Port numbers are the
    physical AutoSlew ports (jk15: 1=ADR6/beso, 2=ADR10/andor) — they are NOT
    0-based; the observatory config maps them to instruments.

    A controller fault (``ErrorRaised`` in the status) is translated to the
    standard error model: every decomposed read raises ``TreeOtherError(4009,
    NORMAL)`` ("device reported an error"), so subscribers and command flows
    see it through their normal error handling instead of polling a
    vendor-specific boolean. ``tertiarystatus`` itself always returns the raw
    dict — the diagnostic view of a faulted controller.
    """

    async def nasmythport(self, **kwargs):
        return await self._status_field('NasmythPort')

    async def tertiarystatus(self, **kwargs):
        return await self._status()

    async def angle(self, **kwargs):
        return await self._status_field('Angle')

    async def moving(self, **kwargs):
        return await self._status_field('Moving')

    async def motoron(self, **kwargs):
        return await self._status_field('MotorOn')

    async def portname(self, **kwargs):
        return await self._status_field('PortName')

    async def selectnasmythport_put(self, **kwargs):
        """Rotate M3 to a physical AutoSlew port. Parameter: ``Position`` (int or int-like str)."""
        position = kwargs.get("Position", None)
        try:
            position = int(position)
        except (TypeError, ValueError):
            raise TreeOtherError(
                address=None, code=4007,
                message=f"selectnasmythport requires an integer 'Position' parameter, got {position!r}",
                severity=TreeOtherError.SEVERITY_NORMAL) from None
        ret = await self._put("action", kind=Telescope.KIND,
                              Action='selectnasmythport', Parameters=str(position))
        if str(ret).strip().lower() != 'true':
            # AutoSlew acknowledges an accepted movement with "true"
            raise TreeOtherError(
                address=None, code=4009,
                message=f"AutoSlew refused selectnasmythport {position}: {ret!r}",
                severity=TreeOtherError.SEVERITY_NORMAL)
        return ret

    async def _status(self) -> dict:
        raw = await self._put("action", kind=Telescope.KIND,
                              Action='tertiarystatus', Parameters='')
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            raise TreeValueError(
                address=None, code=2002,
                message=f"Unparsable tertiarystatus reply from AutoSlew: {raw!r}",
                severity=TreeValueError.SEVERITY_NORMAL) from None

    async def _status_field(self, field: str):
        """Extract one field from ``tertiarystatus``, translating a controller
        fault into the standard 4009 device error."""
        status = await self._status()
        if status.get('ErrorRaised'):
            raise TreeOtherError(
                address=None, code=4009,
                message=f"M3 controller reports an error (AutoSlew ErrorRaised); "
                        f"status: {status!r}",
                severity=TreeOtherError.SEVERITY_NORMAL)
        try:
            return status[field]
        except KeyError:
            raise TreeValueError(
                address=None, code=2002,
                message=f"Field {field!r} missing in AutoSlew tertiarystatus reply: {status!r}",
                severity=TreeValueError.SEVERITY_NORMAL) from None


class MirrorCell(Device):
    """Mirror cell (M1 support / active collimation) — interface contract.

    ALPACA has no mirror-cell device type; the hardware is driven through
    vendor ``Action`` commands on the ASA ACC focuser (see ``MirrorCellACC``).
    The base class declares the interface and every method raises ``3002`` so
    a tree configured with the plain ``mirrorcell`` kind fails loudly instead
    of falling through to a nonexistent ALPACA endpoint.

    Attributes (GET): ``available``, ``mirrorcellstatus`` (raw vendor dict),
    and its decomposed fields ``positions``, ``motorstatuses``,
    ``motorstatustexts``, ``atsetpoint``, ``moving``, plus per-motor scalars
    ``motor<N>position``, ``motor<N>status``, ``motor<N>statustext``
    (``N`` = 0…``MOTOR_COUNT``-1) so telemetry can subscribe to one value per
    subject (the TimescaleDB pipeline stores one number per ``(subject,
    metric)`` — a list attribute cannot land there).
    Attributes (PUT): ``moveallmotorsto``, ``moveallmotorsoffset``,
    ``moveonemotoroffset``, ``stoponemotor``, ``stopallmotors``.

    Error-model contract (mirrors ``Tertiary``): a motor fault must surface as
    ``TreeOtherError(4009)`` "device reported an error" on every *value* read
    (``positions``, ``atsetpoint``, ``moving``, ``motor<N>position``) — never
    as a readable boolean — with per-motor position reads faulting only on
    *their own* motor, so one bad motor does not blind telemetry for the other
    two. *Status* reads (``mirrorcellstatus``, ``motorstatuses``,
    ``motorstatustexts``, ``motor<N>status``, ``motor<N>statustext``) stay
    readable while faulted: the status enum IS the fault channel, and raising
    on it would keep the fault code out of the telemetry time-series exactly
    when an alert needs it. ``available`` stays a plain capability probe.
    """
    KIND = StandardTelescopeComponents.MIRRORCELL

    #: Number of mirror-cell motors (vendor ``Index`` is 0-based: 0…2).
    MOTOR_COUNT = 3

    #: Vendor motor-status enum, shared by mirror cell and image plane.
    STATUS_TEXTS = {
        0: 'Invalid',
        1: 'MovingToSetpoint',
        2: 'TimeoutPositionControl',
        3: 'AtSetpoint',
        4: 'SpeedCtrlActive',
        5: 'MovingError',
        6: 'TemperatureMotorAboveLimit',
        7: 'MovingForward',
        8: 'MovingReverse',
        9: 'Stopped',
        10: 'HbridgeOpen',
        11: 'NoPositionInfo',
        12: 'PositionLimitViolation',
    }
    #: Statuses meaning the motor/controller is faulted → 4009 on value reads.
    #: ``HbridgeOpen`` (10) is deliberately NOT here: it is the normal idle
    #: state — drive bridge disengaged, position still valid from the encoder.
    #: Verified live 2026-08-19: all six motors on jk15 and zb08 sat in
    #: ``HbridgeOpen`` with sane, continuous positions (they reported
    #: ``Stopped`` on 2026-08-04) — treating it as a fault would make every
    #: value read raise 4009 during routine operation.
    FAULT_STATUSES = frozenset({0, 2, 5, 6, 11, 12})
    #: Statuses meaning the motor is in motion.
    MOVING_STATUSES = frozenset({1, 4, 7, 8})
    #: Status meaning the motor holds its commanded position.
    AT_SETPOINT_STATUS = 3

    #: Grammar of the per-motor scalar GET attributes, resolved by
    #: ``_find_attribute`` to the ``_motor_*`` template methods.
    _PER_MOTOR_RE = re.compile(r'motor(\d+)(position|status|statustext)$')

    def _find_attribute(self, attribute):
        match = self._PER_MOTOR_RE.fullmatch(attribute)
        if match:
            index, field = int(match.group(1)), match.group(2)
            return functools.partial(getattr(self, f'_motor_{field}'), index)
        return super()._find_attribute(attribute)

    def _not_implemented(self, method: str):
        raise TreeStructureError(
            code=3002,
            message=f"Method {method!r} is not implemented on {type(self).__name__} "
                    f"({self.sys_id}); use a vendor-specific mirror-cell kind (e.g. 'mirrorcellACC')",
            severity=TreeStructureError.SEVERITY_CRITICAL,
        )

    async def available(self, **kwargs):
        """True when the controller reports mirror-cell support (bool).

        A capability probe: it must stay readable (never raise on device state)
        so a client can ask before subscribing to the rest.
        """
        self._not_implemented('available')

    async def mirrorcellstatus(self, **kwargs):
        """Full raw status as dict: ``Available`` and ``Motors`` (see vendor doc)."""
        self._not_implemented('mirrorcellstatus')

    async def positions(self, **kwargs):
        """Motor positions in **metres**, ordered by motor index (list[float])."""
        self._not_implemented('positions')

    async def motorstatuses(self, **kwargs):
        """Motor status codes, ordered by motor index (list[int]).

        A status read: stays readable while a motor is faulted — the code is
        the fault channel itself.
        """
        self._not_implemented('motorstatuses')

    async def motorstatustexts(self, **kwargs):
        """Motor status names, ordered by motor index (list[str]).

        A status read: stays readable while a motor is faulted.
        """
        self._not_implemented('motorstatustexts')

    async def _motor_position(self, index: int, **kwargs):
        """Position of one motor in **metres** (float); serves ``motor<N>position``.

        Faults only on *its own* motor's fault status.
        """
        self._not_implemented(f'motor{index}position')

    async def _motor_status(self, index: int, **kwargs):
        """Status code of one motor (int); serves ``motor<N>status``.

        A status read: stays readable while faulted.
        """
        self._not_implemented(f'motor{index}status')

    async def _motor_statustext(self, index: int, **kwargs):
        """Status name of one motor (str); serves ``motor<N>statustext``.

        A status read: stays readable while faulted.
        """
        self._not_implemented(f'motor{index}statustext')

    async def atsetpoint(self, **kwargs):
        """True when every motor holds its commanded position (bool)."""
        self._not_implemented('atsetpoint')

    async def moving(self, **kwargs):
        """True while any motor is in motion (bool)."""
        self._not_implemented('moving')

    async def moveallmotorsto_put(self, **kwargs):
        """Move all motors to absolute positions. Parameter: ``Positions`` (metres)."""
        self._not_implemented('moveallmotorsto')

    async def moveallmotorsoffset_put(self, **kwargs):
        """Move all motors by relative offsets. Parameter: ``Offsets`` (metres)."""
        self._not_implemented('moveallmotorsoffset')

    async def moveonemotoroffset_put(self, **kwargs):
        """Move one motor by a relative offset. Parameters: ``Index`` (int), ``Offset`` (metres)."""
        self._not_implemented('moveonemotoroffset')

    async def stoponemotor_put(self, **kwargs):
        """Stop one motor. Parameter: ``Index`` (int)."""
        self._not_implemented('stoponemotor')

    async def stopallmotors_put(self, **kwargs):
        """Stop all motors."""
        self._not_implemented('stopallmotors')


class MirrorCellACC(MirrorCell):
    """ASA ACC mirror cell, driven through the ACC *focuser*'s ALPACA action channel.

    All reads are served by the single vendor action ``mirrorcell_info``
    (``{"Available": bool, "Motors": [{"Index", "Position": {"Value", "Unit"},
    "Status": {"Value", "Text"}}]}``); movement and stops by the
    ``mirrorcell_move_*`` / ``mirrorcell_stop_*`` actions.

    Wiring notes (verified on OCM 2026-08-04, ASCOM Remote Server 6.6.8419):

    * The mirror cell is **not its own ALPACA device** — the actions live on the
      ACC focuser, so every request is routed with ``kind=Focuser.KIND`` (the
      same trick as ``CoverCalibratorOCA``). The component's own
      ``device_number`` selects which focuser; on OCM that is ``0``.
    * The action reply is **double-encoded**: the ALPACA envelope's ``Value`` is
      a *string* containing JSON, hence the explicit ``json.loads`` here.
    * Positions are metres and physically µm-scale (``-9.63e-05`` = −96.3 µm).
      They are passed through unconverted — scaling belongs in whatever
      publishes telemetry, not in the tree.
    * Reads use ``ConvertPositionToAlpacaFocuserValue: false`` (native hardware
      units). The ``true`` variant returns the same reading offset by +1 mm
      while still reporting ``Unit: "m"``, so the flag — not the unit field —
      decides the convention; the tree deliberately exposes one convention.
    * ``mirrorcell_stop_all_motors`` is spelled with the trailing ``s`` (the
      vendor doc drops it in one place; the driver's ``supportedactions`` has it).

    The image plane exposes a byte-identical API under the ``imageplane_``
    prefix, so it becomes a subclass overriding ``ACTION_PREFIX`` once hardware
    reports it available (on OCM it currently reports ``Available: false``).
    """

    #: Vendor action prefix; ``imageplane`` shares the identical payload contract.
    ACTION_PREFIX = 'mirrorcell'

    async def available(self, **kwargs):
        info = await self._info()
        return bool(info.get('Available'))

    async def mirrorcellstatus(self, **kwargs):
        return await self._info()

    async def positions(self, **kwargs):
        return [self._position(m) for m in await self._motors()]

    async def motorstatuses(self, **kwargs):
        return [self._status_code(m) for m in await self._motors_raw()]

    async def motorstatustexts(self, **kwargs):
        return [self._status_text(m) for m in await self._motors_raw()]

    async def atsetpoint(self, **kwargs):
        return all(self._status_code(m) == self.AT_SETPOINT_STATUS for m in await self._motors())

    async def moving(self, **kwargs):
        return any(self._status_code(m) in self.MOVING_STATUSES for m in await self._motors())

    async def _motor_position(self, index: int, **kwargs):
        motor = await self._motor(index)
        if self._status_code(motor) in self.FAULT_STATUSES:
            self._raise_fault(motor)
        return self._position(motor)

    async def _motor_status(self, index: int, **kwargs):
        return self._status_code(await self._motor(index))

    async def _motor_statustext(self, index: int, **kwargs):
        return self._status_text(await self._motor(index))

    async def moveallmotorsto_put(self, **kwargs):
        """Move all motors to absolute positions. Parameter: ``Positions`` (metres)."""
        positions = self._require_motor_values(kwargs.get('Positions'), 'Positions')
        return await self._command(
            'move_all_motors_to',
            PositionIsAlpacaFocuserValue=False,
            Positions=[{"Value": v, "Unit": "m"} for v in positions],
        )

    async def moveallmotorsoffset_put(self, **kwargs):
        """Move all motors by relative offsets. Parameter: ``Offsets`` (metres)."""
        offsets = self._require_motor_values(kwargs.get('Offsets'), 'Offsets')
        return await self._command(
            'move_all_motors_offset',
            Offsets=[{"Value": v, "Unit": "m"} for v in offsets],
        )

    async def moveonemotoroffset_put(self, **kwargs):
        """Move one motor by a relative offset. Parameters: ``Index`` (int), ``Offset`` (metres)."""
        index = self._require_index(kwargs.get('Index'))
        offset = self._require_float(kwargs.get('Offset'), 'Offset')
        return await self._command(
            'move_one_motor_offset',
            Index=index,
            Offset={"Value": offset, "Unit": "m"},
        )

    async def stoponemotor_put(self, **kwargs):
        """Stop one motor. Parameter: ``Index`` (int)."""
        index = self._require_index(kwargs.get('Index'))
        return await self._command('stop_one_motor', Index=index)

    async def stopallmotors_put(self, **kwargs):
        """Stop all motors. Takes no parameters (vendor expects an empty string)."""
        action = f'{self.ACTION_PREFIX}_stop_all_motors'
        return self._verify_ack(action, await self._action(action, ''))

    # --- vendor plumbing -----------------------------------------------------

    async def _action(self, action: str, parameters: str):
        """Send one ACC action on the focuser device and return its raw reply."""
        return await self._put("action", kind=Focuser.KIND, Action=action, Parameters=parameters)

    async def _command(self, suffix: str, **parameters):
        """Send an actuating action and verify the vendor acknowledgement."""
        action = f'{self.ACTION_PREFIX}_{suffix}'
        return self._verify_ack(action, await self._action(action, json.dumps(parameters)))

    def _verify_ack(self, action: str, ret):
        """The driver answers ``"ok"`` on success, an error message otherwise.

        A non-``ok`` reply is a device refusal, so it becomes 4009 instead of
        being returned as if the motion had been accepted.
        """
        if str(ret).strip().lower() != 'ok':
            raise TreeOtherError(
                address=None, code=4009,
                message=f"ACC refused {action} on {self.sys_id}: {ret!r}",
                severity=TreeOtherError.SEVERITY_NORMAL)
        return ret

    async def _info(self) -> dict:
        raw = await self._action(
            f'{self.ACTION_PREFIX}_info',
            json.dumps({"ConvertPositionToAlpacaFocuserValue": False}))
        try:
            info = json.loads(raw)
        except (TypeError, ValueError):
            raise TreeValueError(
                address=None, code=2002,
                message=f"Unparsable {self.ACTION_PREFIX}_info reply from ACC: {raw!r}",
                severity=TreeValueError.SEVERITY_NORMAL) from None
        if not isinstance(info, dict):
            raise TreeValueError(
                address=None, code=2002,
                message=f"Expected a JSON object from {self.ACTION_PREFIX}_info, got {info!r}",
                severity=TreeValueError.SEVERITY_NORMAL)
        return info

    async def _motors_raw(self) -> List[dict]:
        """Motors of an available mirror cell, ordered by motor index — no fault gate.

        Serves the status reads, which must stay readable while a motor is
        faulted. Raises 2002/CRITICAL when the subsystem is absent — permanent
        for that telescope, so a SERVICE-policy subscriber stops instead of
        retrying forever.
        """
        info = await self._info()
        if not info.get('Available'):
            raise TreeValueError(
                address=None, code=2002,
                message=f"Mirror cell is not available on {self.sys_id} "
                        f"(ACC reports Available=false); read 'available' to probe "
                        f"capability without raising",
                severity=TreeValueError.SEVERITY_CRITICAL)
        motors = info.get('Motors')
        if not isinstance(motors, list) or not motors:
            raise TreeValueError(
                address=None, code=2002,
                message=f"ACC reports the mirror cell available but sent no motors: {info!r}",
                severity=TreeValueError.SEVERITY_NORMAL)
        return sorted(motors, key=self._index)

    async def _motors(self) -> List[dict]:
        """Motors of a healthy, available mirror cell — the value-read gate.

        On top of ``_motors_raw``, raises 4009/NORMAL when any motor reports a
        fault: an aggregate value composed over a faulted motor would
        masquerade as healthy data.
        """
        motors = await self._motors_raw()
        faulted = [m for m in motors if self._status_code(m) in self.FAULT_STATUSES]
        if faulted:
            self._raise_fault(faulted[0], context=motors)
        return motors

    async def _motor(self, index: int) -> dict:
        """One motor by vendor ``Index`` — no fault gate (callers decide)."""
        index = self._require_index(index)
        for motor in await self._motors_raw():
            if self._index(motor) == index:
                return motor
        raise TreeValueError(
            address=None, code=2002,
            message=f"Mirror-cell motor {index} missing in ACC reply on {self.sys_id}",
            severity=TreeValueError.SEVERITY_NORMAL)

    def _raise_fault(self, motor: dict, context=None):
        raise TreeOtherError(
            address=None, code=4009,
            message=f"Mirror-cell motor {self._index(motor)} on {self.sys_id} reports "
                    f"{self._status_text(motor)!r}; status: {context if context is not None else motor!r}",
            severity=TreeOtherError.SEVERITY_NORMAL,
            device_errno=self._status_code(motor))

    def _index(self, motor: dict) -> int:
        return self._reply_number(self._require_field(motor, 'Index'), 'Index', int)

    def _position(self, motor: dict) -> float:
        return self._reply_number(
            self._require_field(self._require_field(motor, 'Position'), 'Value'),
            'Position.Value', float)

    def _status_code(self, motor: dict) -> int:
        return self._reply_number(
            self._require_field(self._require_field(motor, 'Status'), 'Value'),
            'Status.Value', int)

    def _reply_number(self, value, field: str, convert):
        """Convert a numeric field of the vendor reply, translating garbage to 2002."""
        try:
            return convert(value)
        except (TypeError, ValueError):
            raise TreeValueError(
                address=None, code=2002,
                message=f"Field {field!r} in ACC {self.ACTION_PREFIX} reply "
                        f"is not a number: {value!r}",
                severity=TreeValueError.SEVERITY_NORMAL) from None

    def _status_text(self, motor: dict) -> str:
        """Status name, preferring the vendor's own text over the local enum."""
        status = self._require_field(motor, 'Status')
        text = status.get('Text') if isinstance(status, dict) else None
        return str(text) if text else self.STATUS_TEXTS.get(self._status_code(motor), 'Unknown')

    def _require_field(self, source, field: str):
        if not isinstance(source, dict) or field not in source:
            raise TreeValueError(
                address=None, code=2002,
                message=f"Field {field!r} missing in ACC {self.ACTION_PREFIX} reply: {source!r}",
                severity=TreeValueError.SEVERITY_NORMAL)
        return source[field]

    def _require_index(self, index) -> int:
        try:
            # bool is an int subclass and float truncation would silently pick
            # the wrong motor (int(1.9) == 1) — both must fail, not convert.
            if isinstance(index, bool) or (isinstance(index, float) and not index.is_integer()):
                raise ValueError
            index = int(index)
        except (TypeError, ValueError):
            raise TreeOtherError(
                address=None, code=4007,
                message=f"Mirror-cell commands require an integer 'Index', got {index!r}",
                severity=TreeOtherError.SEVERITY_NORMAL) from None
        if not 0 <= index < self.MOTOR_COUNT:
            raise TreeOtherError(
                address=None, code=4007,
                message=f"Mirror-cell motor 'Index' must be 0..{self.MOTOR_COUNT - 1}, got {index}",
                severity=TreeOtherError.SEVERITY_NORMAL)
        return index

    def _require_float(self, value, name: str) -> float:
        try:
            # bool would convert to a 1-metre command on a µm-scale actuator;
            # NaN/±inf would even serialize as nonstandard JSON tokens.
            if isinstance(value, bool) or not math.isfinite(number := float(value)):
                raise ValueError
            return number
        except (TypeError, ValueError):
            raise TreeOtherError(
                address=None, code=4007,
                message=f"Mirror-cell commands require a number for {name!r} (metres), "
                        f"got {value!r}",
                severity=TreeOtherError.SEVERITY_NORMAL) from None

    def _require_motor_values(self, values, name: str) -> List[float]:
        if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
            raise TreeOtherError(
                address=None, code=4007,
                message=f"{name!r} must be a list of {self.MOTOR_COUNT} numbers (metres), "
                        f"got {values!r}",
                severity=TreeOtherError.SEVERITY_NORMAL)
        if len(values) != self.MOTOR_COUNT:
            raise TreeOtherError(
                address=None, code=4007,
                message=f"{name!r} must hold exactly {self.MOTOR_COUNT} values "
                        f"(one per motor), got {len(values)}",
                severity=TreeOtherError.SEVERITY_NORMAL)
        return [self._require_float(v, name) for v in values]


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
    MirrorCell.KIND: MirrorCell,  # interface contract only — raises 3002; use a vendor kind below
    'mirrorcellACC': MirrorCellACC,  # ASA ACC mirror cell (actions on the ACC focuser)
}
