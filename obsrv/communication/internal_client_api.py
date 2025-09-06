import logging
from typing import List, Optional

from obcom.comunication.base_client_api import BaseClientAPI
from obcom.comunication.base_client_request_solver import BaseClientRequestSolver
from obsrv.communication.base_request_solver_protocol import BaseRequestSolverProtocol
from obsrv.communication.internal_client_request_solver import InternalClientRequestSolver
from obcom.data_colection.tree_user import BaseTreeUser, TreeServiceUser
from obcom.data_colection.value_call import ValueRequest, ValueResponse

logger = logging.getLogger(__name__.rsplit('.')[-1])


class InternalClientAPI(BaseClientAPI):

    def __init__(self, request_solver: BaseRequestSolverProtocol, user_name: str = '', **kwargs):
        super().__init__(**kwargs)
        self._request_solver = request_solver
        self._crs = InternalClientRequestSolver(request_solver=self._request_solver)
        self._user = TreeServiceUser(name=user_name)

    @property
    def _CRS(self) -> BaseClientRequestSolver:
        return self._crs

    @property
    def user(self) -> BaseTreeUser:
        return self._user

    async def send_multi(self, requests: List[ValueRequest], no_wait: bool = False) -> Optional[List[ValueResponse]]:
        if no_wait:
            # logger.warning("Internal queries do not support the 'no_wait' parameters")
            no_wait = False
        shortest_timeout = None
        for r in requests:
            if r.request_timeout and (shortest_timeout is None or r.request_timeout < shortest_timeout):
                shortest_timeout = r.request_timeout
            if not r.user:
                r.user = self.user
        resp = await self._CRS.send_request(requests, timeout=shortest_timeout)
        if resp is None:
            return None
        return resp

    async def server_is_alive(self, request_timeout: float = None):
        return True

    async def server_reload_nats_config(self, request_timeout: float = None):
        return self._request_solver.reload_nats_config()

    def get_cfg(self, name_cfg: str, default=None, use_default_settings=True):
        # todo Mikołaj coś trzeba zrobić z tym configiem żeby twoje api było kompatybilne na serverze !!!
        return default

    def get_cfg_deep(self, name_cfg: List[str], default=None, use_default_settings=True):
        # todo Mikołaj coś trzeba zrobić z tym configiem żeby twoje api było kompatybilne na serverze !!!
        return default
