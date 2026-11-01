"""Exposure tracking for BESO.

BESO runs exposures in NOWAIT mode and reports progress only through ``GET CAMERA STATUS``. That command
also writes the FITS file once read-out is over, and a new START destroys an unread frame, so the connector
must keep polling until the READY reply arrives, whether or not a client watches.

The tracker is fed every status reply (from any request) and extrapolates between polls from the seconds
left that BESO reported, so subscribers of ``camerastate``/``percentcompleted`` cause no traffic to BESO.
"""
import enum
import logging
import math
import time
from typing import Callable, Optional, Pattern

from obsrv.protocols.beso.beso_config import ExposureSettings
from obsrv.protocols.beso.beso_errors import BesoConversionError, BesoNoDataError

logger = logging.getLogger(__name__.rsplit('.')[-1])


class Phase(enum.Enum):
    UNKNOWN = 'unknown'      # not synchronised with BESO (startup, after a lost link)
    IDLE = 'idle'
    STARTING = 'starting'    # startexposure accepted locally, START not confirmed yet
    EXPOSING = 'exposing'    # EXPOSING/READING/DOWNLOAD are refined by extrapolation in `live_phase`
    READING = 'reading'
    ERROR = 'error'


class CameraState(enum.IntEnum):
    IDLE = 0
    WAITING = 1
    EXPOSING = 2
    READING = 3
    DOWNLOAD = 4
    ERROR = 5


