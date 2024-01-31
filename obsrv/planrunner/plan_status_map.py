from obsrv.util_functions.enum_map_base_class import BaseArgsMap, StrVal


class _CommandStatus(str, BaseArgsMap):
    START = "start"
    RUN = "run"
    FINISH = "finish"
    ERROR = "error"


class PlanStatusMap(BaseArgsMap):
    STATUS: _CommandStatus = _CommandStatus("status")
