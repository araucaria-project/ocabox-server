import asyncio
import random
import time
import unittest

from obsrv.comunication.internal_client_api import InternalClientAPI
from obsrv.comunication.request_solver import RequestSolver
from obsrv.data_colection.specialistic_components.tree_alpaca import TreeAlpacaObservatory
from obsrv.data_colection.specialistic_components.tree_cache_observatory import TreeCache
from obsrv.data_colection.specialistic_components.tree_conditional_freezer import TreeConditionalFreezer
from obsrv.planrunner.moves.custom_moves.camera_exposure import CameraExposure
from obsrv.planrunner.plan_data import PlanData


class CameraExposureTest(unittest.IsolatedAsyncioTestCase):
    OBSERVATORY_NAME = 'test_observatory'

    def setUp(self):
        super().setUp()
        self.tao = TreeAlpacaObservatory(component_name='sample_component', observatory_name=self.OBSERVATORY_NAME)
        self.tree_cache = TreeCache('sample_name_cache', self.tao)
        self.tree_freezer = TreeConditionalFreezer('sample_freezer', self.tree_cache)
        self.rs = RequestSolver(self.tree_freezer)
        self.api_test = InternalClientAPI(request_solver=self.rs, user_name=f"DefaultTest")

    async def test_exposure(self):
        """Test exposure"""

        await self.tree_freezer.run()
        rm = await self.tao.get_res_manager()
        plan_data = PlanData(access_resource_manager=rm, client_api=self.api_test)
        expose_time = random.randint(5, 20)/10
        fw = CameraExposure(exposure_time=expose_time, light=False, plan_data=plan_data)
        await fw.a_init()
        # main method to make move
        start_time = time.time()
        await fw.run()
        self.assertTrue(time.time() - start_time < 0.5)  # method run() should make immediately
        start_time = time.time()
        await fw.wait()
        self.assertTrue(time.time() - start_time > expose_time)
        self.assertFalse(fw.error)  # finish witch no errors
        result = await self.api_test.get_async(f"{rm.get_resource(rm.CAMERA).adr}.imageready")
        self.assertEqual(result.value.v, True)
        self.assertEqual(fw.progress, 1)
        await self.tree_freezer.stop()


if __name__ == '__main__':
    unittest.main()