class ExposureTracker:
    def __init__(self, settings: ExposureSettings, error_pattern: Pattern,
                 clock: Callable[[], float] = time.monotonic, wallclock: Callable[[], float] = time.time):
        self._s = settings
        self._error_pattern = error_pattern
        self._clock = clock
        self._wallclock = wallclock
        self.phase = Phase.UNKNOWN
        self.was_active = False           # UNKNOWN reached from an active exposure -> keep polling
        self.status_at: Optional[float] = None
        self.error_text: Optional[str] = None
        self.image_ready = False
        self.last_duration: Optional[float] = None
        self.last_start: Optional[float] = None
        self.last_file: Optional[str] = None
        self.last_snr: Optional[float] = None
        self._duration: Optional[float] = None
        self._readout = settings.readout_estimate
        self._sample_at = 0.0
        self._exp_left = 0.0
        self._read_left = 0.0
        self._total: Optional[float] = None
        self._before_start = Phase.UNKNOWN

    # ------------------------------------------------------------------------------- transitions
    def on_status_reply(self, reply: str) -> None:
        """Update from a ``GET CAMERA STATUS`` reply (or any reply in that format)."""
        now = self._clock()
        s = self._s
        match = s.ready.search(reply)
        if match:
            self._on_ready(match, now)
            return
        match = s.exposing.search(reply)
        if match:
            self._on_progress(Phase.EXPOSING, float(match.group('exp')), float(match.group('read')), now)
            return
        match = s.reading.search(reply)
        if match:
            self._on_progress(Phase.READING, 0.0, float(match.group('read')), now)
            return
        if s.idle.search(reply):
            if self.phase in (Phase.EXPOSING, Phase.READING) or (self.phase == Phase.UNKNOWN and self.was_active):
                logger.warning("BESO reports IDLE but an exposure was being tracked; the frame was collected or "
                               "aborted elsewhere. Reply: %r", reply)
            self._set_idle(now)
            return
        if self._error_pattern.search(reply):
            self.phase = Phase.ERROR
            self.error_text = reply
            self.status_at = now
            self.was_active = False
            return
        raise BesoConversionError('exposure status', 'status patterns', reply, reply,
                                  'reply matches none of settings.exposure.patterns')

    def _on_ready(self, match, now: float) -> None:
        self.last_duration = float(match.group('duration'))
        self.last_file = match.group('file')
        snr = match.groupdict().get('snr')
        self.last_snr = float(snr) if snr else None
        self.image_ready = True
        if self._duration is None:
            # Frame collected without a start seen by this connector (e.g. after a restart).
            self.last_start = None
        self._set_idle(now)

    def _on_progress(self, phase: Phase, exp_left: float, read_left: float, now: float) -> None:
        if phase == Phase.EXPOSING:
            self._readout = read_left  # while exposing, BESO reports the full read-out time
        if self._duration is not None:
            self._total = self._duration + self._readout
        elif self._total is None:
            # Exposure picked up mid-way (connector restart): the elapsed part is unknown.
            self._total = exp_left + self._readout
        self.phase = phase
        self._sample_at = now
        self._exp_left = exp_left
        self._read_left = read_left
        self.status_at = now
        self.was_active = False
        self.image_ready = False

    def _set_idle(self, now: float) -> None:
        self.phase = Phase.IDLE
        self.status_at = now
        self.was_active = False
        self.error_text = None
        self._total = None
        self._duration = None

    def mark_starting(self) -> None:
        self._before_start = self.phase
        self.phase = Phase.STARTING
        self.image_ready = False

    def mark_started(self, duration: float) -> None:
        now = self._clock()
        self._duration = duration
        self._total = duration + self._readout
        self.last_start = self._wallclock()
        self.phase = Phase.EXPOSING
        self._sample_at = now
        self._exp_left = duration
        self._read_left = self._readout

    def mark_start_failed(self, outcome_unknown: bool) -> None:
        if self.phase != Phase.STARTING:
            return
        if outcome_unknown:
            self.phase = Phase.UNKNOWN
            self.was_active = True
        else:
            self.phase = self._before_start

    def mark_unknown(self) -> None:
        """The link was lost: whatever BESO does now must be read again."""
        active = self.is_busy()
        self.phase = Phase.UNKNOWN
        self.was_active = self.was_active or active

    # ---------------------------------------------------------------------------------- queries
    def _left(self, now: float):
        elapsed = max(0.0, now - self._sample_at)
        exp_left = max(0.0, self._exp_left - elapsed)
        total_left = max(0.0, self._exp_left + self._read_left - elapsed)
        return exp_left, total_left

    def live_state(self) -> CameraState:
        phase = self.phase
        if phase == Phase.IDLE:
            return CameraState.IDLE
        if phase == Phase.STARTING:
            return CameraState.WAITING
        if phase == Phase.ERROR:
            return CameraState.ERROR
        if phase in (Phase.EXPOSING, Phase.READING):
            exp_left, total_left = self._left(self._clock())
            if exp_left > 0:
                return CameraState.EXPOSING
            if total_left > 0:
                return CameraState.READING
            return CameraState.DOWNLOAD  # read-out over, waiting for the status poll that writes the file
        raise BesoNoDataError("exposure state not synchronised with BESO yet")

    def percent(self) -> float:
        if self.phase in (Phase.EXPOSING, Phase.READING):
            _, total_left = self._left(self._clock())
            total = self._total or 0.0
            if total <= 0:
                return 100.0
            return round(max(0.0, min(100.0, 100.0 * (1.0 - total_left / total))), 1)
        if self.phase == Phase.STARTING:
            return 0.0
        return 100.0 if self.image_ready else 0.0

    def is_busy(self) -> bool:
        """True while an exposure is starting, running or waiting to be written (SETs are blocked)."""
        return self.phase in (Phase.STARTING, Phase.EXPOSING, Phase.READING)

    def needs_monitoring(self) -> bool:
        return self.phase in (Phase.EXPOSING, Phase.READING) or (self.phase == Phase.UNKNOWN and self.was_active)

    def next_poll_delay(self) -> float:
        """Poll at the predicted end of the exposure, at most every ``max_poll``, never more often than ``min_poll``."""
        s = self._s
        if self.phase in (Phase.EXPOSING, Phase.READING):
            _, total_left = self._left(self._clock())
            return min(max(total_left, s.min_poll), s.max_poll) if total_left > 0 else s.min_poll
        return s.min_poll

    def status_age(self) -> float:
        return math.inf if self.status_at is None else self._clock() - self.status_at

    def field(self, name: str):
        if name == 'state':
            return int(self.live_state())
        if name == 'percent':
            if self.phase == Phase.UNKNOWN:
                raise BesoNoDataError("exposure state not synchronised with BESO yet")
            return self.percent()
        if name == 'imageready':
            if self.phase == Phase.UNKNOWN:
                raise BesoNoDataError("exposure state not synchronised with BESO yet")
            return self.image_ready
        value = {'lastduration': self.last_duration, 'laststart': self.last_start,
                 'lastfile': self.last_file, 'lastsnr': self.last_snr}[name]
        if value is None:
            raise BesoNoDataError(f"no completed exposure with {name} since the connector started")
        return value
