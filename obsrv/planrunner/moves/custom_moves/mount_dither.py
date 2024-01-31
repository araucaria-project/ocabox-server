import logging
from dataclasses import dataclass
from typing import Optional
from pyaraucaria.coordinates import ra_dec_2_az_alt
from obcom.comunication.comunication_error import CommunicationTimeoutError, CommunicationRuntimeError
from obsrv.data_colection.resource_manager.errors import ResourceCommandError
from obsrv.data_colection.resource_manager.resource_manager import TelescopeComponentManager
from obsrv.planrunner.dither_data import DitherModes
from obsrv.planrunner.moves.move import Move
from obsrv.planrunner.moves.position import Position

logger = logging.getLogger(__name__.rsplit('.')[-1])


@dataclass
class DitherShareStartPosition:
    ra: Optional[float] = None
    dec: Optional[float] = None


class MountDither(Move):
    """
    This move realize dithering.
    If it is not given a dither_share_start_position, the current value of rad dec will be taken each time and the
    effect of rolling dithering will be obtained
    """
    _USED_RESOURCE = TelescopeComponentManager.MOUNT

    def __init__(self, dither_frequency_nr: int, dither_mode: str, dither_distance: float, plan_data, resource_nr=0,
                 dither_share_start_position: Optional[DitherShareStartPosition] = None, virtual_move=False):
        super().__init__(plan_data=plan_data, virtual_move=virtual_move)
        self._resource_nr = resource_nr
        self._resource_content = self.res_mngr.get_resource(self._USED_RESOURCE, nr=self._resource_nr)
        self.dither_mode: str = dither_mode
        self.dither_distance: float = dither_distance
        self.dither_frequency_nr = dither_frequency_nr
        self._real_ra = None
        self._real_dec = None
        self._position_ra_name = "ra"
        self._position_dec_name = "dec"
        if dither_share_start_position is None:
            dither_share_start_position = DitherShareStartPosition()
        self._dither_share_start_position = dither_share_start_position
        self._build()

    def _build(self):
        # ---- virtual move ----
        if self._virtual_move:
            return

        self.add_position(
            Position(f"{self._resource_content.adr}.rightascension", t=self._real_ra, bounds=(0.5, 0.5),
                     name=self._position_ra_name))
        self.add_position(
            Position(f"{self._resource_content.adr}.declination", t=self._real_dec, bounds=(0.5, 0.5),
                     name=self._position_dec_name))
        self.add_position(Position(f"{self._resource_content.adr}.tracking", t=True))
        self.add_position(Position(f"{self._resource_content.adr}.slewing", t=False, order=2))

    async def _action(self):
        if self._virtual_move:
            return
        if self._dither_share_start_position.ra is None:
            self._dither_share_start_position.ra = await self._get_request_safe(
                address=f"{self._resource_content.adr}.rightascension")
        if self._dither_share_start_position.dec is None:
            self._dither_share_start_position.dec = await self._get_request_safe(
                address=f"{self._resource_content.adr}.declination")

        self._real_ra = DitherModes.ra_dither(ra=self._dither_share_start_position.ra,
                                              dec=self._dither_share_start_position.dec,
                                              dither_frequency_nr=self.dither_frequency_nr,
                                              dith_val=self.dither_distance,
                                              mode=self.dither_mode)
        self._real_dec = DitherModes.dec_dither(ra=self._dither_share_start_position.ra,
                                                dec=self._dither_share_start_position.dec,
                                                dither_frequency_nr=self.dither_frequency_nr,
                                                dith_val=self.dither_distance,
                                                mode=self.dither_mode)

        try:
            az, alt = ra_dec_2_az_alt(ra=self._real_ra,
                                      dec=self._real_dec,
                                      latitude=self._resource_content.latitude,
                                      longitude=self._resource_content.longitude,
                                      elevation=self._resource_content.elevation,
                                      epoch=self._resource_content.epoch)
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

        # ----- update positions -----
        for p in self.positions:
            if p.get_name() == self._position_ra_name:
                p.target = self._real_ra
            if p.get_name() == self._position_dec_name:
                p.target = self._real_dec
        await self.watch()
