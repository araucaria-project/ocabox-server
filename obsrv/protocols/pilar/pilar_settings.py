"""``pilar_config.yml`` of the Pilar connector: read, checked and given typed access.

:class:`PilarSettings` is built once, when the connector is created; a missing or wrong key raises
:class:`PilarConfigError` naming the path of the key.
"""
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

import confuse

from obsrv.protocols.pilar.pilar_exceptions import PilarConfigError

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'pilar_config.yml')


def load_protocol_config(path: str = CONFIG_PATH) -> dict:
    config = confuse.Configuration('PilarConnector', __name__, read=False)
    config.set_file(path)
    return config.get()


class _Section:
    """A part of the configuration with typed, checked access; errors name the path of the key."""

    def __init__(self, data: Any, path: str):
        if not isinstance(data, dict):
            raise PilarConfigError(f"{path}: must be a mapping")
        self.data = data
        self.path = path

    def _get(self, key: str):
        if key not in self.data or self.data[key] is None:
            raise PilarConfigError(f"{self.path}.{key}: missing")
        return self.data[key]

    def num(self, key: str) -> float:
        value = self._get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PilarConfigError(f"{self.path}.{key}: must be a number, got {value!r}")
        return float(value)

    def int(self, key: str) -> int:
        value = self._get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise PilarConfigError(f"{self.path}.{key}: must be an integer, got {value!r}")
        return value

    def str(self, key: str) -> str:
        value = self._get(key)
        if not isinstance(value, str) or not value.strip():
            raise PilarConfigError(f"{self.path}.{key}: must be a TPL2 variable name, got {value!r}")
        return value.strip()

    def value(self, key: str):
        return self._get(key)

    def section(self, key: str) -> '_Section':
        return _Section(self._get(key), f'{self.path}.{key}')

    def optional_section(self, key: str) -> '_Section':
        data = self.data.get(key)
        return _Section(data if data is not None else {}, f'{self.path}.{key}')


@dataclass
class MappingEntry:
    """One method of the mapping: a TPL2 variable or a procedure of the connector with its options."""
    variable: Optional[str] = None
    procedure: Optional[str] = None
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ComponentMapping:
    get: Dict[str, MappingEntry]
    put: Dict[str, MappingEntry]
    actions: Dict[str, MappingEntry]
    fixed: Dict[str, Any]


