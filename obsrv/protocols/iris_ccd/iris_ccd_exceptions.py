"""Errors of the IRIS CCD connector; ``IrisCcdConnector._serve`` translates them to Ocabox codes."""
from typing import Optional


class IrisTimeout(TimeoutError):
    """iriscom did not answer in time."""


class IrisProtocolError(Exception):
    """A reply of iriscom which cannot be understood."""


class IrisDeviceError(Exception):
    """iriscom answered ``E<code>``, or answered OKAY with something else than the command must produce."""

    def __init__(self, code: Optional[int], message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class IrisBusy(Exception):
    """iriscom is busy with a long operation started by TIC."""

    def __init__(self, operation: str):
        super().__init__(operation)
        self.operation = operation


class IrisConfigError(Exception):
    """The configuration of the protocol or of a component is missing or wrong."""
