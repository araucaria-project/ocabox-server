import asyncio
import logging
from abc import abstractmethod, ABC
from typing import Optional

from obsrv.data_colection.resource_manager.errors import ResourceCommandError

logger = logging.getLogger(__name__.rsplit('.')[-1])


class PartMove(ABC):
    def __init__(self, address: str, timeout=None, order: int = 1, name: str = ""):
        self._target = 1
        self._start = 0
        self._current = 0
        self.task: Optional[asyncio.Task] = None
        self._address = address
        self._finish_fut = None
        self.timeout = timeout if timeout is not None else 20
        self.order = order
        self._start_tmstp = 0
        self._finish_tmstp = 0
        # ------ optional -------
        self._wait_for_others: list = []
        self._name: str = name

    def get_name(self) -> str:
        return self._name

    @property
    def start(self):
        return self._start

    @start.setter
    def start(self, s):
        self._start = s

    @property
    def current(self):
        return self._current

    @current.setter
    def current(self, c):
        self._current = c

    @property
    def target(self):
        return self._target

    @target.setter
    def target(self, t):
        self._target = t

    @property
    def start_timestamp(self):
        return self._start_tmstp

    @start_timestamp.setter
    def start_timestamp(self, s):
        self._start_tmstp = s

    @property
    def finish_timestamp(self):
        return self._finish_tmstp

    @finish_timestamp.setter
    def finish_timestamp(self, s):
        self._finish_tmstp = s

    def get_finish_future(self):
        return self._finish_fut

    def done(self):
        if self._finish_fut is None:
            return False  # false because position even wasn't start so not done
        return self._finish_fut.done()

    def exception(self):
        if self._finish_fut is None:
            return None
        return self._finish_fut.exception()

    async def wait(self):
        if self._finish_fut is None:
            raise ResourceCommandError(f"{type(self).__name__} is waited but never start")
        # the shield protects against the situation of setting the _finish_fut as canceled. If a task that uses the
        # wait() method is canceled, it should not change the _finish_fut state. Future can only be set by
        # PartMove.task, so if we want to cancel position (PartMove), we need to cancel PartMove.task.
        await asyncio.shield(self._finish_fut)

    @abstractmethod
    async def run(self, api):
        raise NotImplementedError

    def add_more_important_position(self, position):
        self._wait_for_others.append(position)

    @property
    @abstractmethod
    def progress(self) -> float:
        """
        Method count progress changing value

        :return: progress in float format from 0 to 1
        """
        raise NotImplementedError

    async def reset(self):
        """
        Method restart object to initial state

        :return: None
        """
        self.current = None
        self.start = None
        self.start_timestamp = 0
        self.finish_timestamp = 0
        # if someone see this error that mean the position was restarted when working
        if self.get_finish_future() is not None and not self.get_finish_future().done():
            self.get_finish_future().set_exception(ResourceCommandError(f"The {type(self).__name__} element "
                                                                        f"was restarted"))
        self._finish_fut = None

    def cancel(self):
        if self.task is not None:
            self.task.cancel()
