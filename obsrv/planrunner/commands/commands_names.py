from obsrv.planrunner.commands.commands_types import CommandsTypes
from obsrv.util_functions.enum_map_base_class import StrVal, BaseArgsMap


class CommandsNames(BaseArgsMap):
    OBJECT = StrVal("OBJECT", CommandsTypes.SCIENCE)
    SEQUENCE = StrVal("SEQUENCE", "UNDEFINED")
    DOMEFLAT = StrVal("DOMEFLAT", CommandsTypes.FLAT)
    FOCUS = StrVal("FOCUS", CommandsTypes.FOCUSING)
    DARK = StrVal("DARK", CommandsTypes.DARK)
    ZERO = StrVal("ZERO", CommandsTypes.ZERO)
    SKYFLAT = StrVal("SKYFLAT", CommandsTypes.FLAT)
    WAIT = StrVal("WAIT", CommandsTypes.WAIT)
    STOP = StrVal("STOP", CommandsTypes.STOP)
    SNAP = StrVal("SNAP", CommandsTypes.SNAP)
