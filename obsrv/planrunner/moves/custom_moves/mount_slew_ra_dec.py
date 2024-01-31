import logging
from typing import Tuple
from pyaraucaria.coordinates import ra_to_decimal, dec_to_decimal, ra_dec_epoch, ra_dec_2_az_alt
from obcom.comunication.comunication_error import CommunicationTimeoutError, CommunicationRuntimeError
from obsrv.data_colection.resource_manager.resource_manager import TelescopeComponentManager
from obsrv.planrunner.errors import PlanBuildError
from obsrv.planrunner.moves.move import Move
from obsrv.planrunner.moves.position import Position
from obsrv.data_colection.resource_manager.errors import ResourceCommandError

logger = logging.getLogger(__name__.rsplit('.')[-1])


class MountSlewRaDec(Move):
    _USED_RESOURCE = TelescopeComponentManager.MOUNT

    def __init__(self, ra_dec: Tuple, plan_data, resource_nr=0):
        super().__init__(plan_data=plan_data)
        self._resource_nr = resource_nr
        self._resource_content = self.res_mngr.get_resource(self._USED_RESOURCE, nr=self._resource_nr)
        self._ra = ra_dec[0]
        self._dec = ra_dec[1]
        self._virtual_move = False
        self._position_ra_name = "ra"
        self._position_dec_name = "dec"
        self._real_ra = None
        self._real_dec = None
        self._build()

    def _build(self):
        # ---- virtual move ----
        if self._ra is None and self._dec is None:
            self._virtual_move = True
        if self._virtual_move:
            return

        try:
            ra_f = ra_to_decimal(self._ra)
            dec_f = dec_to_decimal(self._dec)
            ra_now, dec_now = ra_dec_epoch(ra=ra_f,
                                           dec=dec_f,
                                           epoch=str(self._resource_content.epoch))
            self._real_ra = ra_now
            self._real_dec = dec_now
        except Exception as e:
            raise PlanBuildError(f"Error when converting RA DEC values. {str(e)}")

        self.add_position(
            Position(f"{self._resource_content.adr}.rightascension", t=self._real_ra, bounds=(0.5, 0.5),
                     name=self._position_ra_name))
        self.add_position(
            Position(f"{self._resource_content.adr}.declination", t=self._real_dec, bounds=(0.5, 0.5),
                     name=self._position_dec_name))
        self.add_position(Position(f"{self._resource_content.adr}.tracking", t=True))
        self.add_position(Position(f"{self._resource_content.adr}.slewing", t=False, order=2))

    async def _action(self):
        # todo podpytać czy ra_dec_2_az_alt napewno musi być przeliczane w momęcie wykonania
        try:
            az, alt = ra_dec_2_az_alt(ra=self._real_ra,
                                      dec=self._real_dec,
                                      latitude=self._resource_content.latitude,
                                      longitude=self._resource_content.longitude,
                                      elevation=self._resource_content.elevation,
                                      epoch=self._resource_content.epoch)  # todo ten epoch musi być z kwargs a potem z settings
        except Exception as e:
            raise ResourceCommandError(f"Error when converting RA DEC values. {str(e)}")
        if alt <= self._resource_content.min_alt:
            raise ResourceCommandError(f"Altitude is lover then min altitude")

        # ----------------- set tracking -----------------
        try:
            result = await self.api.put_async(address=f"{self._resource_content.adr}.tracking",
                                              parameters_dict={"Tracking": True}, no_wait=False)
        except (CommunicationRuntimeError, CommunicationTimeoutError) as e:  # probably newer happened
            raise ResourceCommandError(f"Action has error {str(e)}")
        if not result.status:
            raise ResourceCommandError(f"Action has error status in response {result.error}")
        # ----------------- move mount -----------------
        try:
            result = await self.api.put_async(address=f"{self._resource_content.adr}.slewtocoordinatesasync",
                                              parameters_dict={"RightAscension": self._real_ra,
                                                               "Declination": self._real_dec},
                                              no_wait=False)
        except (CommunicationRuntimeError, CommunicationTimeoutError) as e:  # probably newer happened
            raise ResourceCommandError(f"Action has error {str(e)}")
        if not result.status:
            raise ResourceCommandError("Action has error status in response")
        await self.watch()
