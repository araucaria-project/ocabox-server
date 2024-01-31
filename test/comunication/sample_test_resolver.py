import asyncio
import time
from typing import List

from obsrv.comunication.base_request_solver import BaseRequestSolver
from obsrv.data_colection.base_components.tree_component import ProvidesResponseProtocol
from obcom.data_colection.value import Value
from obcom.data_colection.value_call import ValueRequest, ValueResponse


class SampleTestResolver(BaseRequestSolver):

    RESPONSE_DELAY = 0

    def __init__(self, data_provider: ProvidesResponseProtocol, **kwargs):
        self.response_delay = self.RESPONSE_DELAY
        super().__init__(data_provider, **kwargs)

    async def get_answer(self, request: List[bytes], user_id: bytes, timeout=None) -> List[bytes]:
        response = []
        for req in request:
            v_request = ValueRequest.from_byte(req)
            response.append(ValueResponse(v_request.address, Value('sample_answer', time.time())).to_byte())
        await asyncio.sleep(self.response_delay)
        return response

    async def get_single_answer(self, request: bytes, user_id: bytes, timeout=None) -> bytes:
        raise NotImplementedError
