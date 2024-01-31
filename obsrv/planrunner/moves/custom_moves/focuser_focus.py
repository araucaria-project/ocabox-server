import logging
from typing import Optional

from obcom.comunication.comunication_error import CommunicationTimeoutError, CommunicationRuntimeError
from obsrv.data_colection.resource_manager.errors import ResourceCommandError
from obsrv.data_colection.resource_manager.resource_manager import TelescopeComponentManager
from obsrv.planrunner.errors import PlanBuildError
from obsrv.planrunner.moves.move import Move
from obsrv.planrunner.moves.position import Position

logger = logging.getLogger(__name__.rsplit('.')[-1])


class FocuserFocus(Move):
    _USED_RESOURCE = TelescopeComponentManager.FOCUSER

    def __init__(self, focus: Optional[int], plan_data, resource_nr=0):
        super().__init__(plan_data=plan_data)
        self._resource_nr = resource_nr
        self._resource_content = self.res_mngr.get_resource(self._USED_RESOURCE, nr=self._resource_nr)
        self._focus = focus  # rare focus value
        self._target_focus = None  # filter nr
        self._build()

    def _build(self):
        if self._focus is None:
            self._virtual_move = True
        if self._virtual_move:
            return
        try:
            self._target_focus = int(self._focus)
        except ValueError:
            raise PlanBuildError(f"Wrong focus. Can not read focus value - wrong format")
        tolerance = self._resource_content.focus_tolerance
        # here position has random value in target, it will be updated in a_init() method
        self.add_position(
            Position(f"{self._resource_content.adr}.position", t=self._target_focus, bounds=(tolerance, tolerance)))

    async def _action(self):
        # ----------------- set position focus -----------------
        try:
            result = await self.api.put_async(address=f"{self._resource_content.adr}.move",
                                              parameters_dict={"Position": self._target_focus}, no_wait=False)
        except (CommunicationRuntimeError, CommunicationTimeoutError) as e:
            raise ResourceCommandError(f"Action has error {str(e)}")
        if not result.status:
            raise ResourceCommandError(f"Action has error status in response {result.error}")
        await self.watch()
