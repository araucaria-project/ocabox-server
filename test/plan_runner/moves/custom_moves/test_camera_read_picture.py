import asyncio
import datetime
import random
import time
import unittest

from obsrv.comunication.internal_client_api import InternalClientAPI
from obsrv.comunication.request_solver import RequestSolver
from obsrv.data_colection.specialistic_components.tree_alpaca import TreeAlpacaObservatory
from obsrv.data_colection.specialistic_components.tree_cache_observatory import TreeCache
from obsrv.data_colection.specialistic_components.tree_conditional_freezer import TreeConditionalFreezer
from obsrv.planrunner.errors import PlanBuildError
from obsrv.planrunner.moves.custom_moves.camera_exposure import CameraExposure
from obsrv.planrunner.moves.custom_moves.camera_read_picture import CameraReadPicture
from obsrv.planrunner.plan_data import PlanData


class CameraReadPictureTest(unittest.IsolatedAsyncioTestCase):
    OBSERVATORY_NAME = 'test_observatory'

    def setUp(self):
        super().setUp()
        self.tao = TreeAlpacaObservatory(component_name='sample_component', observatory_name=self.OBSERVATORY_NAME)
        self.tree_cache = TreeCache('sample_name_cache', self.tao)
        self.tree_freezer = TreeConditionalFreezer('sample_freezer', self.tree_cache)
        self.rs = RequestSolver(self.tree_freezer)
        self.api_test = InternalClientAPI(request_solver=self.rs, user_name=f"DefaultTest")

    async def test_read_picture(self):
        """Test read and save picture"""
        await self.tree_freezer.run()
        try:
            rm = await self.tao.get_res_manager()
            plan_data = PlanData(access_resource_manager=rm, client_api=self.api_test)

            # create photo
            expose_time = random.randint(5, 20) / 10
            fw = CameraExposure(exposure_time=expose_time, light=False, plan_data=plan_data)
            await fw.a_init()
            await fw.run()
            await fw.wait()
            # -------------
            fw = CameraReadPicture(exp_time_start=datetime.datetime.now(), exposure_time=expose_time,
                                   plan_data=plan_data, req_ra=None,
                                   req_dec=23, epoch=2001, type_=None, obj_name="some_object_name",
                                   filter_="sample_filter", observer="Andy")
            await fw.a_init()
            # main method to make move
            start_time = time.time()
            await fw.run()
            self.assertTrue(time.time() - start_time < 0.5)  # method run() should make immediately
            await fw.wait()
            self.assertFalse(fw.error)  # finish witch no errors
            result = await self.api_test.get_async(f"{rm.get_resource(rm.CAMERA).adr}.imageready")
            self.assertEqual(result.value.v, True)
            self.assertEqual(fw.progress, 1)
        finally:
            await self.tree_freezer.stop()

    async def test_validate_params(self):
        """Test read and save picture"""

        await self.tree_freezer.run()
        try:
            rm = await self.tao.get_res_manager()
            plan_data = PlanData(access_resource_manager=rm, client_api=self.api_test)
            expose_time = random.randint(5, 20) / 10
            # -------- NO exp_time_start --------
            with self.assertRaises(PlanBuildError):
                fw = CameraReadPicture(exp_time_start=2234, exposure_time=expose_time,
                                       plan_data=plan_data, req_ra=None,
                                       req_dec=23, epoch=2001, type_=None, obj_name="some_object_name",
                                       filter_="sample_filter", observer="Andy")
            # -------- NCan not find mount witch mount_resource_nr --------
            with self.assertRaises(PlanBuildError):
                fw = CameraReadPicture(exp_time_start=datetime.datetime.now(), exposure_time=expose_time,
                                       plan_data=plan_data, req_ra=None, mount_resource_nr=7,
                                       req_dec=23, epoch=2001, type_=None, obj_name="some_object_name",
                                       filter_="sample_filter", observer="Andy")
        finally:
            await self.tree_freezer.stop()


if __name__ == '__main__':
    unittest.main()
