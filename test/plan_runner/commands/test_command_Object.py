import asyncio
import random
import unittest

from pyaraucaria.coordinates import az_alt_2_ra_dec, ra_dec_epoch
from serverish.messenger import Messenger, get_reader
import test.plan_runner.commands.command_Test
from obsrv.comunication.internal_client_api import InternalClientAPI
from obsrv.comunication.nats_streams import NatsStreams
from obsrv.comunication.request_solver import RequestSolver
from obsrv.data_colection.specialistic_components.tree_alpaca import TreeAlpacaObservatory
from obsrv.data_colection.specialistic_components.tree_cache_observatory import TreeCache
from obsrv.data_colection.specialistic_components.tree_conditional_freezer import TreeConditionalFreezer
from obsrv.planrunner.collective_command import CollectiveCommand
from obsrv.planrunner.commands.command_Object import CommandObject
from obsrv.planrunner.errors import PlanBuildError
from obsrv.planrunner.plan_data import PlanData
from obsrv.planrunner.plan_manager import PlanManager
from obsrv.planrunner.plan_status_map import PlanStatusMap
from obsrv.util_functions.asyncio_util_functions import wait_for_psce


class CommandObjectTest(unittest.IsolatedAsyncioTestCase):
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
        await self.rs.run_tree()
        custom_map_commands = PlanData.DEFAULT_MAP_COMMANDS | {
            "TEST": test.plan_runner.commands.command_Test.CommandTest}
        self.custom_plan_data = PlanData(access_resource_manager=self.rm, client_api=self.api_test,
                                         map_commands=custom_map_commands)

    def tearDown(self) -> None:
        super().tearDown()

    async def asyncTearDown(self):
        await self.rs.stop_tree()
        await super().asyncTearDown()
    #
    # def test_build_command_object(self):
    #     """Test build command Object witch validate arguments"""
    #     # ------------------------ positive ------------------------
    #     var = {'command_name': 'OBJECT', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}}
    #     plan = self.pm.from_dict(dic=var, access_resource_manager=self.rm, api=self.api_test, plan_id="some")
    #     self.assertIsInstance(plan, CommandObject)
    #
    #     # -------------------- validate args --------------------
    #     var = {'command_name': 'OBJECT', 'args': ['NGC6871'], 'kwargs': {'clamp': '5'}}
    #     plan = self.pm.from_dict(dic=var, access_resource_manager=self.rm, api=self.api_test, plan_id="some")
    #     self.assertIsInstance(plan, CommandObject)
    #     # ----------------------------------------
    #     var = {'command_name': 'OBJECT', 'args': ['18.157', '-29.351'], 'kwargs': {'clamp': '5'}}
    #     with self.assertRaises(PlanBuildError):
    #         plan = self.pm.from_dict(dic=var, access_resource_manager=self.rm, api=self.api_test, plan_id="some")
    #     # ----------------------------------------
    #     var = {'command_name': 'OBJECT', 'args': ['NGC6871', '18.157'], 'kwargs': {'clamp': '5'}}
    #     with self.assertRaises(PlanBuildError):
    #         plan = self.pm.from_dict(dic=var, access_resource_manager=self.rm, api=self.api_test, plan_id="some")
    #
    #     # -------------------- dither param --------------------
    #     # wrong dither param
    #     var = {'command_name': 'OBJECT', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'dither': '5'}}
    #     with self.assertRaises(PlanBuildError):
    #         plan = self.pm.from_dict(dic=var, access_resource_manager=self.rm, api=self.api_test, plan_id="some")
    #     self.assertFalse(plan.dither_on)
    #     # good dither param
    #     var = {'command_name': 'OBJECT', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {"seq": "2/v/1",
    #                                                                                           'dither': 'base/2/3'}}
    #     plan = self.pm.from_dict(dic=var, access_resource_manager=self.rm, api=self.api_test, plan_id="some")
    #     self.assertIsInstance(plan, CommandObject)
    #     self.assertTrue(plan.dither_on)
    #     # turn off dither
    #     var = {'command_name': 'OBJECT', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'dither': 'none'}}
    #     plan = self.pm.from_dict(dic=var, access_resource_manager=self.rm, api=self.api_test, plan_id="some")
    #     self.assertIsInstance(plan, CommandObject)
    #     self.assertFalse(plan.dither_on)
    #     # ----
    #     var = {'command_name': 'OBJECT', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {"seq": "4/v/1",
    #                                                                                           'dither': 'base/2/3'}}
    #     plan = self.pm.from_dict(dic=var, access_resource_manager=self.rm, api=self.api_test, plan_id="some")
    #     self.assertIsInstance(plan, CommandObject)
    #     self.assertFalse(plan._execute_seq_subcomands[0].exp_list[0][3]._virtual_move)
    #     self.assertTrue(plan._execute_seq_subcomands[0].exp_list[0][3].dither_frequency_nr == 0)
    #     self.assertTrue(plan._execute_seq_subcomands[0].exp_list[0][3].dither_distance == 3)
    #     self.assertTrue(plan._execute_seq_subcomands[0].exp_list[1][3]._virtual_move)

    # def test_run_command_object(self):
    #     """Test run command Object"""
    #     mount = self.rm.get_resource(name=self.rm.MOUNT, nr=0)
    #     dome = self.rm.get_resource(name=self.rm.DOME, nr=0)
    #     az = random.randint(30, 50)
    #     alt = random.randint(20, 50)
    #     position_ra, position_dec = az_alt_2_ra_dec(az, alt,
    #                                                 mount.longitude, mount.latitude, mount.elevation)
    #     position_ra_final, position_dec_final = ra_dec_epoch(ra=position_ra,
    #                                                          dec=position_dec,
    #                                                          epoch=str("2000"))
    #
    #     # todo w tym teście jest zapisywane zdjęcie więc jak będzie nowy mechanizm to dorobić jakieś usuwanie
    #     #  tego zdjęcia
    #
    #     async def coro():
    #         await self.rs.run_tree()
    #         names = (await self.api_test.get_async(f"{self.rm.get_resource(self.rm.FILTERWHEEL).adr}.names")).value.v
    #         targ_filter = names[random.randint(0, len(names) - 1)]
    #
    #         filter_ = targ_filter
    #         number_of_photo = 1
    #         var = {'command_name': 'OBJECT', 'args': ['NGC6871', position_ra, position_dec],
    #                'kwargs': {'clamp': '5', "seq": f"{number_of_photo}/{filter_}/1"}}
    #
    #         plan = self.pm.from_dict(dic=var, access_resource_manager=self.rm, api=self.api_test, plan_id="some")
    #         await plan.a_init()
    #
    #         self.assertIsInstance(plan, CommandObject)
    #         self.assertEqual(plan._execute_seq_subcomands[0].filter._filter, filter_)  # right filter
    #         self.assertFalse(plan._execute_seq_subcomands[0].filter._virtual_move)
    #         focuserFocus, cameraExposure, cameraReadPicture, mountDither = plan._execute_seq_subcomands[0].exp_list[0]
    #         self.assertTrue(focuserFocus._virtual_move)  # skip focusing
    #         self.assertTrue(mountDither._virtual_move)  # skip dithering
    #         self.assertEqual(len(plan._execute_seq_subcomands[0].exp_list), number_of_photo)
    #
    #         await plan.run()
    #
    #         # --------------------------------- check some status is correct ---------------
    #         result = await self.api_test.get_async(f"{mount.adr}.rightascension")
    #         self.assertTrue(position_ra_final - 0.5 < result.value.v < position_ra_final + 0.5)
    #         result = await self.api_test.get_async(f"{mount.adr}.declination")
    #         self.assertTrue(position_dec_final - 0.5 < result.value.v < position_dec_final + 0.5)
    #
    #         result = await self.api_test.get_async(f"{dome.adr}.azimuth")
    #         self.assertTrue(az - 0.5 < result.value.v < az + 0.5)
    #         await self.rs.stop_tree()
    #         await asyncio.sleep(1)
    #
    #     asyncio.run(coro())

    # async def test_cancel_positions_when_error(self):
    #     """the test checks whether all 'positions' are canceled when the command got error."""
    #     mount = self.rm.get_resource(name=self.rm.MOUNT, nr=0)
    #     dome = self.rm.get_resource(name=self.rm.DOME, nr=0)
    #     az = random.randint(30, 50)
    #     alt = random.randint(20, 50)
    #     position_ra, position_dec = az_alt_2_ra_dec(az, alt,
    #                                                 mount.longitude, mount.latitude, mount.elevation)
    #     position_ra_final, position_dec_final = ra_dec_epoch(ra=position_ra,
    #                                                          dec=position_dec,
    #                                                          epoch=str("2000"))
    #
    #     await self.rs.run_tree()
    #     names = (await self.api_test.get_async(f"{self.rm.get_resource(self.rm.FILTERWHEEL).adr}.names")).value.v
    #     targ_filter = names[random.randint(0, len(names) - 1)]
    #
    #     filter_ = targ_filter
    #     number_of_photo = 1
    #     var = {'command_name': 'OBJECT', 'args': ['NGC6871', position_ra, position_dec],
    #            'kwargs': {'clamp': '5', "seq": f"{number_of_photo}/{filter_}/1"}}
    #
    #     plan = self.pm.from_dict(dic=var, access_resource_manager=self.rm, api=self.api_test, plan_id="some")
    #     await plan.a_init()
    #
    #     # change mount and dome Move targets to impossible values
    #     self.assertIsInstance(plan, CommandObject)
    #     plan._move_dome.positions[0].target = 1000  # set target impossible to get
    #     plan._move_dome._position_az_name = "notexsistingname"  # change the name because target
    #     # position is updating after start and is getting by name. We not want to change
    #     plan._move_mount.positions[0].target = 1000  # set target impossible to get
    #
    #     self.assertIsInstance(plan, CommandObject)
    #
    #     task_run = asyncio.create_task(plan.run())  # run command
    #     await asyncio.sleep(0.5)  # wait to start
    #     task_run.cancel()  # cancel task
    #     await asyncio.sleep(0.5)
    #     # -------- check positions is canceled too --------
    #     self.assertTrue(plan._move_mount.positions[0].done())
    #     self.assertTrue(plan._move_mount.positions[0].task.cancelled())
    #     self.assertTrue(plan._move_dome.positions[0].done())
    #     self.assertTrue(plan._move_dome.positions[0].task.cancelled())
    #     await self.rs.stop_tree()
    #     await asyncio.sleep(1)

    # async def test_set_status_in_nats(self):
    #     """Test check if statuses is correctly set in nats server"""
    #     # ------- prepare data ------------
    #     mount = self.rm.get_resource(name=self.rm.MOUNT, nr=0)
    #     dome = self.rm.get_resource(name=self.rm.DOME, nr=0)
    #     az = random.randint(30, 50)
    #     alt = random.randint(20, 50)
    #     position_ra, position_dec = az_alt_2_ra_dec(az, alt,
    #                                                 mount.longitude, mount.latitude, mount.elevation)
    #     position_ra_final, position_dec_final = ra_dec_epoch(ra=position_ra,
    #                                                          dec=position_dec,
    #                                                          epoch=str("2000"))
    #
    #     names = (await self.api_test.get_async(f"{self.rm.get_resource(self.rm.FILTERWHEEL).adr}.names")).value.v
    #     targ_filter = names[random.randint(0, len(names) - 1)]
    #     filter_ = targ_filter
    #     number_of_photo = 1
    #     var = {'command_name': 'SEQUENCE', 'subcommands': [
    #         {'command_name': 'TEST', 'args': ['NGC6871', '18.157', '-29.351'], 'kwargs': {'clamp': '5'}},
    #         {'command_name': 'OBJECT', 'args': ['NGC6871', position_ra, position_dec],
    #          'kwargs': {'clamp': '5', "seq": f"{number_of_photo}/{filter_}/1"}}
    #     ]}
    #
    #     reader = get_reader(NatsStreams.PLAN_MANAGER_STATUS.format(self.custom_plan_data.status_pub._telescope_id),
    #                         deliver_policy="last")
    #
    #     # --------------- test here ----------------
    #     plan = self.pm.from_dict(dic=var, access_resource_manager=self.rm, api=self.api_test, plan_id="some",
    #                              plan_data=self.custom_plan_data)
    #     await plan.a_init()
    #     self.assertIsInstance(plan, CollectiveCommand)
    #     previous_command = plan[0]
    #     command = plan[1]
    #     t = asyncio.create_task(command.run())
    #     await asyncio.sleep(0.2)
    #     message = await reader.read_next()
    #     self.assertEqual(message[0].get("state").get(command.get_id()).get(PlanStatusMap.STATUS),
    #                      PlanStatusMap.STATUS.START)
    #     # unlock command and run it
    #     previous_command._unlock_next_command()
    #     previous_command._set_self_finish()
    #     await asyncio.sleep(0.1)
    #     message = await reader.read_next()
    #     self.assertEqual(message[0].get("state").get(command.get_id()).get(PlanStatusMap.STATUS),
    #                      PlanStatusMap.STATUS.RUN)
    #     await wait_for_psce(t, 15)
    #     message = await reader.read_next()
    #     self.assertEqual(message[0].get("state").get(command.get_id()).get(PlanStatusMap.STATUS),
    #                      PlanStatusMap.STATUS.FINISH)
    #     await asyncio.sleep(1)


if __name__ == '__main__':
    unittest.main()
