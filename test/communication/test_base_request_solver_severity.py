import time
import unittest
from typing import List

from obcom.data_colection.response_error import ResponseError
from obcom.data_colection.value_call import ValueRequest
from obsrv.communication.base_request_solver import BaseRequestSolver
from obsrv.tree_components.base_components.tree_base_provider import TreeBaseProvider


class SampleRequestSolver(BaseRequestSolver):
    async def get_answer(self, request: List[bytes], user_id: bytes, timeout=None) -> List[bytes]:
        pass

    async def get_single_answer(self, request: bytes, user_id: bytes, timeout=None) -> bytes:
        pass


class TestBaseRequestSolverSeverity(unittest.IsolatedAsyncioTestCase):

    async def test_router_timeout_4002_is_normal(self):
        """solver with data_provider=None -> 4002 NORMAL; a provider whose get_response raises -> 4002 NORMAL."""
        req = ValueRequest('sample_telescope.any_val', time.time(), 10.0)

        # Case 1: data_provider is None
        solver_no_provider = SampleRequestSolver(data_provider=None)
        resp = await solver_no_provider._get_single_answer(req)
        self.assertFalse(resp.status)
        self.assertEqual(resp.error.code, 4002)
        self.assertEqual(resp.error.severity, ResponseError.SEVERITY_NORMAL)

        # Case 2: data_provider.get_response raises an exception
        class _RaisingProvider(TreeBaseProvider):
            async def get_response(self, request):
                raise RuntimeError("provider unexpected error")

        solver_raising_provider = SampleRequestSolver(data_provider=_RaisingProvider("raising_provider"))
        resp_raising = await solver_raising_provider._get_single_answer(req)
        self.assertFalse(resp_raising.status)
        self.assertEqual(resp_raising.error.code, 4002)
        self.assertEqual(resp_raising.error.severity, ResponseError.SEVERITY_NORMAL)


if __name__ == '__main__':
    unittest.main()
