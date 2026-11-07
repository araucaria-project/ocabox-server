"""Registry of component drivers.

A component's ``kind`` says what it is (generic, the same for every telescope); its ``driver`` names the vendor
mechanism through which TIC talks to it (``asa-autoslew``, ``asa-acc``). The kind's class in
:mod:`obsrv.telescope_devices.device_tree` is the contract; a driver class implements it and registers here with
:func:`driver`. Driver classes live in :mod:`obsrv.telescope_devices.drivers`, one module per driver — nothing
vendor-specific belongs in the generic tree.

This module imports nothing from the tree, so ``device_tree`` can consult it while the driver modules (which
subclass the tree's contracts) are imported after the tree.
"""
from typing import TYPE_CHECKING, Callable, Optional, Tuple, Type, TypeVar

if TYPE_CHECKING:
    from obsrv.telescope_devices.device_tree import Component

C = TypeVar('C', bound='Type[Component]')

_drivers: dict[Tuple[str, str], 'Type[Component]'] = {}
_deprecated_kinds: dict[str, Tuple[str, str]] = {}


def driver(kind: str, name: str, *, deprecated_kind: Optional[str] = None) -> Callable[[C], C]:
    """Class decorator: the decorated class serves components of ``kind`` configured with ``driver: name``.

    ``deprecated_kind`` is the vendor kind this driver was configured as before ``driver`` existed
    (``tertiaryOCA``); such a configuration still builds, with a warning naming the ``kind`` + ``driver`` to use.
    """
    key = (kind, name)

    def register(cls: C) -> C:
        if key in _drivers:
            raise RuntimeError(f"driver {name!r} for kind {kind!r} registered twice "
                               f"({_drivers[key].__qualname__}, {cls.__qualname__})")
        _drivers[key] = cls
        if deprecated_kind is not None:
            _deprecated_kinds[deprecated_kind] = key
        return cls

    return register


def driver_class(kind: str, name: str) -> 'Optional[Type[Component]]':
    """The class registered for ``kind`` with ``driver: name``; ``None`` when there is none."""
    return _drivers.get((kind, name))


def resolve_deprecated_kind(kind: str) -> Optional[Tuple[str, str]]:
    """``(kind, driver)`` for a deprecated vendor kind; ``None`` for any other kind."""
    return _deprecated_kinds.get(kind)
