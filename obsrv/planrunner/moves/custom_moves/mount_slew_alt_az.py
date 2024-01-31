import logging
from typing import Tuple

from pyaraucaria.coordinates import deg_to_decimal_deg

from obcom.comunication.comunication_error import CommunicationRuntimeError, CommunicationTimeoutError
from obsrv.data_colection.resource_manager.errors import ResourceCommandError
from obsrv.data_colection.resource_manager.resource_manager import TelescopeComponentManager
from obsrv.planrunner.errors import PlanBuildError
from obsrv.planrunner.moves.move import Move
from obsrv.planrunner.moves.position import Position

logger = logging.getLogger(__name__.rsplit('.')[-1])


class MountSlewAltAz(Move):
    _USED_RESOURCE = TelescopeComponentManager.MOUNT

    def __init__(self, alt_az: Tuple, plan_data, resource_nr=0):
        super().__init__(plan_data=plan_data)
        self._resource_nr = resource_nr
        self._resource_content = self.res_mngr.get_resource(self._USED_RESOURCE, nr=self._resource_nr)
        self._alt = alt_az[0]
        self._az = alt_az[1]
        self._virtual_move = False
        self._position_alt_name = "alt"
        self._position_az_name = "az"
        self._real_alt = None
        self._real_az = None
        self._build()

    def _build(self):
        # ---- virtual move ----
        if self._alt is None and self._az is None:
            self._virtual_move = True
        if self._virtual_move:
            return

        try:
            self._real_alt = deg_to_decimal_deg(self._alt)
            self._real_az = deg_to_decimal_deg(self._az)
        except (ValueError, TypeError):
            raise PlanBuildError(f"Wrong az alt format")
            # check min alt
        if self._real_alt <= self._resource_content.min_alt:
            raise PlanBuildError(f"Altitude is lover then min altitude")

        self.add_position(
            Position(f"{self._resource_content.adr}.altitude", t=self._real_alt, bounds=(0.5, 0.5),
                     name=self._position_alt_name))
        self.add_position(
            Position(f"{self._resource_content.adr}.azimuth", t=self._real_az, bounds=(0.5, 0.5),
                     name=self._position_az_name))
        self.add_position(Position(f"{self._resource_content.adr}.tracking", t=False))
        self.add_position(Position(f"{self._resource_content.adr}.slewing", t=False, order=2))

    async def _action(self):
        # todo zacząć uzywać głównego API a nie RARE API
        # ----------------- set tracking -----------------
        try:
            result = await self.api.put_async(address=f"{self._resource_content.adr}.tracking",
                                              parameters_dict={"Tracking": False}, no_wait=False)
        except (CommunicationRuntimeError, CommunicationTimeoutError) as e:  # probably newer happened
            raise ResourceCommandError(f"Action has error {str(e)}")
        if not result.status:
            raise ResourceCommandError(f"Action has error status in response {result.error}")
        # ----------------- move mount -----------------
        try:
            result = await self.api.put_async(address=f"{self._resource_content.adr}.slewtoaltazasync",
                                              parameters_dict={"Azimuth": self._real_az,
                                                               "Altitude": self._real_alt},
                                              no_wait=False)
        except (CommunicationRuntimeError, CommunicationTimeoutError) as e:  # probably newer happened
            raise ResourceCommandError(f"Action has error {str(e)}")
        if not result.status:
            raise ResourceCommandError("Action has error status in response")
        await self.watch()
