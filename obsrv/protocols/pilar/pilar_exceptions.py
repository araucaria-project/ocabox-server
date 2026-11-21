"""Errors of the Pilar connector; ``PilarConnector._serve`` translates them to Ocabox codes (given in brackets)."""


class PilarError(Exception):
    """Base of the Pilar connector errors (translated to Ocabox codes in :meth:`PilarConnector._serve`)."""


class PilarConfigError(PilarError):
    """The protocol or the component configuration is wrong (3001)."""


class PilarUnsupported(PilarError):
    """Pilar does not have the variable (``UNKNOWN``, ``INVALID``, ``DIMENSION``) (3002)."""


class PilarTransient(PilarError):
    """A blip: a freeze, the time just after it, a single missed reply (4005 TEMPORARY)."""


class PilarUnavailable(PilarError):
    """Pilar cannot be reached for longer: refused connection, outage (4005 NORMAL)."""


class PilarBusy(PilarError):
    """The device is busy with another operation, or a variable is locked (4008 TEMPORARY)."""


class PilarDeviceError(PilarError):
    """Pilar answered but said no: ``FAILED``, ``DENIED``, telescope not ready (4009)."""


class PilarReplyError(PilarError):
    """A reply the connector cannot use, e.g. text where a number is needed (2002)."""
