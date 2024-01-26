from typing import Protocol, runtime_checkable


@runtime_checkable
class TreeConditionalFreezerProtocol(Protocol):

    async def set_change_event(self):
        pass

