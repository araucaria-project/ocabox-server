import asyncio
import logging
import random
import time

from obsrv.data_colection.resource_manager.errors import ResourceCommandError
from obsrv.planrunner.command import Command

logger = logging.getLogger(__name__.rsplit('.')[-1])


class CommandTest(Command):
    _NAME = "TEST"

    def __init__(self, command_dict: dict, plan_data, previous_command, id_: str = None,
                 loop: asyncio.AbstractEventLoop = None, **kwargs):
        super().__init__(command_dict=command_dict, plan_data=plan_data, id_=id_, previous_command=previous_command,
                         loop=loop, **kwargs)
        self.start_time = 0
        self.end_time = 0

    def validate(self):
        pass

    async def _run_body(self):
        await self._wait_for_permission_to_locking()
        try:
            self.start_time = time.time()
            sleep = random.randint(2, 6)/10
            await asyncio.sleep(sleep)
            self.end_time = time.time()
        except ResourceCommandError:
            pass

    def _submoves(self):
        return iter(())

    async def reset(self):
        await super().reset()
        for m in self._submoves():
            if m is not None:
                await m.reset()

    async def a_init(self):
        await super().a_init()
        for m in self._submoves():
            if m is not None:
                await m.a_init()
