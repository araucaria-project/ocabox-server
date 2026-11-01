"""Declarative conversion chains for values travelling between BESO and the client.

A chain is a list of steps from the configuration, e.g.::

    parse: [kv, field: T_CCD, float, round: 1]
    format: [int: {rounding: half_up}, map: {values: {0: LOW, 1: HIGH}}]

Each item is either a bare step name or a one-key mapping ``{name: arguments}``. Chains are compiled once
(when the configuration is loaded) into tuples of plain callables, so a request only calls functions.

A new step is added by registering a factory in one place, without touching the protocol core::

    @register_step('my_step')
    def _my_step(args):            # args: whatever the YAML holds after the step name (None if bare)
        def apply(value):
            return ...
        return apply
"""
import math
import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from obsrv.protocols.beso.beso_errors import BesoConfigError, BesoConversionError

StepFn = Callable[[Any], Any]
StepFactory = Callable[[Any], StepFn]

_REGISTRY: Dict[str, StepFactory] = {}

# Exceptions a step may raise for a bad input value; anything else is a bug and propagates.
_VALUE_ERRORS = (ValueError, TypeError, KeyError, IndexError, ArithmeticError, InvalidOperation)


def register_step(name: str, *, replace: bool = False) -> Callable[[StepFactory], StepFactory]:
    """Register a step factory under ``name``. The factory gets the step arguments and returns ``fn(value)``."""
    def decorator(factory: StepFactory) -> StepFactory:
        if name in _REGISTRY and not replace:
            raise ValueError(f"conversion step {name!r} is already registered")
        _REGISTRY[name] = factory
        return factory
    return decorator


def registered_steps() -> Tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


class Chain:
    """A compiled conversion chain. Calling it runs the steps in order."""

    __slots__ = ('where', '_steps')

    def __init__(self, where: str, steps: Sequence[Tuple[str, StepFn]]):
        self.where = where
        self._steps = tuple(steps)

    def __bool__(self):
        return bool(self._steps)

    def __len__(self):
        return len(self._steps)

    def __call__(self, value):
        raw = value
        for index, (name, fn) in enumerate(self._steps):
            try:
                value = fn(value)
            except BesoConversionError:
                raise
            except _VALUE_ERRORS as exc:
                raise BesoConversionError(self.where, f"#{index + 1} {name!r}", raw, value, str(exc) or type(exc).__name__) from None
        return value


IDENTITY = Chain('identity', ())


def compile_chain(spec, where: str) -> Chain:
    """Compile a chain specification from the configuration. Errors raise ``BesoConfigError``."""
    if spec is None:
        return Chain(where, ())
    if not isinstance(spec, (list, tuple)):
        spec = [spec]
    steps = []
    for index, item in enumerate(spec):
        if isinstance(item, str):
            name, args = item, None
        elif isinstance(item, dict) and len(item) == 1:
            name, args = next(iter(item.items()))
        else:
            raise BesoConfigError(f"{where}: step #{index + 1} must be a name or a one-key mapping, got {item!r}")
        factory = _REGISTRY.get(name)
        if factory is None:
            raise BesoConfigError(f"{where}: unknown conversion step {name!r} (known: {', '.join(registered_steps())})")
        try:
            fn = factory(args)
        except BesoConfigError as exc:
            raise BesoConfigError(f"{where}: step #{index + 1} {name!r}: {exc}") from None
        except (ValueError, TypeError, KeyError, re.error) as exc:
            raise BesoConfigError(f"{where}: step #{index + 1} {name!r}: invalid arguments {args!r}: {exc}") from None
        steps.append((name, fn))
    return Chain(where, steps)


# ----------------------------------------------------------------------------------------------- helpers

def _no_args(name: str, args) -> None:
    if args is not None:
        raise BesoConfigError(f"step {name!r} takes no arguments")


def _args_dict(args, allowed: Sequence[str], required: Sequence[str] = ()) -> dict:
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise BesoConfigError(f"expected a mapping with keys {list(allowed)}, got {args!r}")
    unknown = set(args) - set(allowed)
    if unknown:
        raise BesoConfigError(f"unknown arguments {sorted(unknown)}; allowed: {list(allowed)}")
    missing = [k for k in required if k not in args]
    if missing:
        raise BesoConfigError(f"missing arguments {missing}")
    return args


def _number(value) -> float:
    """Accept int/float/numeric strings; refuse bool (True would silently become 1)."""
    if isinstance(value, bool):
        raise TypeError("boolean is not a number")
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        number = float(value.strip())
    else:
        raise TypeError(f"{type(value).__name__} is not a number")
    if not math.isfinite(number):
        raise ValueError("number is not finite")
    return number


