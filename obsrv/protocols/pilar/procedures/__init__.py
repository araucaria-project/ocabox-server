"""Procedures of the Pilar connector: what a method of the mapping in ``pilar_config.yml`` does when it is not a
plain TPL2 variable. One mixin of ``PilarConnector`` per device (as the ``procedures`` of the configuration); the
names of the procedures are in ``_PROCEDURES`` of ``pilar_connector.py``."""
from obsrv.protocols.pilar.procedures.cover import CoverProcedures
from obsrv.protocols.pilar.procedures.focuser import FocuserProcedures
from obsrv.protocols.pilar.procedures.mount import MountProcedures
from obsrv.protocols.pilar.procedures.rotator import RotatorProcedures

__all__ = ['CoverProcedures', 'FocuserProcedures', 'MountProcedures', 'RotatorProcedures']
