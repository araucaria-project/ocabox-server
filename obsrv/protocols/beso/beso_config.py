"""Loading and validation of the BESO mapping configuration (``beso_config.yml``).

The YAML is read once, validated strictly (unknown keys are errors) and turned into frozen objects with
compiled regexes, compiled conversion chains and pre-parsed command templates. Any problem raises
``BesoConfigError`` while the connector is being built, never while serving a request.

Run ``python -m obsrv.protocols.beso.beso_config [path]`` to validate a file without starting the server.
"""
import codecs
import copy
import math
import os
import re
import string
import sys
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, Mapping, Optional, Pattern, Tuple, Union

import confuse

from obsrv.protocols.beso.beso_conversion import Chain, compile_chain
from obsrv.protocols.beso.beso_errors import BesoConfigError
from obsrv.telescope_devices.standard_components import StandardTelescopeComponents

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'beso_config.yml')

KNOWN_KINDS: FrozenSet[str] = frozenset(
    value for name, value in vars(StandardTelescopeComponents).items()
    if not name.startswith('_') and isinstance(value, str))

EXPOSURE_FIELDS = frozenset({'state', 'percent', 'imageready', 'lastduration', 'laststart', 'lastfile', 'lastsnr'})
HANDLERS = frozenset({'exposure_start', 'exposure_abort'})
PRIORITIES = frozenset({'control', 'write'})

_PARAM_NAME = re.compile(r'^[a-z][a-z0-9_]*$')
_ARG_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_COMPONENT_KEY = re.compile(r'^([A-Za-z][A-Za-z0-9_]*)/(\d+)$')

_DEFAULT_SETTINGS: Dict[str, Any] = {
    'timeouts': {'connect': 5.0, 'write': 5.0, 'command': 5.0, 'close': 2.0},
    'framing': {'command_terminator': '', 'reply_terminators': ['\n', '\x00'], 'idle_gap': 0.1,
                'max_reply_bytes': 4096, 'max_command_bytes': 1023, 'encoding': 'latin-1'},
    'queue': {'control': 4, 'monitor': 2, 'write': 16, 'read': 32, 'background': 2},
    'backoff': {'initial': 1.0, 'factor': 2.0, 'maximum': 60.0, 'jitter': 0.2},
    'default_interval': 60.0,
    'min_poll_interval': 5.0,
    'max_job_seconds': 60.0,
    'keepalive_interval': 300.0,
    'log_repeat_interval': 300.0,
    'tcp_keepalive': True,
    'error_pattern': r'^\s*ERROR:',
    'forbidden_commands': [],
    'extra_kinds': [],
    'health': {'query': None, 'parse': None},
    'exposure': {
        'status_query': None,
        'min_poll': 5.0,
        'max_poll': 60.0,
        'idle_interval': 300.0,
        'readout_estimate': 25.0,
        'patterns': {'ready': None, 'exposing': None, 'reading': None, 'idle': None},
    },
}

_REQUIRED_PATTERN_GROUPS = {
    'ready': ('duration', 'file'),
    'exposing': ('exp', 'read'),
    'reading': ('read',),
    'idle': (),
}


# ---------------------------------------------------------------------------------------------- model

@dataclass(frozen=True)
class Timeouts:
    connect: float
    write: float
    command: float
    close: float


@dataclass(frozen=True)
class Framing:
    command_terminator: bytes
    reply_terminators: Tuple[bytes, ...]
    idle_gap: float
    max_reply_bytes: int
    max_command_bytes: int
    encoding: str


@dataclass(frozen=True)
class QueueLimits:
    control: int
    monitor: int
    write: int
    read: int
    background: int


@dataclass(frozen=True)
class Backoff:
    initial: float
    factor: float
    maximum: float
    jitter: float


@dataclass(frozen=True)
class ExposureSettings:
    status_query: str
    min_poll: float
    max_poll: float
    idle_interval: float
    readout_estimate: float
    ready: Pattern
    exposing: Pattern
    reading: Pattern
    idle: Pattern


@dataclass(frozen=True)
class HealthSettings:
    query: Optional[str]
    parse: Chain


@dataclass(frozen=True)
class Settings:
    timeouts: Timeouts
    framing: Framing
    queue: QueueLimits
    backoff: Backoff
    default_interval: float
    min_poll_interval: float
    max_job_seconds: float
    keepalive_interval: float
    log_repeat_interval: float
    tcp_keepalive: bool
    error_pattern: Pattern
    forbidden_commands: Tuple[Pattern, ...]
    exposure: ExposureSettings
    health: HealthSettings


