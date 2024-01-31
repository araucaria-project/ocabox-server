import random
import time
import unittest
from pyaraucaria.coordinates import az_alt_2_ra_dec, ra_dec_epoch
from obsrv.comunication.internal_client_api import InternalClientAPI
from obsrv.comunication.request_solver import RequestSolver
from obsrv.data_colection.specialistic_components.tree_alpaca import TreeAlpacaObservatory
from obsrv.data_colection.specialistic_components.tree_cache_observatory import TreeCache
from obsrv.data_colection.specialistic_components.tree_conditional_freezer import TreeConditionalFreezer
from obsrv.planrunner.errors import PlanBuildError
from obsrv.planrunner.moves.custom_moves.mount_slew_ra_dec import MountSlewRaDec
from obsrv.planrunner.plan_data import PlanData


class MountSlewRaDecTest(unittest.IsolatedAsyncioTestCase):
    OBSERVATORY_NAME = 'test_observatory'

    def setUp(self):
        super().setUp()
        self.tao = TreeAlpacaObservatory(component_name='sample_component', observatory_name=self.OBSERVATORY_NAME)
        self.tree_cache = TreeCache('sample_name_cache', self.tao)
        self.tree_freezer = TreeConditionalFreezer('sample_freezer', self.tree_cache)
        self.rs = RequestSolver(self.tree_freezer)
        self.api_test = InternalClientAPI(request_solver=self.rs, user_name=f"DefaultTest")

    async def test_go_to_position_ra_dec(self):
        """Test slew mount to position ra dec"""
        await self.tree_freezer.run()
        rm = await self.tao.get_res_manager()
        plan_data = PlanData(access_resource_manager=rm, client_api=self.api_test)
        mount = rm.get_resource(name=rm.MOUNT, nr=0)
        position_ra, position_dec = az_alt_2_ra_dec(random.randint(30, 50), random.randint(20, 50),
                                                    mount.longitude, mount.latitude, mount.elevation)
        position_ra_final, position_dec_final = ra_dec_epoch(ra=position_ra,
                                                             dec=position_dec,
                                                             epoch=str("2000"))
        fw = MountSlewRaDec(ra_dec=(position_ra, position_dec), plan_data=plan_data)
        await fw.a_init()
        self.assertIsNotNone(fw.positions[0].target)
        start_time = time.time()
        await fw.run()
        self.assertTrue(time.time() - start_time < 0.5)  # method run() should make immediately
        await fw.wait()
        self.assertFalse(fw.error)  # finish witch no errors
        result = await self.api_test.get_async(f"{mount.adr}.rightascension")
        self.assertTrue(position_ra_final - 0.5 < result.value.v < position_ra_final + 0.5)
        result = await self.api_test.get_async(f"{mount.adr}.declination")
        self.assertTrue(position_dec_final - 0.5 < result.value.v < position_dec_final + 0.5)
        self.assertEqual(fw.progress, 1)
        await self.tree_freezer.stop()

    async def test_go_to_position_wrong_params(self):
        """Test slew mount to position wrong position format"""
        await self.tree_freezer.run()
        rm = await self.tao.get_res_manager()
        plan_data = PlanData(access_resource_manager=rm, client_api=self.api_test)
        mount = rm.get_resource(name=rm.MOUNT, nr=0)
        position_ra, position_dec = az_alt_2_ra_dec(random.randint(30, 50), random.randint(20, 50),
                                                    mount.longitude, mount.latitude, mount.elevation)
        with self.assertRaises(PlanBuildError):
            fw = MountSlewRaDec(ra_dec=(None, position_dec), plan_data=plan_data)
        await self.tree_freezer.stop()


if __name__ == '__main__':
    unittest.main()
