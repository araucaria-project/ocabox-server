import asyncio
import random
import time
import unittest
from obsrv.comunication.internal_client_api import InternalClientAPI
from obsrv.comunication.request_solver import RequestSolver
from obsrv.data_colection.specialistic_components.tree_alpaca import TreeAlpacaObservatory
from obsrv.data_colection.specialistic_components.tree_cache_observatory import TreeCache
from obsrv.data_colection.specialistic_components.tree_conditional_freezer import TreeConditionalFreezer
from obsrv.planrunner.moves.custom_moves.dome_slew import DomeSlew
from obsrv.planrunner.plan_data import PlanData


class DomeSlewTest(unittest.IsolatedAsyncioTestCase):
    OBSERVATORY_NAME = 'test_observatory'

    def setUp(self):
        super().setUp()
        self.tao = TreeAlpacaObservatory(component_name='sample_component', observatory_name=self.OBSERVATORY_NAME)
        self.tree_cache = TreeCache('sample_name_cache', self.tao)
        self.tree_freezer = TreeConditionalFreezer('sample_freezer', self.tree_cache)
        self.rs = RequestSolver(self.tree_freezer)
        self.api_test = InternalClientAPI(request_solver=self.rs, user_name=f"DefaultTest")

    async def test_go_to_position_az(self):
        """Test slew dome to position az"""
        try:
            await self.tree_freezer.run()
            rm = await self.tao.get_res_manager()
            plan_data = PlanData(access_resource_manager=rm, client_api=self.api_test)
            dome = rm.get_resource(name=rm.DOME, nr=0)
            position_az = random.randint(30, 50)

            fw = DomeSlew(az=position_az, plan_data=plan_data)
            await fw.a_init()
            start_time = time.time()
            await fw.run()
            self.assertTrue(time.time() - start_time < 0.5)  # method run() should make immediately
            await fw.wait()
            self.assertFalse(fw.error)  # finish witch no errors
            result = await self.api_test.get_async(f"{dome.adr}.azimuth")
            self.assertTrue(position_az - 0.5 < result.value.v < position_az + 0.5)
            self.assertEqual(fw.progress, 1)
        finally:
            await self.tree_freezer.stop()

    async def test_go_to_position_wrong_params(self):
        """Test slew mount to position wrong position format"""

        try:
            await self.tree_freezer.run()
            rm = await self.tao.get_res_manager()
            plan_data = PlanData(access_resource_manager=rm, client_api=self.api_test)
            position_alt = random.randint(30, 50)

            fw = DomeSlew(az=None, plan_data=plan_data)
            self.assertTrue(fw._virtual_move)
        finally:
            await self.tree_freezer.stop()


if __name__ == '__main__':
    unittest.main()