@dataclass(frozen=True)
class Query:
    name: str
    command: str
    timeout: float
    expect: Optional[Pattern]
    todo: Optional[str] = None


@dataclass(frozen=True)
class CommandStep:
    template: str
    fields: Tuple[str, ...]
    timeout: float
    expect: Optional[Pattern]


@dataclass(frozen=True)
class DelayStep:
    seconds: float


@dataclass(frozen=True)
class WaitStep:
    query: str
    condition: Chain
    timeout: float
    interval: float


Step = Union[CommandStep, DelayStep, WaitStep]


@dataclass(frozen=True)
class ArgSpec:
    name: str        # placeholder name used in command templates
    source: str      # name of the client argument (Alpaca style, e.g. Duration)
    convert: Chain
    has_default: bool = False
    default: Any = None


@dataclass(frozen=True)
class AcceptSpec:
    arg: str
    values: Tuple[Any, ...]
    convert: Chain


@dataclass(frozen=True)
class WriteSpec:
    key: str
    args: Tuple[ArgSpec, ...] = ()
    steps: Tuple[Step, ...] = ()
    on_error: Tuple[Step, ...] = ()
    accept: Optional[AcceptSpec] = None
    handler: Optional[str] = None
    duration_arg: Optional[str] = None
    priority: str = 'write'
    blocked_during_exposure: bool = True
    invalidates: Tuple[str, ...] = ()
    set_markers: Tuple[str, ...] = ()
    clear_markers: Tuple[str, ...] = ()
    max_queue_wait: Optional[float] = None
    unsupported: Optional[str] = None
    todo: Optional[str] = None


@dataclass(frozen=True)
class Source:
    kind: str            # 'query' | 'exposure' | 'constant'
    ref: Any             # query name | exposure field | constant value
    parse: Chain
    optional: bool = False   # a device/conversion error yields None instead of failing the whole value


@dataclass(frozen=True)
class MarkerExpiry:
    marker: str
    after: float
    value: Any


@dataclass(frozen=True)
class ReadSpec:
    key: str
    sources: Tuple[Source, ...] = ()
    combine: Optional[Chain] = None
    interval: float = 60.0
    unsupported: Optional[str] = None
    expire_marker: Optional[MarkerExpiry] = None
    todo: Optional[str] = None

    @property
    def queries(self) -> Tuple[str, ...]:
        return tuple(s.ref for s in self.sources if s.kind == 'query')


@dataclass(frozen=True)
class ComponentSpec:
    kind: str
    device: int
    reads: Mapping[str, ReadSpec]
    writes: Mapping[str, WriteSpec]


@dataclass(frozen=True)
class BesoConfig:
    path: str
    settings: Settings
    queries: Mapping[str, Query]
    components: Mapping[Tuple[str, int], ComponentSpec]
    markers: FrozenSet[str]
    todos: Tuple[str, ...] = field(default=())

    def component(self, kind: str, device: int) -> Optional[ComponentSpec]:
        return self.components.get((kind, device))


# -------------------------------------------------------------------------------------------- loading

def load_config(path: Optional[str] = None, overrides: Optional[dict] = None) -> BesoConfig:
    """Read and validate a configuration file. ``overrides`` is deep-merged over the file (tests use it)."""
    path = os.path.abspath(path or DEFAULT_CONFIG_PATH)
    try:
        # read=False: do not pull in user/global confuse sources, only this file
        config = confuse.Configuration('BesoConnector', read=False)
        config.set_file(path)
        raw = config.get()
    except (confuse.ConfigError, OSError) as exc:
        raise BesoConfigError(f"cannot read BESO configuration {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise BesoConfigError(f"{path}: top level must be a mapping")
    if overrides:
        raw = _deep_merge(raw, overrides)
    return build_config(raw, path)


def build_config(raw: dict, path: str = '<memory>') -> BesoConfig:
    _check_keys(raw, ('settings', 'queries', 'components'), 'top level')
    settings_raw = _deep_merge(copy.deepcopy(_DEFAULT_SETTINGS), raw.get('settings') or {}, strict=True)
    builder = _Builder(path, settings_raw)
    return builder.build(raw.get('queries') or {}, raw.get('components') or {})


def _deep_merge(base: dict, extra: dict, strict: bool = False, where: str = 'settings') -> dict:
    result = dict(base)
    for key, value in extra.items():
        if strict and key not in base:
            raise BesoConfigError(f"{where}: unknown key {key!r}; allowed: {sorted(base)}")
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value, strict, f"{where}.{key}")
        else:
            result[key] = value
    return result


