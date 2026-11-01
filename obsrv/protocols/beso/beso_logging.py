"""Cheap suppression of repeated log messages (the same failure every poll must not flood the log)."""
import time
from typing import Callable, Dict, Hashable, Tuple


class RepeatLimiter:
    """Lets the first occurrence of a key through, then at most one message per ``interval``.

    ``check`` returns ``(should_log, suppressed)`` where ``suppressed`` is the number of occurrences
    swallowed since the last logged one. Memory is bounded by ``max_keys`` (oldest keys are evicted).
    """

    __slots__ = ('_interval', '_clock', '_max_keys', '_state')

    def __init__(self, interval: float, clock: Callable[[], float] = time.monotonic, max_keys: int = 256):
        self._interval = interval
        self._clock = clock
        self._max_keys = max_keys
        self._state: Dict[Hashable, Tuple[float, int]] = {}

    def check(self, key: Hashable) -> Tuple[bool, int]:
        now = self._clock()
        last = self._state.get(key)
        if last is None:
            if len(self._state) >= self._max_keys:
                self._state.pop(next(iter(self._state)))
            self._state[key] = (now, 0)
            return True, 0
        logged_at, suppressed = last
        if now - logged_at >= self._interval:
            self._state[key] = (now, 0)
            return True, suppressed
        self._state[key] = (logged_at, suppressed + 1)
        return False, suppressed + 1

    def reset(self, key: Hashable) -> int:
        """Forget a key (e.g. the failure ended); returns how many occurrences were suppressed."""
        last = self._state.pop(key, None)
        return last[1] if last else 0
