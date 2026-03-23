import asyncio
import logging
import param
from obsrv.utils.asyncio_util_functions import wait_for_psce
from typing import Optional

logger = logging.getLogger(__name__.rsplit('.')[-1])


param.parameterized.async_executor = lambda f: asyncio.create_task(f())


class OcaboxTask(param.Parameterized):
    time_tick_s = param.Number(default=1.0, doc="Time tick in seconds", allow_None=True)

    def __init__(self, **params):
        super().__init__(**params)
        self.task = None
        self.stop_running_event: Optional[asyncio.Event] = None

    async def on_time_tick(self):
        """This method is called every time_tick_s seconds. Override it to implement your periodic task."""
        pass

    async def on_start(self):
        """This method is called when the task is started. Override it to implement your task start."""
        pass

    async def on_stop(self):
        """This method is called when the task is stopped. Override it to implement your task stop."""
        pass

    async def run(self):
        logger.info(f'Starting {self}')
        try:
            self.stop_running_event = asyncio.Event()
            self.task = asyncio.create_task(self._run())
        except Exception as e:
            logger.error(f'Error starting {self}: {e}')
            raise

    async def _run(self):
        logger.info(f'Task starting {self}')
        await self.on_start()
        while not self.stop_running_event.is_set():
            try:
                await self.on_time_tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Method in task {self} raise sam errors: {e}")
                # close task because method is corrupted
                self.stop_running_event.set()
            try:
                await wait_for_psce(self.stop_running_event.wait(), timeout=self.time_tick_s)
            except asyncio.TimeoutError:
                pass
        logger.info(f'Task finished {self}')

    async def stop(self):
        logger.info(f'Stopping requested {self}')
        await self.on_stop()
        self.stop_running_event.set()
        if self.task is not None:
            t = self.task
            self.task = None
            logger.info(f'Stopping {self}')
            await t
            logger.info(f'Stopped {self}')
        else:
            logger.warning('Task EphemerisData is not running, so it can not be stopped')
