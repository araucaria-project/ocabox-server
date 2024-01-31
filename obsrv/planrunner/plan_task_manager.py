import asyncio
import dataclasses
import logging
from typing import Optional, Tuple, List

logger = logging.getLogger(__name__.rsplit('.')[-1])


@dataclasses.dataclass
class PlanTaskManager:
    tasks: List[Tuple[asyncio.Task, bool]] = None
    max_tasks: int = 5  # todo wziąść potem to z configa
    _future_full_list: Optional[asyncio.Future] = None

    def __post_init__(self):
        if self.tasks is None:
            self.tasks = []

    def _pop_task(self, t):
        t_ob = None
        for x in self.tasks:
            if x[0] == t:
                t_ob = x
        if t_ob:
            fut = self._future_full_list
            if fut is not None:
                self._future_full_list = None
                if not fut.done():
                    fut.set_result(True)
            self.tasks.remove(t_ob)
        else:
            #  it should never show up
            logger.error(f"an attempt to delete a task is not allowed. The specified task is not in the list.")

    def _has_space(self):
        return len(self.tasks) < self.max_tasks

    async def run_task_when_can(self, coro, is_independent: bool) -> asyncio.Task:
        """


        :param coro: task method
        :param is_independent: DOESN'T WORK YET. the x parameter determines whether a given task is independent of
        another and can end on its own. This parameter is intended to prevent situations where there is a command tree,
        e.g. SEQUENCE, which will exhaust the pool of free slots for tasks.
        :return: task
        """
        if not self._has_space():
            fut = self._future_full_list
            if fut is not None:
                await fut
            else:
                logger.error(f"For unexplained reasons, there is no Future object")
        t = asyncio.get_running_loop().create_task(coro)

        def callback(context):
            self._pop_task(t)

        t.add_done_callback(callback)
        self.tasks.append((t, is_independent))
        if not self._has_space():
            if self._future_full_list is not None:
                logger.error(f"For unexplained reasons, the future is not None ")
            self._future_full_list = asyncio.get_running_loop().create_future()
        return t
