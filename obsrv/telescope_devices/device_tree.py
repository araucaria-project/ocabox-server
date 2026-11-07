import functools
import logging
import re
from datetime import datetime
from typing import Optional, Union, List, MutableMapping, Dict, Coroutine, Callable

from obcom.data_colection.coded_error import TreeStructureError
from obcom.optics import DEFAULT_REGISTRY as OPTICAL_KINDS

from obsrv.protocols import create_connector
from obsrv.utils.coordinates import check_equatorial_coordinates, check_horizontal_coordinates
from obsrv.telescope_devices.driver_registry import driver_class, resolve_deprecated_kind
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
            sys_id = self.sys_id + '.' + cid
            # One broken component must not take its siblings (and the published configuration) down with it.
            try:
                if not isinstance(op, dict) or 'kind' not in op:
                    raise ComponentConfigError(f"{sys_id}: component has no 'kind'")
                child = self._create_component(kind=op['kind'], sys_id=sys_id, parent=self, driver=op.get('driver'))
                child._setup(op)
            except Exception:
                logger.exception(f"Component {sys_id} skipped: it could not be built from the configuration")
                continue
            self.children[cid] = child
            setattr(self, cid, child)  # allow easy navigation: `parent.child`

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
    def _create_component(cls, kind: str, sys_id: str, parent: 'Component',
                          driver: Optional[str] = None) -> 'Component':
        """``kind`` says what the component is, ``driver`` how TIC talks to it. With a ``driver``, the class
        registered for that pair (:mod:`obsrv.telescope_devices.driver_registry`); without one, the TIC device
        class of ``kind`` when one exists, otherwise an :class:`OpticalElement` when the optical-path model knows
        the kind; otherwise the configuration is wrong. A vendor kind from before ``driver`` (``tertiaryOCA``)
        still builds, with a deprecation warning."""
        deprecated = resolve_deprecated_kind(kind)
        if deprecated is not None:
            generic, vendor_driver = deprecated
            logger.warning(f"{sys_id}: kind {kind!r} is deprecated, configure kind: {generic}, driver: {vendor_driver}")
            kind, driver = generic, driver or vendor_driver
        if driver is not None:
            device_class = driver_class(kind, driver)
            if device_class is None:
                raise ComponentConfigError(f"{sys_id}: no driver {driver!r} for kind {kind!r}")
            return device_class(sys_id=sys_id, parent=parent)
        device_class = _component_classes.get(kind)
        if device_class is not None:
            return device_class(sys_id=sys_id, parent=parent)
        if kind in OPTICAL_KINDS:
            return OpticalElement(sys_id=sys_id, parent=parent, kind=kind)
        raise ComponentConfigError(f"{sys_id}: unknown component kind {kind!r}")


class ComponentConfigError(ValueError):
    """The configuration of a component cannot be turned into a tree node."""


