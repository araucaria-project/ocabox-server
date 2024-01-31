import asyncio
from typing import Protocol, runtime_checkable


@runtime_checkable
class PlanTaskManagerProtocol(Protocol):

    async def run_task_when_can(self, coro, is_independent: bool) -> asyncio.Task:
        pass
