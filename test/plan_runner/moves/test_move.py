import random
import unittest
from obsrv.comunication.internal_client_api import InternalClientAPI
from obsrv.comunication.request_solver import RequestSolver
from obsrv.planrunner.moves.move import Move
from obsrv.planrunner.moves.position import Position
from obsrv.data_colection.specialistic_components.tree_alpaca import TreeAlpacaObservatory
from obsrv.data_colection.specialistic_components.tree_cache_observatory import TreeCache
from obsrv.data_colection.specialistic_components.tree_conditional_freezer import TreeConditionalFreezer
from obsrv.planrunner.plan_data import PlanData


class MoveTest(unittest.IsolatedAsyncioTestCase):
    OBSERVATORY_NAME = 'test_observatory'

    def setUp(self):
        super().setUp()
        self.tao = TreeAlpacaObservatory(component_name='sample_component', observatory_name=self.OBSERVATORY_NAME)
        self.tree_cache = TreeCache('sample_name_cache', self.tao)
        self.tree_freezer = TreeConditionalFreezer('sample_freezer', self.tree_cache)
        self.rs = RequestSolver(self.tree_freezer)
        self.api_test = InternalClientAPI(request_solver=self.rs, user_name=f"DefaultTest")

    async def test_progress(self):
        """Test method `progress` from Move object"""
        plan_data = PlanData(access_resource_manager=await self.tao.get_res_manager(), client_api=self.api_test)
        await self.tree_freezer.run()
        position_ra = random.randint(170, 190)
        rm = await self.tao.get_res_manager()
        dome = rm.get_resource(name=rm.DOME, nr=0)

        mov = Move(plan_data=plan_data)
        pos = Position("abc", t=8.0, c="-1", s=int(-6))
        mov.positions.append(pos)
        pos2 = Position("abc", t=7.0, c="6", s=int(-5))
        mov.positions.append(pos2)

        self.assertEqual(pos.progress, 5 / 14)
        self.assertEqual(pos2.progress, 11 / 12)

        self.assertEqual(mov.progress, (5 / 14 + 11 / 12) / 2)

        await self.tree_freezer.stop()


if __name__ == '__main__':
    unittest.main()
