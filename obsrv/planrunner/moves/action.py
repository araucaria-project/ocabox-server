import asyncio
import logging
import time
from typing import Optional, Awaitable
from obsrv.planrunner.moves.part_move import PartMove
from obsrv.util_functions.asyncio_util_functions import wait_for_psce

logger = logging.getLogger(__name__.rsplit('.')[-1])


class Action(PartMove):
    def __init__(self, address, timeout=None, order: int = 1, name: str = "",
                 custom_action: Optional[Awaitable[bool]] = None):
        super().__init__(address=address, timeout=timeout, order=order, name=name)
        # ------ optional -------
        self._custom_action_coro: Optional[Awaitable[bool]] = custom_action

    @property
    def progress(self) -> float:
        if self.done():
            return float(1)
        else:
            return float(0)

    async def _coro(self, api):
        # wait for your turn
        for o in self._wait_for_others:
            await o.get_finish_future()

        if self._custom_action_coro is not None:
            action = self._custom_action_coro
        else:
            action = self._main_action()

        try:
            result = await action
            if result:
                self._finish_fut.set_result(True)
        except NotImplementedError:
            self._finish_fut.set_result(False)
            self.error = True
            self.error_content = "Action method is not defined"
        except (asyncio.CancelledError, asyncio.TimeoutError):
            self._finish_fut.set_result(False)
            self.error = True
            self.error_content = "Waiting for the end of action has been interrupted"
        except Exception as e:
            self.error = True
            self.error_content = str(e)
            self._finish_fut.set_result(False)
        finally:
            self._finish_tmstp = time.time()
            if not self._finish_fut.done():
                self._finish_fut.set_result(False)

    async def run(self, api):
        if self.task:
            self.task.cancel()
        self._finish_fut = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(wait_for_psce(self._coro(api=api), timeout=self.timeout))

    async def _main_action(self):
        raise NotImplementedError
