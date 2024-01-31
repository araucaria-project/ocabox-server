import asyncio
import random
import time
import unittest

from obsrv.comunication.internal_client_api import InternalClientAPI
from obsrv.comunication.request_solver import RequestSolver
from obsrv.data_colection.specialistic_components.tree_alpaca import TreeAlpacaObservatory
from obsrv.data_colection.specialistic_components.tree_cache_observatory import TreeCache
from obsrv.data_colection.specialistic_components.tree_conditional_freezer import TreeConditionalFreezer
from obsrv.planrunner.moves.custom_moves.focuser_focus import FocuserFocus
from obsrv.planrunner.plan_data import PlanData


class FocuserFocusTest(unittest.IsolatedAsyncioTestCase):
    OBSERVATORY_NAME = 'test_observatory'

    def setUp(self):
        super().setUp()
        self.tao = TreeAlpacaObservatory(component_name='sample_component', observatory_name=self.OBSERVATORY_NAME)
        self.tree_cache = TreeCache('sample_name_cache', self.tao)
        self.tree_freezer = TreeConditionalFreezer('sample_freezer', self.tree_cache)
        self.rs = RequestSolver(self.tree_freezer)
        self.api_test = InternalClientAPI(request_solver=self.rs, user_name=f"DefaultTest")

    async def test_set_focus(self):
        """Test set focus value"""
        await self.tree_freezer.run()
        rm = await self.tao.get_res_manager()
        plan_data = PlanData(access_resource_manager=rm, client_api=self.api_test)
        target = random.randint(18000, 20000) + 0.2  # check is correctly cast to int
        fw = FocuserFocus(focus=target, plan_data=plan_data)
        await fw.a_init()
        # main method to make move
        start_time = time.time()
        await fw.run()
        self.assertTrue(time.time() - start_time < 0.5)  # method run() should make immediately
        await fw.wait()
        self.assertFalse(fw.error)  # finish witch no errors
        result = await self.api_test.get_async(f"{rm.get_resource(rm.FOCUSER).adr}.position")
        self.assertEqual(result.value.v, int(target))
        self.assertEqual(fw.progress, 1)
        await self.tree_freezer.stop()


if __name__ == '__main__':
    unittest.main()
