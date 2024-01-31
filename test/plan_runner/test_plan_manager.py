import asyncio
import logging
import unittest

from serverish.base import StatusEnum
from serverish.messenger import Messenger, get_reader

from obsrv.comunication.internal_client_api import InternalClientAPI
from obsrv.comunication.nats_streams import NatsStreams
from obsrv.comunication.request_solver import RequestSolver
from obsrv.data_colection.specialistic_components.tree_alpaca import TreeAlpacaObservatory
from obsrv.data_colection.specialistic_components.tree_cache_observatory import TreeCache
from obsrv.data_colection.specialistic_components.tree_conditional_freezer import TreeConditionalFreezer
from obsrv.planrunner.collective_command import CollectiveCommand
from obsrv.planrunner.commands.command_Object import CommandObject
from obsrv.planrunner.commands.command_Sequence import CommandSequence
from obsrv.planrunner.errors import PlanBuildError
from obsrv.planrunner.plan_manager import PlanManager

logger = logging.getLogger(__name__.rsplit('.')[-1])


class TestPlanManager(unittest.IsolatedAsyncioTestCase):
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

    def tearDown(self) -> None:
        super().tearDown()

    async def asyncTearDown(self):
        await self.messenger.close()
        await super().asyncTearDown()

    def test_from_dict(self):
        """Test method from_dict()"""
        var = {'command_name': 'SEQUENCE', 'subcommands': [
            {'command_name': 'OBJECT', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}},
            {'command_name': 'OBJECT', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}}
        ]
               }

        plan = self.pm.from_dict(dic=var, access_resource_manager=self.rm, api=self.api_test, plan_id="some")
        self.assertIsInstance(plan, CommandSequence)
        self.assertEqual(len(plan.get_subcommands()), 2)
        for s in plan.get_subcommands():
            self.assertIsInstance(s, CommandObject)

    def test_from_dict_unknown_command(self):
        """Test method from_dict() witch unknown command type"""
        var = {'command_name': 'SEQUENCE', 'subcommands': [
            {'command_name': 'UNKNOWN', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}},
            {'command_name': 'OBJECT', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}}
        ]
               }
        with self.assertRaises(PlanBuildError):
            plan = self.pm.from_dict(dic=var, access_resource_manager=self.rm, api=self.api_test, plan_id="some")

    async def test_upload_plan_empty_dict(self):
        """Test situation when user try upload empty dct as plan"""

        await self.pm.upload_plan(plan_id="some_id", raw_plan={"uop": {}})
        self.assertEqual(0, len(self.pm._plans))

    def test__turn_off_command_until_given(self):
        """Test method _turn_off_command_until_given()"""
        var = {'command_name': 'SEQUENCE', 'subcommands': [
            {'command_name': 'OBJECT', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}},
            {'command_name': 'OBJECT', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}},
            {'command_name': 'SEQUENCE', 'subcommands': [
                {'command_name': 'OBJECT', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}}
            ]
             },
            {'command_name': 'SEQUENCE', 'subcommands': [
                {'command_name': 'OBJECT', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}},
                {'command_name': 'OBJECT', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}},
                {'command_name': 'OBJECT', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}}
            ]
             },
            {'command_name': 'OBJECT', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}}
        ]
               }
        plan = self.pm.from_dict(dic=var, access_resource_manager=self.rm, api=self.api_test, plan_id="some")
        self.assertIsInstance(plan, CollectiveCommand)
        id_ = plan[3][1].get_id()
        self.pm._turn_off_command_until_given(plan=plan, step_id=id_)

        self.assertTrue(plan.is_on())  # main sequence
        self.assertFalse(plan[0].is_on())  # first element
        self.assertFalse(plan[1].is_on())  # second element
        self.assertFalse(plan[2].is_on())  # first internal sequence
        self.assertFalse(plan[2][0].is_on())
        self.assertTrue(plan[3].is_on())  # second internal sequence
        self.assertFalse(plan[3][0].is_on())
        self.assertTrue(plan[3][1].is_on())  # this element we're looking for
        self.assertTrue(plan[3][2].is_on())
        self.assertTrue(plan[4].is_on())

    def test__reset_plan(self):
        """Test method _reset_plan()"""
        var = {'command_name': 'SEQUENCE', 'subcommands': [
            {'command_name': 'OBJECT', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}},
            {'command_name': 'OBJECT', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}},
            {'command_name': 'SEQUENCE', 'subcommands': [
                {'command_name': 'OBJECT', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}}
            ]
             },
        ]
               }
        plan = self.pm.from_dict(dic=var, access_resource_manager=self.rm, api=self.api_test, plan_id="some")
        self.assertIsInstance(plan, CollectiveCommand)
        asyncio.run(plan.a_init())
        plan[0]._unlock_next_command()
        plan[0]._set_self_finish()
        plan[1]._unlock_next_command()
        plan[1]._set_self_finish()
        plan[2]._unlock_next_command()
        plan[1].turn_off()

        asyncio.run(self.pm._reset_plan(plan=plan))

        self.assertFalse(plan[0]._finish.done())
        self.assertTrue(plan[1]._finish.done())
        self.assertFalse(plan[2]._finish.done())

    async def test__send_plan_status(self):
        """Thest method _send_plan_status()"""
        var = {'command_name': 'SEQUENCE', 'subcommands': [
            {'command_name': 'OBJECT', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}},
            {'command_name': 'SEQUENCE', 'subcommands': [
                {'command_name': 'OBJECT', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}}
            ]
             },
        ]
               }
        self.assertEqual(self.pm._nats_messenger.conn.status.get("nats_server"), StatusEnum.ok)

        await self.pm.upload_plan(plan_id="some_id", raw_plan=var)
        self.assertEqual(1, len(self.pm._plans))
        plan = self.pm.get_plan_structure("some_id")
        await self.pm._send_plan_status(plan_id="some_id", plan=plan)

        reader = get_reader(NatsStreams.PLAN_MANAGER_PLAN.format(self.pm._acs_res_mngr.get_observatory_name()),
                            deliver_policy="last")

        status = await reader.read_next()
        self.assertEqual(status[0].get("id"), "some_id")
        self.assertIsInstance(status[0].get("state"), dict)
        self.assertEqual(len(status[0].get("state")), 1)
        sequence_parmas = status[0].get("state").get(list(status[0].get("state").keys())[0])
        self.assertEqual(sequence_parmas.get("command_name"), "SEQUENCE")
        self.assertEqual(len(sequence_parmas.get("subcommands")), 2)


if __name__ == '__main__':
    unittest.main()
