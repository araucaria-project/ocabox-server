"""Handlers of the IRIS CCD connector: what a GET or PUT of a component does. One mixin of ``IrisCcdConnector`` per
device kind; the tables of handlers (variable -> method) are in ``IrisCcdConnector.__init__``."""
from obsrv.protocols.iris_ccd.handlers.camera import CameraHandlers
from obsrv.protocols.iris_ccd.handlers.filterwheel import FilterWheelHandlers

__all__ = ['CameraHandlers', 'FilterWheelHandlers']
