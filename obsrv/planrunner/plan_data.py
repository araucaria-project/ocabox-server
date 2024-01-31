import dataclasses
import logging
import random
import string
import typing
import obsrv.planrunner.commands.command_Sequence
import obsrv.planrunner.commands.command_Object
from obsrv.comunication.internal_client_api import InternalClientAPI
from obsrv.data_colection.resource_manager.resource_manager import TelescopeComponentManager
from obsrv.planrunner.commands.commands_names import CommandsNames
from obsrv.planrunner.plan_task_manager import PlanTaskManager
from obsrv.planrunner.plan_task_manager_protocol import PlanTaskManagerProtocol
from obsrv.planrunner.plan_status_publisher import PlanStatusPublisher

logger = logging.getLogger(__name__.rsplit('.')[-1])


@dataclasses.dataclass
class PlanData:

    access_resource_manager: TelescopeComponentManager
    client_api: InternalClientAPI
    DEFAULT_MAP_COMMANDS: typing.ClassVar[dict] = {
        CommandsNames.SEQUENCE: obsrv.planrunner.commands.command_Sequence.CommandSequence,
        CommandsNames.OBJECT: obsrv.planrunner.commands.command_Object.CommandObject,
    }
    plan_id: str = None
    task_mngr: PlanTaskManagerProtocol = None
    status_pub: PlanStatusPublisher = None
    plan_log: logging.Logger = logging.getLogger(name="ObsPlanLog")
    map_commands: dict = None
    _last_id: int = 0

    def __post_init__(self):
        if self.plan_id is None:
            # generate random string
            self.plan_id = ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(16))
        if self.task_mngr is None:
            self.task_mngr = PlanTaskManager()
        if self.map_commands is None:
            self.map_commands = self.DEFAULT_MAP_COMMANDS
        if self.status_pub is None:
            self.status_pub = PlanStatusPublisher(plan_id=self.plan_id,
                                                  telescope_id=self.access_resource_manager.get_observatory_name())

    def generate_next_id(self) -> str:
        self._last_id += 10
        return str(self._last_id)
