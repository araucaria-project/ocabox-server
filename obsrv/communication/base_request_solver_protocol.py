from typing import List, Protocol
from obcom.data_colection.value_call import ValueRequest, ValueResponse


class BaseRequestSolverProtocol(Protocol):

    async def get_answer(self, request: List[bytes], user_id: bytes, timeout=None) -> List[bytes]:
        pass

    async def get_single_answer(self, request: bytes, user_id: bytes, timeout=None) -> bytes:
        pass

    async def run_tree(self):
        pass

    async def stop_tree(self):
        pass

    async def get_answer_internal(self, request: List[ValueRequest], timeout=None) -> List[ValueResponse]:
        pass

    async def get_single_answer_internal(self, v_request: ValueRequest, timeout=None) -> ValueResponse:
        pass

    def get_tree_configuration(self) -> dict:
        pass

    def reload_nats_config(self) -> bool:
        pass