def _decimal(value) -> Decimal:
    # 15 significant digits = float precision: -60 + 273.15 is 213.14999999999998 in binary but rounds as 213.15.
    return Decimal(format(_number(value), '.15g'))


def _map_key(value, case_insensitive: bool):
    # bool, numbers and strings get distinct tags: True == 1 must not collide in the lookup table.
    if isinstance(value, bool):
        return 'b', value
    if isinstance(value, (int, float)):
        return 'n', float(value)
    if value is None:
        return 'z', None
    if isinstance(value, str):
        text = value.strip()
        return 's', text.upper() if case_insensitive else text
    return 'o', value


# ------------------------------------------------------------------------------------------- type casts

@register_step('str')
def _step_str(args):
    _no_args('str', args)
    return lambda v: v if isinstance(v, str) else str(v)


@register_step('float')
def _step_float(args):
    _no_args('float', args)
    return _number


_ROUNDING = {
    'half_up': ROUND_HALF_UP,       # 2.5 -> 3, -2.5 -> -3 (away from zero)
    'half_even': ROUND_HALF_EVEN,   # 2.5 -> 2, 3.5 -> 4
    'floor': ROUND_FLOOR,
    'ceil': ROUND_CEILING,
    'trunc': ROUND_DOWN,
}


@register_step('int')
def _step_int(args):
    """float -> int needs an explicit rounding mode; the default ``strict`` refuses fractional values."""
    if isinstance(args, str):
        args = {'rounding': args}
    rounding = _args_dict(args, ('rounding',)).get('rounding', 'strict')
    if rounding != 'strict' and rounding not in _ROUNDING:
        raise BesoConfigError(f"rounding must be 'strict' or one of {sorted(_ROUNDING)}, got {rounding!r}")

    def apply(value):
        if isinstance(value, bool):
            raise TypeError("boolean is not an integer")
        if isinstance(value, int):
            return value
        number = _decimal(value)
        if rounding == 'strict':
            if number != number.to_integral_value():
                raise ValueError("fractional value needs an explicit rounding mode")
            return int(number)
        return int(number.quantize(Decimal(1), rounding=_ROUNDING[rounding]))
    return apply


_TRUE_WORDS = ('1', 'TRUE', 'T', 'ON', 'YES', 'Y')
_FALSE_WORDS = ('0', 'FALSE', 'F', 'OFF', 'NO', 'N')


