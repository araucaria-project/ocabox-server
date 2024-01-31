import unittest
from obsrv.comunication.internal_client_api import InternalClientAPI
from obsrv.comunication.request_solver import RequestSolver
from obsrv.planrunner.moves.action import Action
from obsrv.data_colection.specialistic_components.tree_alpaca import TreeAlpacaObservatory
from obsrv.data_colection.specialistic_components.tree_cache_observatory import TreeCache
from obsrv.data_colection.specialistic_components.tree_conditional_freezer import TreeConditionalFreezer


class ActionTest(unittest.IsolatedAsyncioTestCase):
    OBSERVATORY_NAME = 'test_observatory'

    def setUp(self):
        super().setUp()
        self.tao = TreeAlpacaObservatory(component_name='sample_component', observatory_name=self.OBSERVATORY_NAME)
        self.tree_cache = TreeCache('sample_name_cache', self.tao)
        self.tree_freezer = TreeConditionalFreezer('sample_freezer', self.tree_cache)
        self.rs = RequestSolver(self.tree_freezer)
        self.api_test = InternalClientAPI(request_solver=self.rs, user_name=f"DefaultTest")

    async def test_make_action(self):
        """Test normal work of given custom action method"""
        class T:
            def __init__(self):
                self.a = 0

        t = T()

        async def custom_action():
            t.a = 1

        await self.tree_freezer.run()
        try:
            ac = Action("address", custom_action=custom_action())
            await ac.run(api=self.api_test)
            await ac.wait()

            self.assertEqual(t.a, 1)
        finally:
            await self.tree_freezer.stop()

    async def test_make_action_inside_class(self):
        """Test normal work of given custom action method from class"""

        class T:
            def __init__(self):
                self.a = 0

        class C:
            def __init__(self):
                self.b = 0

            async def custom_action(self) -> bool:
                t.a = 1
                self.b = 2
                return True

        t = T()

        await self.tree_freezer.run()
        try:
            c = C()
            ac = Action("address", custom_action=c.custom_action())
            await ac.run(api=self.api_test)
            await ac.wait()

            self.assertEqual(t.a, 1)
            self.assertEqual(c.b, 2)
        finally:
            await self.tree_freezer.stop()


if __name__ == '__main__':
    unittest.main()
