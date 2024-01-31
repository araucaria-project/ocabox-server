import logging
from abc import ABC, abstractmethod

from obsrv.comunication.internal_client_api import InternalClientAPI
from obsrv.data_colection.resource_manager.resource_manager import TelescopeComponentManager
from obsrv.planrunner.plan_status_publisher import PlanStatusPublisher
from obsrv.planrunner.plan_task_manager_protocol import PlanTaskManagerProtocol

logger = logging.getLogger(__name__.rsplit('.')[-1])


class BaseCommand(ABC):
    _NAME = "BASECOMMAND"

    def __init__(self, plan_data, **kwargs):
        super().__init__(**kwargs)
        self._plan_data = plan_data

    @classmethod
    def get_name(cls) -> str:
        """Method returning a command name"""
        return cls._NAME

    @property
    def res_mngr(self) -> TelescopeComponentManager:
        return self._plan_data.access_resource_manager

    @property
    def plan_log(self) -> logging.Logger:
        return self._plan_data.plan_log

    @property
    def tsk_mngr(self) -> PlanTaskManagerProtocol:
        return self._plan_data.task_mngr

    @property
    def api(self) -> InternalClientAPI:
        return self._plan_data.client_api

    @property
    def status_pub(self) -> PlanStatusPublisher:
        return self._plan_data.status_pub

    @abstractmethod
    async def a_init(self):
        raise NotImplementedError

    @abstractmethod
    async def run(self):
        raise NotImplementedError

    @abstractmethod
    async def reset(self):
        raise NotImplementedError