class PilarSettings:
    """``pilar_config.yml`` read and checked once, when the connector is created."""

    def __init__(self, cfg: dict, procedures: Dict[str, Tuple[str, str]]):
        root = _Section(cfg, 'pilar_config')
        link = root.section('link')
        self.encoding = link.str('encoding')
        self.pool_size = link.int('pool_size')
        self.reserved_for_stop = link.int('reserved_for_stop')
        self.long_connections = link.int('long_connections')
        ids = link.value('command_ids')
        if not (isinstance(ids, list) and len(ids) == 2 and all(isinstance(i, int) for i in ids)
                and 0 < ids[0] < ids[1]):
            raise PilarConfigError("pilar_config.link.command_ids: must be [first, last], 0 < first < last")
        self.command_ids = (ids[0], ids[1])
        self.reconnect_cooldown = link.num('reconnect_cooldown')
        if self.reserved_for_stop < 0 or self.pool_size < self.reserved_for_stop + 2:
            raise PilarConfigError("pilar_config.link.pool_size: must be at least reserved_for_stop + 2 "
                                   "(one connection of the watcher and one for everything else)")

        t = root.section('timeouts')
        self.connect_timeout = t.num('connect')
        self.greeting_timeout = t.num('greeting')
        self.command_timeout = t.num('command')
        self.pool_wait = t.num('pool_wait')
        self.stalled_connection = t.num('stalled_connection')

        w = root.section('watcher')
        self.poll_interval = w.num('poll_interval')
        self.snapshot_max_age = w.num('snapshot_max_age')
        self.freeze_after = w.num('freeze_after')
        self.freeze_suspect_after = w.num('freeze_suspect_after')
        self.freeze_snapshot_max_age = w.num('freeze_snapshot_max_age')
        self.post_freeze_guard = w.num('post_freeze_guard')
        self.outage_after = w.num('outage_after')
        self.command_freeze_wait = w.num('command_freeze_wait')
        self.retrack_attempts = w.int('retrack_attempts')
        self.retrack_retry = w.num('retrack_retry')
        self.retrack_window = w.num('retrack_window')

        s = root.section('state')
        self.state_ready = s.str('ready_state')
        self.ready_value = s.num('ready_value')
        self.off_value = s.num('off_value')
        self.state_track = s.str('track')
        self.tracking_value = s.num('tracking_value')
        self.state_azimuth = s.str('azimuth')
        self.state_altitude = s.str('altitude')

        m = root.section('mount')
        self.slew_speed = m.num('slew_speed')
        self.speed_window = m.num('speed_window')
        self.settle_time = m.num('settle_time')
        self.arrival_tolerance = m.num('arrival_tolerance')
        self.start_delay = {'altaz': m.num('start_delay_altaz'), 'track': m.num('start_delay_track'),
                            'abort': m.num('start_delay_abort')}
        self.max_slew_time = m.num('max_slew_time')
        self.sync_slew_timeout = m.num('sync_slew_timeout')
        self.queue_wait = m.num('queue_wait')

        p = root.section('procedures')
        self.power = p.section('power')
        self.slew_altaz = p.section('slew_altaz')
        self.slew_equatorial = p.section('slew_equatorial')
        self.tracking = p.section('tracking')
        self.abort_slew = p.section('abort_slew')
        self.cover = p.section('cover')
        self.focuser = p.section('focuser')
        self.derotator = p.section('derotator')
        self.motor_status = p.section('motor_status')
        self._check_procedures()

        self.components: Dict[str, ComponentMapping] = {}
        comps = root.section('components')
        for kind, data in comps.data.items():
            c = _Section(data, f'pilar_config.components.{kind}')
            self.components[kind] = ComponentMapping(
                get=self._entries(c.optional_section('get'), procedures, 'get'),
                put=self._entries(c.optional_section('put'), procedures, 'put'),
                actions=self._entries(c.optional_section('actions'), procedures, None),
                fixed={str(k).lower(): v for k, v in c.optional_section('fixed').data.items()},
            )

    def _check_procedures(self) -> None:
        """Read every key the procedures use, so a typo shows when the connector is created."""
        for key in ('ready',):
            self.power.str(key)
        for key in ('timeout_on', 'timeout_off'):
            self.power.num(key)
        for section in (self.slew_altaz, self.abort_slew):
            for key in ('azimuth', 'altitude', 'track'):
                section.str(key)
            section.num('track_value')
        self.slew_altaz.str('zd_limit')
        epoch = self.slew_equatorial.value('epoch')
        if not (isinstance(epoch, list) and epoch and all(isinstance(v, str) for v in epoch)):
            raise PilarConfigError("pilar_config.procedures.slew_equatorial.epoch: must be a list of variables")
        before = self.slew_equatorial.data.get('set_before') or {}
        if not isinstance(before, dict):
            raise PilarConfigError("pilar_config.procedures.slew_equatorial.set_before: must be a mapping")
        for key in ('ra', 'dec', 'track'):
            self.slew_equatorial.str(key)
        self.slew_equatorial.num('track_value')
        for key in ('track', 'current_ra', 'current_dec'):
            self.tracking.str(key)
        for key in ('on_value', 'off_value'):
            self.tracking.num(key)
        for key in ('target', 'position'):
            self.cover.str(key)
        for key in ('open_value', 'closed_value', 'tolerance', 'timeout'):
            self.cover.num(key)
        states = self.cover.section('states')
        for key in ('closed', 'moving', 'open', 'unknown'):
            states.int(key)
        for key in ('position', 'minimum', 'maximum', 'syncmode', 'target', 'track'):
            self.focuser.str(key)
        self.focuser.int('syncmode_bit')
        for key in ('track_value', 'steps_per_mm', 'tolerance', 'still_time', 'speed', 'timeout_margin'):
            self.focuser.num(key)
        for section in (self.focuser, self.derotator):
            if section.int('attempts') < 1:
                raise PilarConfigError(f"{section.path}.attempts: must be at least 1")
        if self.focuser.num('steps_per_mm') <= 0 or self.focuser.num('speed') <= 0:
            raise PilarConfigError("pilar_config.procedures.focuser: steps_per_mm and speed must be > 0")
        for key in ('position', 'target', 'minimum', 'maximum', 'syncmode', 'offset', 'track'):
            self.derotator.str(key)
        for key in ('tracking_syncmode', 'track_value', 'tolerance', 'tracking_tolerance', 'still_speed',
                    'still_time', 'timeout'):
            self.derotator.num(key)
        axes = self.motor_status.section('axes')
        if not axes.data:
            raise PilarConfigError("pilar_config.procedures.motor_status.axes: at least one axis")
        for key in axes.data:
            axes.str(key)
        self.motor_status.section('states')

    @staticmethod
    def _entries(section: _Section, procedures: Dict[str, Tuple[str, str]], mode: Optional[str]) \
            -> Dict[str, MappingEntry]:
        entries = {}
        for method, spec in section.data.items():
            path = f'{section.path}.{method}'
            if isinstance(spec, str) and spec.strip():
                entries[str(method).lower()] = MappingEntry(variable=spec.strip())
                continue
            if not isinstance(spec, dict) or not isinstance(spec.get('procedure'), str):
                raise PilarConfigError(f"{path}: must be a TPL2 variable or {{procedure: <name>, ...}}")
            name = spec['procedure']
            if name not in procedures:
                raise PilarConfigError(f"{path}: unknown procedure {name!r} (known: {', '.join(sorted(procedures))})")
            if mode is not None and procedures[name][1] != mode:
                raise PilarConfigError(f"{path}: procedure {name!r} is a {procedures[name][1]} procedure")
            options = {k: v for k, v in spec.items() if k != 'procedure'}
            if not all(isinstance(k, str) for k in options):
                raise PilarConfigError(f"{path}: option names must be text (YAML reads on/off/yes/no as true/false)")
            entries[str(method).lower()] = MappingEntry(procedure=name, options=options)
        return entries