@register_step('bool')
def _step_bool(args):
    args = _args_dict(args, ('true', 'false'))
    true_words = frozenset(str(w).strip().upper() for w in args.get('true', _TRUE_WORDS))
    false_words = frozenset(str(w).strip().upper() for w in args.get('false', _FALSE_WORDS))
    if true_words & false_words:
        raise BesoConfigError(f"words used both as true and false: {sorted(true_words & false_words)}")

    def apply(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            word = value.strip().upper()
            if word in true_words:
                return True
            if word in false_words:
                return False
        raise ValueError("not a recognised boolean")
    return apply


# ------------------------------------------------------------------------------------------ extraction

_REGEX_FLAGS = {'IGNORECASE': re.IGNORECASE, 'MULTILINE': re.MULTILINE, 'DOTALL': re.DOTALL}


@register_step('regex')
def _step_regex(args):
    """Search a string. Returns ``group`` if given; otherwise the named groups as a dict when the pattern has
    named groups, else group 1 when it has groups, else the whole match."""
    if isinstance(args, str):
        args = {'pattern': args}
    args = _args_dict(args, ('pattern', 'group', 'flags'), required=('pattern',))
    flags = 0
    for flag in args.get('flags', ()):
        try:
            flags |= _REGEX_FLAGS[flag]
        except KeyError:
            raise BesoConfigError(f"unknown regex flag {flag!r}") from None
    pattern = re.compile(args['pattern'], flags)
    group = args.get('group')
    if group is not None and not isinstance(group, (int, str)):
        raise BesoConfigError(f"group must be an int or a name, got {group!r}")
    if isinstance(group, str) and group not in pattern.groupindex:
        raise BesoConfigError(f"pattern has no group named {group!r}")
    if isinstance(group, int) and not 0 <= group <= pattern.groups:
        raise BesoConfigError(f"pattern has no group {group}")

    def apply(value):
        if not isinstance(value, str):
            raise TypeError("regex needs a string")
        match = pattern.search(value)
        if match is None:
            raise ValueError(f"pattern {pattern.pattern!r} did not match")
        if group is not None:
            return match.group(group)
        if pattern.groupindex:
            return match.groupdict()
        return match.group(1) if pattern.groups else match.group(0)
    return apply


@register_step('field')
def _step_field(args):
    if not isinstance(args, (str, int)):
        raise BesoConfigError(f"field needs a key, got {args!r}")
    key = args

    def apply(value):
        if not isinstance(value, dict):
            raise TypeError("field needs a mapping")
        return value[key]
    return apply


@register_step('item')
def _step_item(args):
    if not isinstance(args, int) or isinstance(args, bool):
        raise BesoConfigError(f"item needs an integer index, got {args!r}")
    index = args

    def apply(value):
        if not isinstance(value, (list, tuple)):
            raise TypeError("item needs a list")
        return value[index]
    return apply


@register_step('split')
def _step_split(args):
    args = _args_dict(args, ('sep', 'maxsplit'))
    sep = args.get('sep')
    maxsplit = int(args.get('maxsplit', -1))

    def apply(value):
        if not isinstance(value, str):
            raise TypeError("split needs a string")
        return value.split(sep, maxsplit)
    return apply


@register_step('kv')
def _step_kv(args):
    """``'T_OUT=16.25 T_CCD=-60.00'`` -> ``{'T_OUT': '16.25', 'T_CCD': '-60.00'}``.
    Tokens without the separator (like the ``INFO:`` prefix) are ignored."""
    args = _args_dict(args, ('pair_sep', 'kv_sep'))
    pair_sep = args.get('pair_sep')
    kv_sep = args.get('kv_sep', '=')
    if not kv_sep:
        raise BesoConfigError("kv_sep must not be empty")

    def apply(value):
        if not isinstance(value, str):
            raise TypeError("kv needs a string")
        result = {}
        for token in value.split(pair_sep):
            key, sep, val = token.partition(kv_sep)
            if sep and key.strip():
                result[key.strip()] = val.strip()
        return result
    return apply


@register_step('strip')
def _step_strip(args):
    if args is not None and not isinstance(args, str):
        raise BesoConfigError("strip takes an optional string of characters")
    chars = args

    def apply(value):
        if not isinstance(value, str):
            raise TypeError("strip needs a string")
        return value.strip(chars)
    return apply


@register_step('upper')
def _step_upper(args):
    _no_args('upper', args)
    return lambda v: v.upper() if isinstance(v, str) else _raise(TypeError("upper needs a string"))


@register_step('lower')
def _step_lower(args):
    _no_args('lower', args)
    return lambda v: v.lower() if isinstance(v, str) else _raise(TypeError("lower needs a string"))


def _raise(exc):
    raise exc


# --------------------------------------------------------------------------------------------- mapping

_MISSING = object()


@register_step('map')
def _step_map(args):
    """Map values through a dictionary. Unknown values return ``default`` if it is given (it may be null),
    otherwise they are an error. Strings are compared stripped and, by default, case-insensitively."""
    args = _args_dict(args, ('values', 'default', 'case_insensitive'), required=('values',))
    values = args['values']
    if not isinstance(values, dict) or not values:
        raise BesoConfigError("map.values must be a non-empty mapping")
    case_insensitive = bool(args.get('case_insensitive', True))
    table = {}
    for key, mapped in values.items():
        normalized = _map_key(key, case_insensitive)
        if normalized in table:
            raise BesoConfigError(f"map.values has duplicate key {key!r}")
        table[normalized] = mapped
    default = args.get('default', _MISSING)

    def apply(value):
        mapped = table.get(_map_key(value, case_insensitive), _MISSING)
        if mapped is not _MISSING:
            return mapped
        if default is not _MISSING:
            return default
        raise ValueError("value not in map")
    return apply


@register_step('const')
def _step_const(args):
    return lambda _value: args


@register_step('equals')
def _step_equals(args):
    expected = _map_key(args, True)
    return lambda v: _map_key(v, True) == expected


@register_step('not')
def _step_not(args):
    _no_args('not', args)

    def apply(value):
        if not isinstance(value, bool):
            raise TypeError("not needs a boolean")
        return not value
    return apply


# ---------------------------------------------------------------------------------------------- numbers

@register_step('scale')
def _step_scale(args):
    """``value * factor + offset`` (unit change), e.g. ``scale: {offset: -273.15}``."""
    args = _args_dict(args, ('factor', 'offset'))
    factor = _number(args.get('factor', 1.0))
    offset = _number(args.get('offset', 0.0))
    return lambda v: _number(v) * factor + offset


@register_step('round')
def _step_round(args):
    """Round half away from zero to ``args`` decimal places; returns float."""
    if not isinstance(args, int) or isinstance(args, bool) or args < 0:
        raise BesoConfigError(f"round needs a non-negative number of decimals, got {args!r}")
    quantum = Decimal(1).scaleb(-args)
    return lambda v: float(_decimal(v).quantize(quantum, rounding=ROUND_HALF_UP))


def _bounds(args):
    args = _args_dict(args, ('min', 'max'))
    low = _number(args['min']) if 'min' in args else -math.inf
    high = _number(args['max']) if 'max' in args else math.inf
    if low > high:
        raise BesoConfigError(f"min {low} is greater than max {high}")
    return low, high


@register_step('clamp')
def _step_clamp(args):
    low, high = _bounds(args)

    def apply(value):
        clamped = min(max(_number(value), low), high)
        return int(clamped) if isinstance(value, int) and math.isfinite(clamped) else clamped
    return apply


@register_step('check_range')
def _step_check_range(args):
    """Like ``clamp`` but refuses out-of-range values instead of changing them (use for client input)."""
    low, high = _bounds(args)

    def apply(value):
        number = _number(value)
        if not low <= number <= high:
            raise ValueError(f"value outside allowed range [{low}, {high}]")
        return value
    return apply


@register_step('format')
def _step_format(args):
    """Python format spec applied to the value, e.g. ``.2f``, ``+d``, ``>5``."""
    if not isinstance(args, str):
        raise BesoConfigError(f"format needs a format spec string, got {args!r}")
    spec = args
    for probe in (0, 0.0, ''):  # fail at load time on a spec that fits no value type
        try:
            format(probe, spec)
            break
        except (ValueError, TypeError):
            continue
    else:
        raise BesoConfigError(f"invalid format spec {spec!r}")
    return lambda v: format(v, spec)


@register_step('template')
def _step_template(args):
    """``str.format`` template; a tuple/list value is spread as positional arguments, other values are ``{0}``."""
    if not isinstance(args, str):
        raise BesoConfigError(f"template needs a string, got {args!r}")
    template = args

    def apply(value):
        if isinstance(value, (list, tuple)):
            return template.format(*value)
        return template.format(value)
    return apply


# ------------------------------------------------------------------------------------------- date/time

@register_step('timestamp')
def _step_timestamp(args):
    """Parse a date/time string to a unix timestamp. ``format`` is a ``strptime`` format or ``iso``;
    naive times are interpreted in ``utc_offset`` hours (default 0 = UTC)."""
    if isinstance(args, str):
        args = {'format': args}
    args = _args_dict(args, ('format', 'utc_offset'))
    fmt = args.get('format', 'iso')
    tz = timezone(timedelta(hours=_number(args.get('utc_offset', 0))))

    def apply(value):
        if not isinstance(value, str):
            raise TypeError("timestamp needs a string")
        text = value.strip()
        parsed = datetime.fromisoformat(text) if fmt == 'iso' else datetime.strptime(text, fmt)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.timestamp()
    return apply


_JD_UNIX_EPOCH = 2440587.5


@register_step('jd_to_unix')
def _step_jd_to_unix(args):
    _no_args('jd_to_unix', args)
    return lambda v: (_number(v) - _JD_UNIX_EPOCH) * 86400.0


# ----------------------------------------------------------------------------------- combining sources

def _sequence(value) -> Sequence:
    if not isinstance(value, (list, tuple)):
        raise TypeError("needs a list of source values")
    return value


@register_step('first_not_null')
def _step_first_not_null(args):
    _no_args('first_not_null', args)

    def apply(value):
        for item in _sequence(value):
            if item is not None:
                return item
        raise ValueError("all sources are null")
    return apply


@register_step('max')
def _step_max(args):
    _no_args('max', args)
    return lambda v: max(x for x in _sequence(v) if x is not None)


@register_step('min')
def _step_min(args):
    _no_args('min', args)
    return lambda v: min(x for x in _sequence(v) if x is not None)


@register_step('any')
def _step_any(args):
    _no_args('any', args)
    return lambda v: any(bool(x) for x in _sequence(v))


@register_step('all')
def _step_all(args):
    _no_args('all', args)
    return lambda v: all(bool(x) for x in _sequence(v))


@register_step('sum')
def _step_sum(args):
    _no_args('sum', args)
    return lambda v: sum(_number(x) for x in _sequence(v))


def apply_optional(chain: Optional[Chain], value):
    return chain(value) if chain else value
