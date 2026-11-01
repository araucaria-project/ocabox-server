"""Internal BESO exceptions and their translation to the server's coded errors.

Everything below the connector raises these classes; ``to_tree_error`` is the single place that maps
them onto ``obcom`` errors according to ``doc/errors.md`` (like ``AlpacaConnector.raise_tree_exeption``).
"""
import asyncio
from typing import Optional

from obcom.data_colection.coded_error import BaseCodedError, TreeOtherError, TreeStructureError
from obcom.data_colection.value import TreeValueError


SEVERITY_TEMPORARY = TreeOtherError.SEVERITY_TEMPORARY
SEVERITY_NORMAL = TreeOtherError.SEVERITY_NORMAL


class BesoError(Exception):
    """Base class of all BESO protocol errors."""


class BesoConfigError(ValueError):
    """The mapping configuration is invalid. Raised while the connector is being built."""


class BesoLinkError(BesoError, ConnectionError):
    """The TCP link is down or must be treated as unusable (it is closed and rebuilt)."""


class BesoTimeoutError(BesoLinkError):
    """BESO did not answer in time. The link is dropped: a late reply would desynchronise it."""


class BesoProtocolError(BesoLinkError):
    """Framing broke (oversized reply, unsolicited bytes, connection closed mid-reply)."""


class BesoLinkDownError(BesoLinkError):
    """No connection and the reconnect back-off has not elapsed yet; the request is not queued."""


class BesoClosedError(BesoLinkError):
    """The connector is shutting down."""


class BesoDeviceError(BesoError):
    """BESO answered with ``ERROR:`` or with a reply that does not confirm the command."""

    def __init__(self, command: str, reply: str, step: Optional[int] = None, steps: Optional[int] = None,
                 unexpected: bool = False):
        self.command = command
        self.reply = reply
        self.step = step
        self.steps = steps
        self.unexpected = unexpected
        where = f" (step {step + 1}/{steps})" if step is not None and steps else ""
        what = "unexpected reply" if unexpected else "error"
        super().__init__(f"BESO {what} for {command!r}{where}: {reply!r}")


class BesoBusyError(BesoError):
    """The device or the command queue is busy; by default retryable (4008 TEMPORARY)."""

    def __init__(self, message: str, severity: str = SEVERITY_TEMPORARY):
        self.severity = severity
        super().__init__(message)


class BesoArgumentError(BesoError):
    """A request argument is missing or invalid."""


class BesoUnsupportedError(BesoError):
    """The parameter or action is not provided by BESO (or not mapped in the configuration)."""


class BesoNoDataError(BesoError):
    """The value does not exist yet (e.g. last exposure duration before any exposure)."""


class BesoConversionError(BesoError):
    """A conversion chain failed. Carries the raw value so it can be logged."""

    def __init__(self, where: str, step: str, raw, value, reason: str):
        self.where = where
        self.step = step
        self.raw = raw
        self.value = value
        self.reason = reason
        super().__init__(f"{where}: conversion step {step} failed for {value!r} (raw {raw!r}): {reason}")


def to_tree_error(exc: BaseException, where: str) -> BaseException:
    """Translate an exception raised below the connector to the server's error model.

    ``asyncio.CancelledError`` and errors that already are coded errors are returned unchanged.
    """
    if isinstance(exc, (asyncio.CancelledError, BaseCodedError)):
        return exc
    if isinstance(exc, BesoUnsupportedError):
        return TreeStructureError(code=3002, message=f"{where}: {exc}", severity=TreeStructureError.SEVERITY_CRITICAL)
    if isinstance(exc, BesoArgumentError):
        return TreeOtherError(address=None, code=4007, message=f"{where}: {exc}",
                              severity=TreeOtherError.SEVERITY_NORMAL)
    if isinstance(exc, BesoBusyError):
        return TreeOtherError(address=None, code=4008, message=f"{where}: {exc}", severity=exc.severity)
    if isinstance(exc, BesoDeviceError):
        # BESO has no numeric error codes, so there is no device_errno to forward.
        return TreeOtherError(address=None, code=4009, message=f"{where}: {exc}",
                              severity=TreeOtherError.SEVERITY_NORMAL)
    if isinstance(exc, (BesoConversionError, BesoNoDataError)):
        return TreeValueError(address=None, code=2002, message=f"{where}: {exc}",
                              severity=TreeValueError.SEVERITY_NORMAL)
    if isinstance(exc, (BesoLinkError, ConnectionError, TimeoutError, OSError)):
        return TreeOtherError(address=None, code=4005, message=f"{where}: BESO not reachable: {exc}",
                              severity=TreeOtherError.SEVERITY_NORMAL)
    if isinstance(exc, BesoConfigError):
        return TreeStructureError(code=3002, message=f"{where}: {exc}", severity=TreeStructureError.SEVERITY_CRITICAL)
    return TreeStructureError(code=3003, message=f"{where}: unexpected error: {exc!r}",
                              severity=TreeStructureError.SEVERITY_NORMAL)
