import asyncio
import logging
from typing import List, Optional

from obcom.comunication.base_client_request_solver import BaseClientRequestSolver
from obsrv.communication.base_request_solver_protocol import BaseRequestSolverProtocol
from obcom.data_colection.value_call import ValueRequest, ValueResponse

logger = logging.getLogger(__name__.rsplit('.')[-1])


class InternalClientRequestSolver(BaseClientRequestSolver):

    def __init__(self, request_solver: BaseRequestSolverProtocol, **kwargs):
        super().__init__(**kwargs)
        self._request_solver: BaseRequestSolverProtocol = request_solver

    @property
    def request_solver(self):
        return self._request_solver

    async def send_request(self, requests: List[ValueRequest], timeout: float = None,
                           no_wait: bool = False) -> Optional[List[ValueResponse]]:
        try:
            resp = await self._request_solver.get_answer_internal(requests, timeout=timeout)
        except asyncio.CancelledError:
            raise
        if resp is None:
            return None
        return resp
