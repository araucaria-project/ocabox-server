import asyncio
import unittest
from serverish.messenger import Messenger
import test.plan_runner.commands.command_Test
from obsrv.comunication.internal_client_api import InternalClientAPI
from obsrv.comunication.request_solver import RequestSolver
from obsrv.data_colection.specialistic_components.tree_alpaca import TreeAlpacaObservatory
from obsrv.data_colection.specialistic_components.tree_cache_observatory import TreeCache
from obsrv.data_colection.specialistic_components.tree_conditional_freezer import TreeConditionalFreezer
from obsrv.planrunner.collective_command import CollectiveCommand
from obsrv.planrunner.plan_data import PlanData
from obsrv.planrunner.plan_manager import PlanManager
from obsrv.planrunner.plan_task_manager import PlanTaskManager


class CommandSequenceTest(unittest.IsolatedAsyncioTestCase):
    OBSERVATORY_NAME = 'test_observatory'

    def setUp(self):
        super().setUp()
        self.tao = TreeAlpacaObservatory(component_name='sample_component', observatory_name=self.OBSERVATORY_NAME)
        self.tree_cache = TreeCache('sample_name_cache', self.tao)
        self.tree_freezer = TreeConditionalFreezer('sample_freezer', self.tree_cache)
        self.rs = RequestSolver(self.tree_freezer)
        self.api_test = InternalClientAPI(request_solver=self.rs, user_name=f"DefaultTest")

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.loop = asyncio.get_running_loop()
        self.rm = await self.tao.get_res_manager()
        self.messenger = Messenger()
        self.pm = PlanManager(acs_res_mngr=self.rm, api=self.api_test, nats_messenger=self.messenger, loop=self.loop)
        await self.messenger.open()
        custom_map_commands = PlanData.DEFAULT_MAP_COMMANDS | {
            "TEST": test.plan_runner.commands.command_Test.CommandTest}
        self.custom_plan_data = PlanData(access_resource_manager=self.rm, client_api=self.api_test,
                                         map_commands=custom_map_commands)

    def tearDown(self) -> None:
        super().tearDown()

    async def asyncTearDown(self):
        await self.messenger.close()
        await super().asyncTearDown()

    async def test_run_command_sequence_no_blocked_child_sequence(self):
        """Test check if Sequence waiting for child sequence before will be reserved task slots """
        var = {'command_name': 'SEQUENCE', 'subcommands': [
            {'command_name': 'TEST', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}},
            {'command_name': 'SEQUENCE', 'subcommands': [
                {'command_name': 'TEST', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}},
                {'command_name': 'TEST', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}},
                {'command_name': 'TEST', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}},
                {'command_name': 'TEST', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}},
                {'command_name': 'TEST', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}},
            ]},
            {'command_name': 'TEST', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}},
            {'command_name': 'TEST', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}},
        ]
               }
        self.custom_plan_data.task_mngr = PlanTaskManager(max_tasks=3)
        plan = self.pm.from_dict(dic=var, access_resource_manager=self.rm, api=self.api_test, plan_id="some",
                                 plan_data=self.custom_plan_data)
        await plan.a_init()
        await plan.run()
        self.assertIsInstance(plan, CollectiveCommand)
        flat_plan = []
        for p in plan:
            if isinstance(p, test.plan_runner.commands.command_Test.CommandTest):
                flat_plan.append(p)
            else:
                for p2 in p:
                    if isinstance(p2, test.plan_runner.commands.command_Test.CommandTest):
                        flat_plan.append(p2)

        previous_start = 0
        previous_end = 0
        for p in flat_plan:
            self.assertTrue(previous_start < p.start_time)
            self.assertTrue(previous_end < p.end_time)
            previous_end = p.end_time


if __name__ == '__main__':
    unittest.main()
