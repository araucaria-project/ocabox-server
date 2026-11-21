"""The last value of a NATS subject (the detector temperature), kept by a serverish callback subscriber. The
subscription is made by ``CameraHandlers._telemetry_for``; ``IrisCcdConnector._telemetry`` keeps one
:class:`TelemetryValue` per subject."""
import asyncio
import logging
import time
from datetime import timezone
from typing import Any, Optional, Sequence

from serverish.base.datetime import dt_ensure_datetime

logger = logging.getLogger('iris_ccd_connector')     # one logger for the whole IRIS CCD connector


class TelemetryValue:
    """The last value of a NATS subject (detector temperature), kept by a serverish callback subscriber."""

    def __init__(self, subject: str, value_path: Sequence[str], timestamp_path: Optional[Sequence[str]]):
        self.subject = subject
        self.value_path = list(value_path)
        self.timestamp_path = list(timestamp_path) if timestamp_path else None
        self.value: Optional[float] = None
        self.ts: Optional[float] = None
        self.received = asyncio.Event()
        self.subscriber = None
        self.reading_task: Optional[asyncio.Task] = None
        self.reader_error: Optional[str] = None
        self.next_subscribe = 0.0  # monotonic time before which a failed subscription is not retried
        self._warned: set = set()

    def _warn_once(self, what: str, message: str) -> None:
        if what not in self._warned:
            self._warned.add(what)
            logger.warning(message)

    @staticmethod
    def _walk(node: Any, path: Sequence[str]) -> Any:
        for key in path:
            if not isinstance(node, dict) or key not in node:
                raise KeyError('.'.join(path))
            node = node[key]
        return node

    def _timestamp(self, data: dict) -> float:
        if self.timestamp_path is None:
            return time.time()
        try:
            raw = self._walk(data, self.timestamp_path)
            when = dt_ensure_datetime(float(raw) if isinstance(raw, (int, float)) else raw)
            if when is None:
                raise ValueError('empty')
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return when.timestamp()
        except (KeyError, TypeError, ValueError) as e:
            self._warn_once('ts', f"{self.subject}: no measurement time at {'.'.join(self.timestamp_path)} ({e}), "
                                  f"using the time of arrival")
            return time.time()

    async def on_message(self, data: dict, meta: dict) -> None:
        try:
            value = float(self._walk(data, self.value_path))
        except (KeyError, TypeError, ValueError) as e:
            self._warn_once('value', f"{self.subject}: message without a number at {'.'.join(self.value_path)} "
                                     f"({e}), ignored")
            return
        self.value = value
        self.ts = self._timestamp(data)
        self.received.set()

    def reader_alive(self) -> bool:
        return self.reading_task is not None and not self.reading_task.done()
