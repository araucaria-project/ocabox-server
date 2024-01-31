import asyncio
import logging
from typing import Optional, Dict, Union
from pyaraucaria.obs_plan.obs_plan_parser import ObsPlanParser
from serverish.messenger import Messenger

from obsrv.comunication.internal_client_api import InternalClientAPI
from obsrv.data_colection.resource_manager.resource_manager import TelescopeComponentManager
from obsrv.planrunner.collective_command import CollectiveCommand
from obsrv.planrunner.command import Command
from obsrv.planrunner.errors import PlanBuildError
from obsrv.planrunner.plan_data import PlanData
from obsrv.planrunner.plan_status_publisher import PlanStatusPublisher

logger = logging.getLogger(__name__.rsplit('.')[-1])


class PlanManager:
    def __init__(self, acs_res_mngr: TelescopeComponentManager, api: InternalClientAPI, nats_messenger: Messenger,
                 loop: asyncio.AbstractEventLoop = None,
                 **kwargs):
        super().__init__(**kwargs)
        self._loop = loop
        self._set_loop()
        self._acs_res_mngr: TelescopeComponentManager = acs_res_mngr
        self._nats_messenger: Messenger = nats_messenger
        if not isinstance(self._acs_res_mngr, TelescopeComponentManager):
            raise RuntimeError
        self._plans: Dict[str, Command] = {}
        self._plan_runner_task = asyncio.Task = None
        self._running_plan_id: str = ""
        self._clientAPI: InternalClientAPI = api

    @staticmethod
    def _parse_plan(raw_plan: str):
        try:
            parsed_plan = ObsPlanParser.convert_from_string(raw_plan)
            logger.debug(f'Parsed: {parsed_plan}')
            return parsed_plan
        except asyncio.CancelledError:
            raise
        except Exception as e:  # todo powinny być konkretne błedy przechwytywane. Sprawdzić jakie
            logger.error(f"Unexpected error when parse plan: {e}")
        return None

    async def upload_plan(self, plan_id: str, raw_plan: Union[str, dict], overwrite: bool = False) -> bool:
        parsed_plan = self._parse_plan(raw_plan) if isinstance(raw_plan, str) else raw_plan
        if parsed_plan is None:
            logger.error(f'Plan is wrong. Check the syntax.')
            return False
        if not overwrite and plan_id in self._plans:
            logger.warning(f"Plan with given id already exists.")
            return False
        if self._running_plan_id == plan_id:
            logger.warning(f"Can not update plan witch id: {plan_id} because is actually running")
            return False
        try:
            np = PlanManager.from_dict(parsed_plan, access_resource_manager=self._acs_res_mngr, api=self._clientAPI,
                                       plan_id=plan_id, loop=self._loop)
        except PlanBuildError as e:
            logger.warning(f"Can not create plan witch id: {plan_id} because plan data is corrupted. error "
                           f"message: {e.msg}")
            return False
        if np is None:
            logger.warning(f"Can not create plan witch id: {plan_id} because plan data is corrupted. Plan dict is "
                           f"empty, has wrong format or has unknown commands")
            return False
        self._plans[plan_id] = np
        try:
            await self._a_init_plan(plan_id)
        except PlanBuildError as e:
            logger.warning(f"Can not initiate plan witch id: {plan_id} because plan data is corrupted. error "
                           f"message: {e.msg}")
            return False
        return True

    @classmethod
    def from_dict(cls, dic: dict, access_resource_manager, api, plan_id=None, loop=None, plan_data=None) -> Command:
        """
        Method create observation plan structure from given dictionary.

        :param dic: dictionary representing observation plans
        :param access_resource_manager: resource manager
        :param loop: async loop
        :param api: requesting api
        :param plan_id: plan id, if id None id will be generated
        :param plan_data: custom PlanData object
        :raise PlanBuildError: if dict data is corrupted or can not create plan for other reasons
        :return: plan structure
        """
        plan_data = plan_data if plan_data is not None else PlanData(access_resource_manager=access_resource_manager,
                                                                     client_api=api, plan_id=plan_id)
        try:
            plans = Command.build_command(dic=dic, plan_data=plan_data, id_=None, previous_command=None, loop=loop)
        except PlanBuildError:
            raise
        return plans

    def get_plan_structure(self, id_: str) -> Optional[Command]:
        try:
            return self._plans[id_]
        except LookupError:
            logger.info(f'There is no plan with id: {id_}')
        return None

    def delete_plan(self, id_: str) -> None:
        """
        Delete loaded plan from observation plan.

        :param id_: plan id (default: julian night date)
        """
        if self._running_plan_id == id_:
            logger.warning(f"Can not delete plan witch id: {id_} because is actually running")
            return
        if id_ in self._plans.keys():
            del self._plans[id_]
            logger.info(f"plan witch id: {id_} was successfully removed")

    async def run_plan(self, plan_id: str,
                       step_id: str = None) -> None:  # todo step_id trzeba nazywać że jest 'skip to' - wszystko dział ok tylko zazewnictwo bo się myli
        if not self.is_stopped():
            logger.info(f"Can not run plan with id: {plan_id} because some plan is already running. "
                        f"It will have to be stopped first")
            return

        plan = self.get_plan_structure(plan_id)
        if plan is None:
            logger.info(f"Can not run plan with id: {plan_id}, Can not find it. it was not loaded")
            return

        self._running_plan_id = plan_id
        self._plan_runner_task = self._loop.create_task(self._main(plan=plan, step_id=step_id, plan_id=plan_id))

    def stop_plan(self) -> None:
        if not self.is_stopped() and not self._plan_runner_task.done():
            self._plan_runner_task.cancel()
            self._running_plan_id = ""

    def is_stopped(self):
        """
        Method return false if plan runer is actually running.

        :return: False if running
        """
        if self._plan_runner_task and self._plan_runner_task in asyncio.all_tasks(self._loop):
            return False
        return True

    def _set_loop(self):
        if not self._loop:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.error(f"{self}: Can not get current async loop, something goes wrong")
                raise

    async def _main(self, plan: Command, step_id: str, plan_id: str):
        # if starting from specific command, turn off all previous
        if step_id:
            self._turn_off_command_until_given(plan=plan, step_id=step_id)
        await self._reset_plan(plan=plan)  # reset commands that have already been executed
        await self._send_plan_status(plan_id=plan_id, plan=plan)  # send to NATS plan initial state
        await plan.run()

    async def _a_init_plan(self, plan_id):
        """

        :param plan_id:
        :raise PlanBuildError:
        :return:
        """
        ps = self.get_plan_structure(plan_id)
        if ps is None:
            raise PlanBuildError(msg=f"Something is wrong, the plan witch id {plan_id} not exists")
        try:
            await ps.a_init()
        except PlanBuildError:
            raise

    def _turn_off_command_until_given(self, plan: Command, step_id: str, _done=False) -> bool:
        """
        This method turn off all command from beginning to command witch given id. Method turning off sequence too, if
        all included command turned off

        :param plan: plan structure
        :param step_id: start command id
        :param _done: NOT USE !
        :return: True if done or False if not find command witch given id and turn off all commands
        """
        done = _done
        if isinstance(plan, CollectiveCommand):
            for c in plan:
                done = self._turn_off_command_until_given(c, step_id, done)

        # turn all command until find command witch given id
        if not done and plan.get_id() != step_id:
            plan.turn_off()
            return False
        else:
            return True

    async def _reset_plan(self, plan: Command):
        """
        Method resets commands if is not turned off

        :param plan: plan structure
        :return: None
        """
        if isinstance(plan, CollectiveCommand):
            for c in plan:
                await self._reset_plan(c)
        if plan.is_on():  # reset only commands with is not turned off
            await plan.reset()

    async def _send_plan_status(self, plan_id: str, plan: Command):
        """
        Method send dictionary representing plan status to nats server

        :param plan_id: observation plan id
        :param plan: plan structure
        :return: None
        """
        # collect dictionary
        dic = {plan.get_id(): plan.get_status_dict()}
        # send
        await PlanStatusPublisher.send_program_state(plan_id=plan_id, dic=dic,
                                                     telescope_id=self._acs_res_mngr.get_observatory_name())
