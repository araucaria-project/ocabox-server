import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Optional

from obsrv.planrunner.base_command import BaseCommand
from obsrv.planrunner.command_args_reader import CommandArgsReader
from obsrv.planrunner.command_error import CriticalCommandError
from obsrv.planrunner.errors import PlanBuildError
from obsrv.planrunner.plan_status_map import PlanStatusMap

logger = logging.getLogger(__name__.rsplit('.')[-1])


class Command(BaseCommand, CommandArgsReader, ABC):
    _NAME = "DEFAULT"
    # --- arg keys ---
    _NAME_KEY = "command_name"
    _ARGS_KEY = "args"
    _KWARGS_KEY = "kwargs"

    def __init__(self, command_dict: dict, plan_data, previous_command, id_: str = None,
                 loop: asyncio.AbstractEventLoop = None, **kwargs):
        self._command_dict: dict = command_dict
        self.previous_command = previous_command
        self._loop = loop
        if id_ is None:
            id_ = plan_data.generate_next_id()
        self._id: str = id_
        super().__init__(plan_data=plan_data, arg=self.get_args(), kwarg=self.get_kwargs(), **kwargs)

        # --- --- --- --- --- --- --- --- --- --- ---
        self._needed_modules = []
        self._finish_locking: Optional[asyncio.Future] = None
        self._finish: Optional[asyncio.Future] = None
        self._is_on = True  # flag mean command is turn on. If command is off will be not do

    def log(self, msg, lvl: Optional[int] = None):
        lvl = logging.DEBUG if lvl is None else lvl
        self.plan_log.log(msg=f"[{self._id}][{self.get_name()}] {msg}", level=lvl)

    def get_id(self) -> str:
        return self._id

    def get_kwargs(self) -> dict:
        """Method returns loaded kwargs"""
        return self._command_dict.get(self._KWARGS_KEY, {})

    def get_args(self) -> list:
        """Method returns loaded args"""
        return self._command_dict.get(self._ARGS_KEY, [])

    async def a_init(self):
        """
        Async version innit method, Should run always after normal init.

        :raise PlanBuildError:
        :return:
        """
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                raise PlanBuildError("Can not find running async loop")
        self._finish_locking = self._loop.create_future()
        self._finish = self._loop.create_future()

    async def wait_to_lock_all_resources(self):
        fut = self._finish_locking
        if fut is not None:
            await fut
            return
        raise CriticalCommandError

    async def wait_to_finish(self):
        fut = self._finish
        if fut is not None:
            await fut
            return
        raise CriticalCommandError

    async def _wait_for_permission_to_locking(self):
        # No pre-command has been wired, so you can start right now
        if self.previous_command is None:
            return
        # wait for pre-command end
        await self.previous_command.wait_to_lock_all_resources()

    async def _wait_for_previous_command_finish(self):
        if self.previous_command is None:
            return
        await self.previous_command.wait_to_finish()

    def _unlock_next_command(self):
        if self._finish_locking is None:
            return
        if self._finish_locking.done():
            return
        self._finish_locking.set_result(True)  # all locks were used

    def _set_self_finish(self):
        if self._finish is not None:
            self._finish.set_result(True)  # all locks were used

    @staticmethod
    def build_command(dic, plan_data, id_: str = None, previous_command=None,
                      loop: asyncio.AbstractEventLoop = None, **kwargs):
        """
        Method create command object from give dictionary

        :param dic: dict witch command settings needed to build
        :param plan_data: object PlanData
        :param id_: command id
        :param previous_command: previous command
        :param loop: asyncio loop
        :param kwargs:
        :raise PlanBuildError:
        :return: command object
        """
        command_map = plan_data.map_commands
        command_type = dic.get(Command._NAME_KEY, None)
        if command_type and command_type in command_map:
            command_class = command_map.get(command_type)(command_dict=dic, plan_data=plan_data, id_=id_,
                                                          previous_command=previous_command,
                                                          loop=loop, **kwargs)
        else:
            raise PlanBuildError(f"Unrecognized command type")
        return command_class

    async def run(self):
        try:
            if not self.is_on():  # do not run if command is turn off
                return
            await self.status_pub.update_status(cmd_id=self.get_id(),
                                                data={PlanStatusMap.STATUS: PlanStatusMap.STATUS.START})
            await self._run_body()
        finally:
            self._unlock_next_command()  # unlock in the end
            self._set_self_finish()  # command finished
            # check if status is not error and set status finish (if status is error leave it)
            if self.status_pub.get_status_local(cmd_id=self.get_id()).get(PlanStatusMap.STATUS,
                                                                          None) is not PlanStatusMap.STATUS.ERROR:
                await self.status_pub.update_status(cmd_id=self.get_id(),
                                                    data={PlanStatusMap.STATUS: PlanStatusMap.STATUS.FINISH})

            # cancel Moves
            for s in self._submoves():
                s.cancel()

    @abstractmethod
    async def _run_body(self):
        raise NotImplementedError

    def turn_off(self):
        self._is_on = False

    def turn_on(self):
        self._is_on = True

    def is_on(self):
        return self._is_on

    async def reset(self):
        self._finish_locking = self._loop.create_future()
        self._finish = self._loop.create_future()
        # reset locally and not send because is not necessary to send for all commands just need one time after all
        self.status_pub.reset_status_local(cmd_id=self.get_id())

    @abstractmethod
    def _submoves(self):
        """
        Method return generator witch all `Move` objects.

        :return:
        """
        return iter(())

    def get_status_dict(self) -> dict:
        """
        Method collect and return command initial status data and return as dict

        :return: dictionary witch command initial status data
        """
        return {self._NAME_KEY: self._NAME, "is_on": self.is_on()}
