import asyncio
import logging
from typing import List
from obsrv.planrunner.collective_command import CollectiveCommand
from obsrv.planrunner.command import Command
from obsrv.planrunner.command_error import CriticalCommandError, NormalCommandError
from obsrv.planrunner.errors import PlanBuildError
from obsrv.planrunner.commands.commands_names import CommandsNames
from obsrv.planrunner.plan_status_map import PlanStatusMap


class CommandSequence(CollectiveCommand):
    _NAME = CommandsNames.SEQUENCE
    # --- arg keys ---
    _SUBCOMMANDS_KEY = "subcommands"

    def __init__(self, command_dict: dict, plan_data, previous_command, id_: str = None,
                 loop: asyncio.AbstractEventLoop = None, **kwargs):
        super().__init__(command_dict=command_dict, plan_data=plan_data, id_=id_, previous_command=previous_command,
                         loop=loop, **kwargs)
        self._create_subcommands_objects(self._command_dict.get(self._SUBCOMMANDS_KEY, []))

    def validate(self):
        # nothing args to validate, because Sequence command not take args and kwargs
        pass

    def _create_subcommands_objects(self, subcommands: list):
        """

        :param subcommands:
        :raise PlanBuildError
        :return:
        """
        if not subcommands:
            self.log(f"Command {self._NAME} has empty list of Subcommands", lvl=logging.INFO)
            return
        if not isinstance(subcommands, list):
            self.log(f"Command {self._NAME} get subcommands type {type(subcommands)} instead {list}. "
                     f"Subcommand will be not created", lvl=logging.ERROR)
            return
        com = None
        for s in subcommands:
            try:
                com = Command.build_command(dic=s, plan_data=self._plan_data, previous_command=com, loop=self._loop)
            except PlanBuildError:
                raise
            self._subcommands.append(com)

    def get_subcommands(self) -> List[Command]:
        return self._subcommands

    async def _run_body(self):
        stop_future = asyncio.get_running_loop().create_future()

        def callback_subtask_check_status(context):
            try:
                context.result()
            except asyncio.CancelledError:
                if not stop_future.done():
                    stop_future.set_result(True)
            except CriticalCommandError:
                if not stop_future.done():
                    stop_future.set_result(True)
            except NormalCommandError:
                pass
            except Exception:
                if not stop_future.done():
                    stop_future.set_result(True)

        await self._wait_for_previous_command_finish()
        await self.status_pub.update_status(cmd_id=self.get_id(), data={PlanStatusMap.STATUS: PlanStatusMap.STATUS.RUN})
        self.log("Start doing")
        tasks = []
        for s in self.get_subcommands():
            self.log(f"Waiting for free task slot")
            try:
                t = await self.tsk_mngr.run_task_when_can(s.run(), is_independent=True)
                t.add_done_callback(callback_subtask_check_status)
                # !!! situation when a sequence will trigger other sequences. You should wait until the second sequence
                # finishes running so as not to compete with it for free task slots
                if isinstance(s, CollectiveCommand):
                    await s.wait_to_finish()
                # check if one of task finish with error and set stop_future as done
                if stop_future.done():
                    await self._cancel_tasks(tasks)
                    break
            except asyncio.CancelledError:
                await self._cancel_tasks(tasks)
                break
            self.log(f"Created task for {s.get_name()} Id: {s.get_id()}")
            tasks.append(t)

        # this block is needed for wait for last tasks and update statuses
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                await self._cancel_tasks(tasks)
                break
            except CriticalCommandError:
                await self.status_pub.update_status(cmd_id=self.get_id(),
                                                    data={PlanStatusMap.STATUS: PlanStatusMap.STATUS.ERROR})
                await self._cancel_tasks(tasks)
                break
            except NormalCommandError:
                pass
            except Exception as e:
                await self.status_pub.update_status(cmd_id=self.get_id(),
                                                    data={PlanStatusMap.STATUS: PlanStatusMap.STATUS.ERROR})
                await self._cancel_tasks(tasks)
                break
        self.log("All subcommand finished")

    @staticmethod
    async def _cancel_tasks(tasks):
        for t in tasks:
            t.cancel()

    async def a_init(self):
        await super().a_init()
        for s in self._subcommands:
            await s.a_init()

    def _submoves(self):
        return iter(())  # empty iterator
