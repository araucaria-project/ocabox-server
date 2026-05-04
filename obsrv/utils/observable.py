"""Asyncio-friendly Observable[T] for push-on-change semantics.

The Observable wraps a value and fires registered watchers when the value
changes. Watchers fire **once per event-loop tick**, not once per ``set``
call — multiple ``set()`` calls within a single coroutine collapse into
one notification carrying the final value. This is correct for "what is
the current state?" semantics; it is wrong for "what is the change
history?". If you need a change log, use a separate append-only
structure — do not derive it from this Observable.

Construction-time initial state is set via ``__init__`` and does not go
through ``set``. Pre-loop mutation through ``set`` is a programming error
and raises ``RuntimeError`` (asyncio.get_running_loop's contract).

For in-place mutation of mutable values (``obs.get().append(x)``), use
``touch()`` to fire watchers without re-assigning. Prefer building fresh
values where practical: ``obs.set([*obs.get(), x])``.

See ``doc/errors.md`` for how observable wakeups interact with the
freezer/cache cycle-query layer, and the ecosystem vault note
``Architecture/Observable Pattern for ocabox-server.md`` for the design
rationale and wiring patterns.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Generic, Optional, TypeVar

T = TypeVar('T')

Watcher = Callable[[T], Optional[Awaitable[None]]]


class Observable(Generic[T]):
    """A mutable value with a watcher list, asyncio-friendly.

    Methods:
        ``get()`` returns the current value.
        ``set(v)`` mutates the value; if it differs (``!=``) from the
        current one, schedules a watcher fire on the next event-loop tick.
        Equal-value reassignments are no-ops.
        ``touch()`` fires watchers without changing the value — for
        in-place mutation cases.
        ``subscribe(fn)`` registers a watcher and returns an
        idempotent unsubscribe callable.

    Watchers may be sync or async; async ones are wrapped in
    ``asyncio.create_task`` when fired.
    """

    __slots__ = ('_value', '_watchers', '_dirty')

    def __init__(self, initial: T) -> None:
        self._value: T = initial
        self._watchers: list[Watcher] = []
        self._dirty: bool = False

    def get(self) -> T:
        return self._value

    def set(self, new: T) -> None:
        """Set the value. Must be called from a running event loop.

        If ``new == self._value``, this is a no-op. Otherwise the value
        is updated immediately and a watcher fire is scheduled for the
        next event-loop tick. Multiple ``set()`` calls within a single
        coroutine collapse into one fire carrying the final value.

        Raises ``RuntimeError`` if called outside a running loop —
        construction-time mutation is a programming error, not a state
        to silently absorb.
        """
        if new == self._value:
            return
        self._value = new
        self._schedule_fire()

    def touch(self) -> None:
        """Fire watchers without changing the value.

        Use when the value was mutated in place (``obs.get().append(x)``)
        and the equality-guard in ``set()`` would suppress the
        notification. Prefer building fresh values where practical:
        ``obs.set([*obs.get(), x])``. ``touch()`` is the escape hatch
        for cases where building a fresh value is genuinely impractical.
        """
        self._schedule_fire()

    def subscribe(self, watcher: Watcher) -> Callable[[], None]:
        """Register a watcher. Returns an idempotent unsubscribe callable."""
        self._watchers.append(watcher)
        watchers = self._watchers

        def unsubscribe() -> None:
            try:
                watchers.remove(watcher)
            except ValueError:
                pass

        return unsubscribe

    def _schedule_fire(self) -> None:
        if self._dirty:
            return
        self._dirty = True
        # Raises RuntimeError if no running loop — pre-loop mutation
        # is a programming error.
        asyncio.get_running_loop().call_soon(self._fire)

    def _fire(self) -> None:
        self._dirty = False
        v = self._value
        for w in list(self._watchers):
            r = w(v)
            if asyncio.iscoroutine(r):
                asyncio.create_task(r)
