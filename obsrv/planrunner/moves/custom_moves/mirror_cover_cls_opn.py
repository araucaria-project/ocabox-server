import logging

from obcom.comunication.comunication_error import CommunicationRuntimeError, CommunicationTimeoutError
from obsrv.data_colection.resource_manager.errors import ResourceCommandError
from obsrv.data_colection.resource_manager.resource_manager import TelescopeComponentManager
from obsrv.planrunner.moves.move import Move
from obsrv.planrunner.moves.position import Position

logger = logging.getLogger(__name__.rsplit('.')[-1])


class MirrorCoverClsOpn(Move):
    _USED_RESOURCE = TelescopeComponentManager.COVERCALIBRATOR

    def __init__(self, action: bool, plan_data, resource_nr=0):
        super().__init__(plan_data=plan_data)
        self._resource_nr = resource_nr
        self._resource_content = self.res_mngr.get_resource(self._USED_RESOURCE, nr=self._resource_nr)
        self._action_ = action
        self._target_position_nr = None
        self._build()

    def _build(self):
        if self._action_:
            self._target_position_nr = self._resource_content.COVER_STATUS_MAP.get("open")
        else:
            self._target_position_nr = self._resource_content.COVER_STATUS_MAP.get("closed")

        self.add_position(
            Position(f"{self._resource_content.adr}.coverstate", t=self._target_position_nr))

    async def _action(self):
        # ----------------- open/close covers -----------------
        try:
            if self._action_:
                result = await self.api.put_async(address=f"{self._resource_content.adr}.opencover", no_wait=False)
            else:
                result = await self.api.put_async(address=f"{self._resource_content.adr}.closecover", no_wait=False)
        except (CommunicationRuntimeError, CommunicationTimeoutError) as e:
            raise ResourceCommandError(f"Action has error {str(e)}")
        if not result.status:
            raise ResourceCommandError(f"Action has error status in response {result.error}")
        await self.watch()
