import logging
from typing import Optional

from obcom.comunication.comunication_error import CommunicationTimeoutError, CommunicationRuntimeError
from obsrv.data_colection.resource_manager.errors import ResourceCommandError
from obsrv.data_colection.resource_manager.resource_manager import TelescopeComponentManager
from obsrv.planrunner.errors import PlanBuildError
from obsrv.planrunner.moves.move import Move
from obsrv.planrunner.moves.position import Position

logger = logging.getLogger(__name__.rsplit('.')[-1])


class FilterWheel(Move):
    _USED_RESOURCE = TelescopeComponentManager.FILTERWHEEL

    def __init__(self, filter_: Optional[str], plan_data, resource_nr=0):
        super().__init__(plan_data=plan_data)
        self._resource_nr = resource_nr
        self._resource_content = self.res_mngr.get_resource(self._USED_RESOURCE, nr=self._resource_nr)
        self._filter = filter_  # rare filter name
        self._target_filter = None  # filter nr
        self._position_filter_name = "filter position"
        self._build()

    def _build(self):
        if self._filter is None:
            self._virtual_move = True
        if self._virtual_move:
            return
        # here position has random value in target, it will be updated in a_init() method
        self.add_position(
            Position(f"{self._resource_content.adr}.position", t=self._target_filter, name=self._position_filter_name))

    async def _action(self):
        # ----------------- set filter -----------------
        try:
            result = await self.api.put_async(address=f"{self._resource_content.adr}.position",
                                              parameters_dict={"Position": self._target_filter}, no_wait=False)
        except (CommunicationRuntimeError, CommunicationTimeoutError) as e:
            raise ResourceCommandError(f"Action has error {str(e)}")
        if not result.status:
            raise ResourceCommandError(f"Action has error status in response {result.error}")
        await self.watch()

    async def a_init(self):
        if self._virtual_move:
            return
        filters: dict = await self.res_mngr.get_resource(self._USED_RESOURCE).get_filters()
        if not filters:
            raise PlanBuildError(f"No filter found. Can not find list of filters in resource manager")
        fil = filters.get(self._filter, None)
        if fil is None:  # check is None because fil can be 0 and bool(0) == False
            raise PlanBuildError(f"Wrong filter. Can not find given filter name in list of filters")
        self._target_filter = fil

        # update Positions
        for p in self.positions:
            if p.get_name() == self._position_filter_name:
                p.target = self._target_filter
