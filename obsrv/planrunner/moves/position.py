import asyncio
import logging
import time
from typing import Tuple, Optional, Callable
from obcom.comunication.comunication_error import CommunicationRuntimeError
from obsrv.data_colection.resource_manager.errors import ResourceCommandError
from obsrv.planrunner.moves.part_move import PartMove
from obsrv.util_functions.asyncio_util_functions import wait_for_psce

logger = logging.getLogger(__name__.rsplit('.')[-1])


# todo Position do zmiany nazwy
class Position(PartMove):
    def __init__(self, address, t=None, s=None, c=None, timeout=None,
                 bounds: Tuple[Optional[float], Optional[float]] = (None, None), order: int = 1, name: str = ""):
        super().__init__(address=address, timeout=timeout, order=order, name=name)
        self.custom_end_condition: Optional[Callable] = None
        self.start = s
        self.current = c
        self.target = t
        # ------ optional -------
        self.bounds: Tuple[Optional[float], Optional[float]] = bounds

    def _check_finish(self):
        """
            Method check current value is achieved target.

            :raise ValueError
            :return: bool
            """
        if self.custom_end_condition is not None and callable(self.custom_end_condition):
            return self.custom_end_condition(self.start, self.current, self.target)
        elif self.bounds[0] is not None and self.bounds[1] is not None:
            return self._check_finish_bounded()
        if self.current == self.target:
            return True
        else:
            return False

    def _check_finish_bounded(self):
        """
            Method check current value is achieved target witch bounds.

            :raise ValueError
            :return: bool
            """
        try:
            t = float(self.target)
            c = float(self.current)
        except ValueError:
            raise
        bound_btm = self.bounds[0]
        bound_top = self.bounds[1]
        if t - bound_btm < c < t + bound_top:
            return True
        else:
            return False

    @property
    def progress(self) -> float:
        # todo zrobić z tego Param - urzyć tej biblioteki Param i ze wszystkich innych wartości też
        if self.done():
            return float(1)
        # one of values not loaded
        if self.current is None or self.start is None or self.target is None:
            return float(0)
        # count progress percent if values can cast to float
        try:
            s = float(self.start)
            t = float(self.target)
            c = float(self.current)
            if abs(t - s) == 0:
                return float(1)
            return float(abs(c - s) / abs(t - s))
        except ValueError:
            pass
        # count progress in other situation
        if self.target == self.current:
            return float(1)
        else:
            return float(0)

    async def _coro(self, api):
        # wait for your turn
        for o in self._wait_for_others:
            try:
                await o.wait()
            except ResourceCommandError as e:
                self.get_finish_future().set_exception(ResourceCommandError(f"the higher-priority procedure "
                                                                            f"({type(self).__name__}) failed"))
                self.finish_timestamp = time.time()
                return

        CQ = await api.subscribe(address=self._address,
                                 time_of_data_tolerance=0.3, delay=0.1)
        CQ.start()
        try:
            while True:
                await asyncio.sleep(0)
                try:
                    result = await CQ.get_response()
                except CommunicationRuntimeError as e:
                    self.get_finish_future().set_exception(ResourceCommandError(str(e)))
                    return

                single_result = result[0]
                if not single_result.status:
                    self.get_finish_future().set_exception(ResourceCommandError(f"Get error form server:"
                                                                                f" {str(single_result.error)}"))
                    return
                if single_result.value:
                    if self.start is None:
                        self.start = single_result.value.v
                    self.current = single_result.value.v
                    try:
                        res = self._check_finish()
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        raise
                    except Exception as e:
                        self.get_finish_future().set_exception(ResourceCommandError(str(e)))
                        return
                    if res:
                        self.get_finish_future().set_result(True)
                        return
                else:
                    self.get_finish_future().set_exception(
                        ResourceCommandError("Response from server have no errors but "
                                             "value is empty"))
                    return
        except (asyncio.CancelledError, asyncio.TimeoutError):
            self.get_finish_future().set_exception(ResourceCommandError("Waiting for the target to be reached has been "
                                                                        "interrupted"))
        finally:
            self.finish_timestamp = time.time()
            if not self.get_finish_future().done():
                self.get_finish_future().set_exception(
                    ResourceCommandError(f"Main method in {type(self).__name__} finish but "
                                         f"future not done"))
            await CQ.stop_and_wait()

    async def run(self, api):
        if self.task:
            self.task.cancel()
        self._finish_fut = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(wait_for_psce(self._coro(api=api), timeout=self.timeout))
