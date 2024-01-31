import logging
from typing import Tuple
from pyaraucaria.coordinates import ra_to_decimal, dec_to_decimal, ra_dec_epoch, ra_dec_2_az_alt
from obsrv.data_colection.resource_manager.errors import ResourceCommandError
from obsrv.data_colection.resource_manager.resource_manager import TelescopeComponentManager
from obsrv.planrunner.errors import PlanBuildError
from obsrv.planrunner.moves.custom_moves.dome_slew import DomeSlew

logger = logging.getLogger(__name__.rsplit('.')[-1])


class DomeSlewRaDec(DomeSlew):
    _USED_RESOURCE = TelescopeComponentManager.DOME

    def __init__(self, ra_dec: Tuple, plan_data, resource_nr: int = 0, mount_resource_nr=None):
        self._mount_resource_nr = mount_resource_nr if mount_resource_nr is not None else resource_nr
        self._mount_resource_content = None
        self._ra = ra_dec[0]
        self._dec = ra_dec[1]
        self._real_ra = None
        self._real_dec = None
        super().__init__(plan_data=plan_data, resource_nr=resource_nr, az=0)  # az should not be None here

    def _build(self):
        # ---- virtual move ----
        if self._ra is None and self._dec is None:
            self._virtual_move = True
        # get mount context - here because _build method is call in __init__ method of inherit class
        self._mount_resource_content = self.res_mngr.get_resource(TelescopeComponentManager.MOUNT, nr=self._mount_resource_nr)

        super()._build()

    def _prepare_real_coordinates(self):
        # prepare ra dec values to convert for alt az
        try:
            ra_f = ra_to_decimal(self._ra)
            dec_f = dec_to_decimal(self._dec)
            ra_now, dec_now = ra_dec_epoch(ra=ra_f,
                                           dec=dec_f,
                                           epoch=str(self._mount_resource_content.epoch))
            self._real_ra = ra_now
            self._real_dec = dec_now
        except Exception as e:
            raise PlanBuildError(f"Error when converting RA DEC values. {str(e)}")

    async def _action(self):
        # convert ra dec to alt az
        try:
            az, alt = ra_dec_2_az_alt(ra=self._real_ra,
                                      dec=self._real_dec,
                                      latitude=self._mount_resource_content.latitude,
                                      longitude=self._mount_resource_content.longitude,
                                      elevation=self._mount_resource_content.elevation,
                                      epoch=self._mount_resource_content.epoch)
        except Exception as e:
            raise ResourceCommandError(f"Error when converting RA DEC values. {str(e)}")

        self._real_az = az

        # ------------ update position az -------------
        for p in self.positions:
            if p.get_name() == self._position_az_name:
                p.target = self._real_az
                break
        await super()._action()