class OpticalElement(Component):
    """A component of the optical path with no device behind it in TIC: sky, flat screen, lamps,
    fibres, beam splitters, mirrors driven by another system (BESO M4/M5), a spectrograph served
    elsewhere. It keeps the configuration one tree (its ``optics:``, ``positions:``, ``paths:``,
    ``light:`` are published with the rest), has no connector, and answers every request with
    3002 — it never falls through to an ALPACA endpoint.

    When TIC gains a device class for one of these kinds, that class takes precedence."""
    KIND = "optical_element"

    def __init__(self, sys_id: str, parent: Union['Component', None], kind: str) -> None:
        super().__init__(sys_id=sys_id, parent=parent)
        self.kind = kind

    def _no_device(self, attribute: str):
        raise TreeStructureError(
            code=3002,
            message=f"{self.sys_id} ({self.kind!r}) is an optical element without a device in TIC; "
                    f"{attribute!r} cannot be served",
            severity=TreeStructureError.SEVERITY_CRITICAL,
        )

    async def get(self, attribute: str, kind=None, **data):
        self._no_device(attribute)

    async def put(self, attribute: str, kind=None, **data):
        self._no_device(attribute)


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
        """Deprecated: the flat screen's ``lampon``, for clients from before the flat screen device. Switches
        through that device, so it remembers the command."""
        return await self._flat_screen('domeflatlampon').lampon_put()

    async def domeflatlampoff_put(self, **kwargs):
        """Deprecated: the flat screen's ``lampoff``; see :meth:`domeflatlampon_put`."""
        return await self._flat_screen('domeflatlampoff').lampoff_put()

    def _flat_screen(self, method: str) -> 'FlatScreen':
        """The flat screen device of this telescope; 3002 when the configuration gives it none (a ``flatscreen``
        with a ``driver``)."""
        siblings = self.parent.children.values() if self.parent is not None else ()
        screen = next((c for c in siblings if isinstance(c, FlatScreen)), None)
        if screen is None:
            raise TreeStructureError(
                code=3002,
                message=f"Method {method!r} on {self.sys_id} needs a flat screen device: "
                        f"configure 'flatscreen' with a driver (e.g. driver: asa-autoslew)",
                severity=TreeStructureError.SEVERITY_CRITICAL,
            )
        return screen

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


class FlatScreen(Device):
    """Flat screen (dome flat) lamp — interface contract.

    The lamp's vocabulary is ``on`` / ``off``, the same values as the lamp axis of the optical path model.
    Attributes (GET): ``lamp`` — the reading; ``commanded`` — the last ``lampon`` / ``lampoff`` this TIC sent,
    ``{'lamp': 'on' | 'off' | None, 'at': ISO UTC | None}`` (``None`` since TIC started). A command is not a
    reading: clients that need proof (the optics graph) must not take ``commanded`` for one.
    Attributes (PUT): ``lampon``, ``lampoff``.

    Every method raises ``3002``: a flat screen has a device only through a ``driver`` (see
    :mod:`obsrv.telescope_devices.drivers`); without one the configuration's ``flatscreen`` is an :class:`OpticalElement`.
    """
    KIND = StandardTelescopeComponents.FLATSCREEN

    def _not_implemented(self, method: str, why: str = "use a flat screen driver (e.g. 'asa-autoslew')"):
        raise TreeStructureError(
            code=3002,
            message=f"Method {method!r} is not implemented on {type(self).__name__} ({self.sys_id}); {why}",
            severity=TreeStructureError.SEVERITY_CRITICAL,
        )

    async def lamp(self, **kwargs):
        """Whether the lamp is lit, read from the device: ``'on'`` / ``'off'``."""
        self._not_implemented('lamp')

    async def commanded(self, **kwargs):
        """The last switching command TIC sent: ``{'lamp': 'on' | 'off' | None, 'at': ISO UTC | None}``."""
        self._not_implemented('commanded')

    async def lampon_put(self, **kwargs):
        """Switch the lamp on."""
        self._not_implemented('lampon')

    async def lampoff_put(self, **kwargs):
        """Switch the lamp off."""
        self._not_implemented('lampoff')


class Tertiary(Device):
    """Tertiary (M3) mirror device — interface contract.

    ALPACA has no tertiary device type, so every implementation is
    vendor-specific (drivers in :mod:`obsrv.telescope_devices.drivers`). The base class
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
                    f"({self.sys_id}); configure a driver (e.g. driver: asa-autoslew)",
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


class MirrorCell(Device):
    """Mirror cell (M1 support / active collimation) — interface contract.

    ALPACA has no mirror-cell device type; the hardware is driven through
    vendor ``Action`` commands on the ASA ACC focuser (driver ``asa-acc``).
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
                    f"({self.sys_id}); configure a driver (e.g. driver: asa-acc)",
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


#: kind → the TIC class of a generic component (no driver); a contract-only class raises 3002 until a driver
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
    Tertiary.KIND: Tertiary,  # interface contract only — raises 3002; needs a driver
    MirrorCell.KIND: MirrorCell,  # interface contract only — raises 3002; needs a driver
}