def _check_keys(mapping, allowed, where: str, required=()):
    if not isinstance(mapping, dict):
        raise BesoConfigError(f"{where}: expected a mapping, got {mapping!r}")
    unknown = set(mapping) - set(allowed)
    if unknown:
        raise BesoConfigError(f"{where}: unknown keys {sorted(map(str, unknown))}; allowed: {sorted(allowed)}")
    missing = [k for k in required if k not in mapping]
    if missing:
        raise BesoConfigError(f"{where}: missing keys {missing}")


def _number(value, where: str, minimum: float = -math.inf, allow_inf: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BesoConfigError(f"{where}: expected a number, got {value!r}")
    number = float(value)
    if math.isnan(number) or (math.isinf(number) and not allow_inf) or number < minimum:
        raise BesoConfigError(f"{where}: {value!r} is not a valid value (minimum {minimum})")
    return number


def _integer(value, where: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise BesoConfigError(f"{where}: expected an integer >= {minimum}, got {value!r}")
    return value


def _regex(value, where: str) -> Pattern:
    if not isinstance(value, str):
        raise BesoConfigError(f"{where}: expected a regex string, got {value!r}")
    try:
        return re.compile(value)
    except re.error as exc:
        raise BesoConfigError(f"{where}: invalid regex {value!r}: {exc}") from None


def _text(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BesoConfigError(f"{where}: expected a non-empty string, got {value!r}")
    return value


class _Builder:
    def __init__(self, path: str, settings_raw: dict):
        self.path = path
        self.settings_raw = settings_raw
        self.queries: Dict[str, Query] = {}
        self.todos = []
        self.markers_set = set()
        self.markers_used = set()
        self.settings: Optional[Settings] = None
        self._forbidden: Tuple[Pattern, ...] = ()
        self._error_pattern: Optional[Pattern] = None

    # ------------------------------------------------------------------------------------ settings
    def _build_settings(self) -> Settings:
        s = self.settings_raw
        t = s['timeouts']
        timeouts = Timeouts(*(_number(t[k], f"settings.timeouts.{k}", 0.001) for k in ('connect', 'write', 'command', 'close')))

        f = s['framing']
        encoding = _text(f['encoding'], 'settings.framing.encoding')
        try:
            codecs.lookup(encoding)
        except LookupError:
            raise BesoConfigError(f"settings.framing.encoding: unknown encoding {encoding!r}") from None
        if not isinstance(f['command_terminator'], str):
            raise BesoConfigError("settings.framing.command_terminator must be a string")
        terminators = f['reply_terminators'] or []
        if not isinstance(terminators, list) or not all(isinstance(x, str) and x for x in terminators):
            raise BesoConfigError("settings.framing.reply_terminators must be a list of non-empty strings")
        idle_gap = _number(f['idle_gap'], 'settings.framing.idle_gap', 0.0)
        if not terminators and idle_gap == 0:
            raise BesoConfigError("settings.framing: without reply_terminators the idle_gap must be > 0")
        framing = Framing(
            command_terminator=f['command_terminator'].encode(encoding),
            reply_terminators=tuple(x.encode(encoding) for x in terminators),
            idle_gap=idle_gap,
            max_reply_bytes=_integer(f['max_reply_bytes'], 'settings.framing.max_reply_bytes', 64),
            max_command_bytes=_integer(f['max_command_bytes'], 'settings.framing.max_command_bytes', 16),
            encoding=encoding,
        )

        q = s['queue']
        queue = QueueLimits(*(_integer(q[k], f"settings.queue.{k}", 1)
                              for k in ('control', 'monitor', 'write', 'read', 'background')))

        b = s['backoff']
        backoff = Backoff(
            initial=_number(b['initial'], 'settings.backoff.initial', 0.001),
            factor=_number(b['factor'], 'settings.backoff.factor', 1.0),
            maximum=_number(b['maximum'], 'settings.backoff.maximum', 0.001),
            jitter=_number(b['jitter'], 'settings.backoff.jitter', 0.0),
        )
        if backoff.maximum < backoff.initial or backoff.jitter >= 1:
            raise BesoConfigError("settings.backoff: need maximum >= initial and 0 <= jitter < 1")

        min_poll = _number(s['min_poll_interval'], 'settings.min_poll_interval', 0.0)
        default_interval = _number(s['default_interval'], 'settings.default_interval', min_poll)
        keepalive = _number(s['keepalive_interval'], 'settings.keepalive_interval', 0.0)
        if 0 < keepalive < min_poll:
            raise BesoConfigError("settings.keepalive_interval must be 0 (off) or >= min_poll_interval")

        self._error_pattern = _regex(s['error_pattern'], 'settings.error_pattern')
        forbidden = s['forbidden_commands'] or []
        if not isinstance(forbidden, list):
            raise BesoConfigError("settings.forbidden_commands must be a list of regexes")
        self._forbidden = tuple(_regex(p, f"settings.forbidden_commands[{i}]") for i, p in enumerate(forbidden))

        extra_kinds = s['extra_kinds'] or []
        if not isinstance(extra_kinds, list) or not all(isinstance(k, str) for k in extra_kinds):
            raise BesoConfigError("settings.extra_kinds must be a list of strings")
        self.kinds = KNOWN_KINDS | frozenset(extra_kinds)

        e = s['exposure']
        patterns = e['patterns']
        _check_keys(patterns, _REQUIRED_PATTERN_GROUPS, 'settings.exposure.patterns')
        compiled = {}
        for name, groups in _REQUIRED_PATTERN_GROUPS.items():
            pattern = _regex(patterns[name], f"settings.exposure.patterns.{name}")
            missing = [g for g in groups if g not in pattern.groupindex]
            if missing:
                raise BesoConfigError(f"settings.exposure.patterns.{name}: missing named groups {missing}")
            compiled[name] = pattern
        exp_min = _number(e['min_poll'], 'settings.exposure.min_poll', max(min_poll, 0.001))
        exp_max = _number(e['max_poll'], 'settings.exposure.max_poll', exp_min)
        exposure = ExposureSettings(
            status_query=_text(e['status_query'], 'settings.exposure.status_query'),
            min_poll=exp_min,
            max_poll=exp_max,
            idle_interval=_number(e['idle_interval'], 'settings.exposure.idle_interval', min_poll),
            readout_estimate=_number(e['readout_estimate'], 'settings.exposure.readout_estimate', 0.0),
            **compiled,
        )

        h = s['health']
        _check_keys(h, ('query', 'parse'), 'settings.health')
        health = HealthSettings(query=h['query'], parse=compile_chain(h['parse'], 'settings.health.parse'))

        if not isinstance(s['tcp_keepalive'], bool):
            raise BesoConfigError("settings.tcp_keepalive must be a boolean")

        return Settings(
            timeouts=timeouts, framing=framing, queue=queue, backoff=backoff,
            default_interval=default_interval, min_poll_interval=min_poll,
            max_job_seconds=_number(s['max_job_seconds'], 'settings.max_job_seconds', 0.001),
            keepalive_interval=keepalive,
            log_repeat_interval=_number(s['log_repeat_interval'], 'settings.log_repeat_interval', 0.0),
            tcp_keepalive=s['tcp_keepalive'],
            error_pattern=self._error_pattern,
            forbidden_commands=self._forbidden,
            exposure=exposure,
            health=health,
        )

    # ------------------------------------------------------------------------------------- commands
    def _check_command(self, template: str, where: str) -> Tuple[str, ...]:
        """Validate a command template; returns its placeholder names."""
        _text(template, where)
        fields = []
        literal = []
        try:
            parsed = list(string.Formatter().parse(template))
        except ValueError as exc:
            raise BesoConfigError(f"{where}: invalid template {template!r}: {exc}") from None
        for text, name, spec, conversion in parsed:
            literal.append(text)
            if name is None:
                continue
            if not _ARG_NAME.match(name) or spec or conversion:
                raise BesoConfigError(f"{where}: placeholder {{{name}}} must be a plain name; "
                                      f"formatting belongs in the conversion chain")
            fields.append(name)
            literal.append('X')
        rendered = ''.join(literal)
        if re.search(r'[a-z]', rendered):
            raise BesoConfigError(f"{where}: BESO commands are upper case (the CLI upper-cases them): {template!r}")
        for pattern in self._forbidden:
            if pattern.search(rendered):
                raise BesoConfigError(f"{where}: command {template!r} is forbidden by {pattern.pattern!r}")
        return tuple(fields)

    def _query(self, name_or_command, where: str) -> str:
        """Resolve a query reference. A string that is not a defined query name is an inline command."""
        _text(name_or_command, where)
        if name_or_command in self.queries:
            return name_or_command
        if self._check_command(name_or_command, where):
            raise BesoConfigError(f"{where}: an inline query must not contain placeholders")
        self.queries[name_or_command] = Query(name_or_command, name_or_command, self.settings.timeouts.command, None)
        return name_or_command

    def _build_queries(self, raw: dict):
        if not isinstance(raw, dict):
            raise BesoConfigError("queries: expected a mapping")
        for name, body in raw.items():
            where = f"queries.{name}"
            _check_keys(body, ('command', 'timeout', 'expect', 'todo'), where, required=('command',))
            if self._check_command(body['command'], f"{where}.command"):
                raise BesoConfigError(f"{where}: queries must not contain placeholders")
            self.queries[name] = Query(
                name=name,
                command=body['command'],
                timeout=_number(body.get('timeout', self.settings.timeouts.command), f"{where}.timeout", 0.001),
                expect=_regex(body['expect'], f"{where}.expect") if 'expect' in body else None,
                todo=body.get('todo'),
            )
            if body.get('todo'):
                self.todos.append(f"{where}: {body['todo']}")

    # ---------------------------------------------------------------------------------------- reads
    def _interval(self, value, where: str) -> float:
        if value is None:
            return self.settings.default_interval
        if value == 'connection':
            return math.inf
        interval = _number(value, where, 0.0)
        if interval < self.settings.min_poll_interval:
            raise BesoConfigError(f"{where}: {interval} s is below settings.min_poll_interval "
                                  f"({self.settings.min_poll_interval} s); BESO must not be polled that often")
        return interval

    def _source(self, body: dict, where: str, allow_optional: bool = False) -> Source:
        _check_keys(body, ('get', 'exposure', 'constant', 'parse', 'optional') if allow_optional
                    else ('get', 'exposure', 'constant', 'parse'), where)
        kinds = [k for k in ('get', 'exposure', 'constant') if k in body]
        if len(kinds) != 1:
            raise BesoConfigError(f"{where}: exactly one of get/exposure/constant is required")
        kind = kinds[0]
        optional = body.get('optional', False)
        if not isinstance(optional, bool):
            raise BesoConfigError(f"{where}.optional must be a boolean")
        if kind == 'constant':
            if 'parse' in body or optional:
                raise BesoConfigError(f"{where}: a constant takes no parse chain and cannot be optional")
            return Source('constant', body['constant'], compile_chain(None, where))
        parse = compile_chain(body.get('parse'), f"{where}.parse")
        if kind == 'exposure':
            if body['exposure'] not in EXPOSURE_FIELDS:
                raise BesoConfigError(f"{where}: unknown exposure field {body['exposure']!r}; "
                                      f"known: {sorted(EXPOSURE_FIELDS)}")
            return Source('exposure', body['exposure'], parse, optional)
        return Source('query', self._query(body['get'], f"{where}.get"), parse, optional)

    def _read(self, key: str, body: dict, where: str) -> Optional[ReadSpec]:
        if 'unsupported' in body:
            return ReadSpec(key=key, unsupported=_text(body['unsupported'], f"{where}.unsupported"))
        sources = []
        if 'sources' in body:
            for k in ('get', 'exposure', 'constant', 'parse'):
                if k in body:
                    raise BesoConfigError(f"{where}: use either 'sources' or '{k}', not both")
            raw_sources = body['sources']
            if not isinstance(raw_sources, list) or not raw_sources:
                raise BesoConfigError(f"{where}.sources: expected a non-empty list")
            sources = [self._source(s, f"{where}.sources[{i}]", allow_optional=True)
                       for i, s in enumerate(raw_sources)]
            if 'combine' not in body:
                raise BesoConfigError(f"{where}: 'sources' needs a 'combine' chain")
            combine = compile_chain(body['combine'], f"{where}.combine")
        else:
            if 'combine' in body:
                raise BesoConfigError(f"{where}: 'combine' is only valid with 'sources'")
            single = {k: body[k] for k in ('get', 'exposure', 'constant', 'parse') if k in body}
            if not single:
                return None  # write-only parameter
            sources = [self._source(single, where)]
            combine = None
        expire = None
        if 'expire_marker' in body:
            e = body['expire_marker']
            _check_keys(e, ('marker', 'after', 'value'), f"{where}.expire_marker", required=('marker', 'after', 'value'))
            expire = MarkerExpiry(_text(e['marker'], f"{where}.expire_marker.marker"),
                                  _number(e['after'], f"{where}.expire_marker.after", 0.0), e['value'])
            self.markers_used.add(expire.marker)
        has_query = any(s.kind == 'query' for s in sources)
        if 'interval' in body and not has_query:
            raise BesoConfigError(f"{where}: 'interval' only applies to parameters read from BESO queries")
        todo = body.get('todo')
        if todo:
            self.todos.append(f"{where}: {todo}")
        return ReadSpec(key=key, sources=tuple(sources), combine=combine,
                        interval=self._interval(body.get('interval'), f"{where}.interval"),
                        expire_marker=expire, todo=todo)

    # --------------------------------------------------------------------------------------- writes
    def _steps(self, raw, where: str, arg_names) -> Tuple[Tuple[Step, ...], float]:
        if not isinstance(raw, list):
            raise BesoConfigError(f"{where}: expected a list of steps")
        steps = []
        budget = 0.0
        for i, body in enumerate(raw):
            w = f"{where}[{i}]"
            if not isinstance(body, dict) or len(body) == 0:
                raise BesoConfigError(f"{w}: expected a mapping")
            if 'command' in body:
                _check_keys(body, ('command', 'expect', 'timeout'), w)
                fields = self._check_command(body['command'], f"{w}.command")
                missing = [f for f in fields if f not in arg_names]
                if missing:
                    raise BesoConfigError(f"{w}: placeholders {missing} are not declared in args")
                timeout = _number(body.get('timeout', self.settings.timeouts.command), f"{w}.timeout", 0.001)
                expect = _regex(body['expect'], f"{w}.expect") if 'expect' in body else None
                steps.append(CommandStep(body['command'], fields, timeout, expect))
                budget += timeout
            elif 'delay' in body:
                _check_keys(body, ('delay',), w)
                seconds = _number(body['delay'], f"{w}.delay", 0.0)
                steps.append(DelayStep(seconds))
                budget += seconds
            elif 'wait_until' in body:
                _check_keys(body, ('wait_until',), w)
                u = body['wait_until']
                _check_keys(u, ('get', 'parse', 'timeout', 'interval'), f"{w}.wait_until",
                            required=('get', 'parse', 'timeout'))
                query = self._query(u['get'], f"{w}.wait_until.get")
                interval = _number(u.get('interval', 1.0), f"{w}.wait_until.interval", 0.001)
                timeout = _number(u['timeout'], f"{w}.wait_until.timeout", interval)
                steps.append(WaitStep(query, compile_chain(u['parse'], f"{w}.wait_until.parse"), timeout, interval))
                budget += timeout + self.queries[query].timeout
            else:
                raise BesoConfigError(f"{w}: unknown step; use command, delay or wait_until")
        return tuple(steps), budget

    def _args(self, raw, where: str) -> Tuple[ArgSpec, ...]:
        if raw is None:
            return ()
        if not isinstance(raw, dict):
            raise BesoConfigError(f"{where}: expected a mapping of argument names")
        args = []
        for name, body in raw.items():
            w = f"{where}.{name}"
            if not isinstance(name, str) or not _ARG_NAME.match(name):
                raise BesoConfigError(f"{w}: invalid argument name")
            if isinstance(body, dict):
                _check_keys(body, ('from', 'convert', 'default'), w)
                source = body.get('from', name)
                if not isinstance(source, str) or not _ARG_NAME.match(source):
                    raise BesoConfigError(f"{w}.from: invalid argument name {source!r}")
                args.append(ArgSpec(name, source, compile_chain(body.get('convert'), f"{w}.convert"),
                                    'default' in body, body.get('default')))
            else:
                args.append(ArgSpec(name, name, compile_chain(body, w)))
        return tuple(args)

    def _write(self, key: str, body, where: str) -> WriteSpec:
        if isinstance(body, str) and body == 'unsupported':
            return WriteSpec(key=key, unsupported='not supported by BESO')
        _check_keys(body, ('args', 'steps', 'on_error', 'accept', 'handler', 'duration_arg', 'priority',
                           'blocked_during_exposure', 'invalidates', 'set_markers', 'clear_markers',
                           'max_queue_wait', 'unsupported', 'todo'), where)
        if 'unsupported' in body:
            if len(body) > 1 and set(body) - {'unsupported', 'todo'}:
                raise BesoConfigError(f"{where}: 'unsupported' excludes all other keys")
            return WriteSpec(key=key, unsupported=_text(body['unsupported'], f"{where}.unsupported"))
        handler = body.get('handler')
        if handler is not None and handler not in HANDLERS:
            raise BesoConfigError(f"{where}.handler: unknown handler {handler!r}; known: {sorted(HANDLERS)}")
        priority = body.get('priority', 'control' if handler == 'exposure_abort' else 'write')
        if priority not in PRIORITIES:
            raise BesoConfigError(f"{where}.priority: must be one of {sorted(PRIORITIES)}")
        accept = None
        if 'accept' in body:
            if 'steps' in body or handler:
                raise BesoConfigError(f"{where}: 'accept' (constant parameter) excludes steps and handlers")
            a = body['accept']
            _check_keys(a, ('arg', 'values', 'convert'), f"{where}.accept", required=('arg', 'values'))
            if not isinstance(a['values'], list) or not a['values']:
                raise BesoConfigError(f"{where}.accept.values: expected a non-empty list")
            accept = AcceptSpec(_text(a['arg'], f"{where}.accept.arg"), tuple(a['values']),
                                compile_chain(a.get('convert'), f"{where}.accept.convert"))
        args = self._args(body.get('args'), f"{where}.args")
        arg_names = {a.name for a in args}
        if len(arg_names) != len(args):
            raise BesoConfigError(f"{where}.args: duplicate argument names")
        steps, budget = self._steps(body.get('steps', []), f"{where}.steps", arg_names)
        on_error, err_budget = self._steps(body.get('on_error', []), f"{where}.on_error", arg_names)
        if accept is None and not steps:
            raise BesoConfigError(f"{where}: needs 'steps' (or 'accept' for a constant parameter)")
        if budget + err_budget > self.settings.max_job_seconds:
            raise BesoConfigError(f"{where}: steps may hold the link for {budget + err_budget:.1f} s, more than "
                                  f"settings.max_job_seconds ({self.settings.max_job_seconds} s)")
        duration_arg = body.get('duration_arg')
        if handler == 'exposure_start' and duration_arg not in {a.source for a in args if not a.has_default}:
            raise BesoConfigError(f"{where}: handler exposure_start needs 'duration_arg' naming a required argument")
        invalidates = tuple(self._query(q, f"{where}.invalidates") for q in body.get('invalidates', []))
        set_markers = tuple(_text(m, f"{where}.set_markers") for m in body.get('set_markers', []))
        clear_markers = tuple(_text(m, f"{where}.clear_markers") for m in body.get('clear_markers', []))
        self.markers_set.update(set_markers)
        blocked = body.get('blocked_during_exposure', handler != 'exposure_abort')
        if not isinstance(blocked, bool):
            raise BesoConfigError(f"{where}.blocked_during_exposure must be a boolean")
        max_wait = body.get('max_queue_wait')
        todo = body.get('todo')
        if todo:
            self.todos.append(f"{where}: {todo}")
        return WriteSpec(
            key=key, args=args, steps=steps, on_error=on_error, accept=accept, handler=handler,
            duration_arg=duration_arg, priority=priority, blocked_during_exposure=blocked,
            invalidates=invalidates, set_markers=set_markers, clear_markers=clear_markers,
            max_queue_wait=_number(max_wait, f"{where}.max_queue_wait", 0.001) if max_wait is not None else None,
            todo=todo,
        )

    # ----------------------------------------------------------------------------------- components
    def _parameter(self, key: str, body, where: str):
        if not isinstance(body, dict):
            raise BesoConfigError(f"{where}: expected a mapping")
        _check_keys(body, ('get', 'exposure', 'constant', 'parse', 'sources', 'combine', 'interval', 'put', 'arg',
                           'format', 'unsupported', 'expire_marker', 'todo'), where)
        if 'unsupported' in body:
            extra = set(body) - {'unsupported', 'todo'}
            if extra:
                raise BesoConfigError(f"{where}: 'unsupported' excludes {sorted(extra)}")
            reason = _text(body['unsupported'], f"{where}.unsupported")
            return ReadSpec(key=key, unsupported=reason), WriteSpec(key=key, unsupported=reason)
        read = self._read(key, body, where)
        write = None
        put = body.get('put')
        if isinstance(put, str) and put != 'unsupported':
            # shorthand from the user's sketch: put: "SET X {value}" + arg + format
            if 'arg' not in body:
                raise BesoConfigError(f"{where}: shorthand 'put' needs 'arg' (the client argument name)")
            write = self._write(key, {
                'args': {'value': {'from': body['arg'], 'convert': body.get('format')}},
                'steps': [{'command': put}],
                'invalidates': list(read.queries) if read else [],
            }, f"{where}.put")
        else:
            if 'arg' in body or 'format' in body:
                raise BesoConfigError(f"{where}: 'arg'/'format' only go with the shorthand string 'put'")
            if put is not None:
                if isinstance(put, dict) and read is not None and 'accept' not in put:
                    put = dict(put)
                    put.setdefault('invalidates', [])
                    put['invalidates'] = list(dict.fromkeys(list(put['invalidates']) + list(read.queries)))
                write = self._write(key, put, f"{where}.put")
        if read is None and write is None:
            raise BesoConfigError(f"{where}: defines neither a value source nor a put")
        return read, write

    def _component(self, key: str, body) -> ComponentSpec:
        match = _COMPONENT_KEY.match(str(key))
        if not match:
            raise BesoConfigError(f"components.{key}: key must be '<kind>/<device_number>', e.g. 'camera/0'")
        kind, device = match.group(1), int(match.group(2))
        if kind not in self.kinds:
            raise BesoConfigError(f"components.{key}: unknown kind {kind!r}; known: {sorted(self.kinds)}")
        _check_keys(body, ('parameters', 'actions'), f"components.{key}")
        reads, writes = {}, {}
        for name, pbody in (body.get('parameters') or {}).items():
            where = f"components.{key}.parameters.{name}"
            if not isinstance(name, str) or not _PARAM_NAME.match(name):
                raise BesoConfigError(f"{where}: parameter names are lower case identifiers")
            read, write = self._parameter(f"{key}.{name}", pbody, where)
            if read is not None:
                reads[name] = read
            if write is not None:
                writes[name] = write
        for name, abody in (body.get('actions') or {}).items():
            where = f"components.{key}.actions.{name}"
            if not isinstance(name, str) or not _PARAM_NAME.match(name):
                raise BesoConfigError(f"{where}: action names are lower case identifiers")
            if name in writes:
                raise BesoConfigError(f"{where}: {name!r} is already defined as a parameter put")
            writes[name] = self._write(f"{key}.{name}", abody, where)
        return ComponentSpec(kind, device, MappingProxyType(reads), MappingProxyType(writes))

    def build(self, queries_raw: dict, components_raw: dict) -> BesoConfig:
        self.settings = self._build_settings()
        self._build_queries(queries_raw)
        if not isinstance(components_raw, dict) or not components_raw:
            raise BesoConfigError("components: at least one component is required")
        components = {}
        for key, body in components_raw.items():
            spec = self._component(key, body)
            components[(spec.kind, spec.device)] = spec
        status = self.settings.exposure.status_query
        if status not in self.queries:
            raise BesoConfigError(f"settings.exposure.status_query: unknown query {status!r}")
        health = self.settings.health.query
        if health is not None and health not in self.queries:
            raise BesoConfigError(f"settings.health.query: unknown query {health!r}")
        orphaned = self.markers_used - self.markers_set
        if orphaned:
            raise BesoConfigError(f"markers {sorted(orphaned)} are read by expire_marker but never set by a put")
        return BesoConfig(
            path=self.path,
            settings=self.settings,
            queries=MappingProxyType(dict(self.queries)),
            components=MappingProxyType(components),
            markers=frozenset(self.markers_set),
            todos=tuple(self.todos),
        )


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    path = argv[0] if argv else DEFAULT_CONFIG_PATH
    try:
        config = load_config(path)
    except BesoConfigError as exc:
        print(f"INVALID: {exc}")
        return 1
    print(f"OK: {config.path}: {len(config.components)} components, {len(config.queries)} queries")
    for todo in config.todos:
        print(f"  TODO {todo}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
