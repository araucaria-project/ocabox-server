import asyncio
import logging
import math
import os
import random
import re
from datetime import datetime
from typing import List, Optional

from pyaraucaria.date import datetime_to_julian, julian_to_tuple
from pyaraucaria.fits import fits_header, save_fits_from_array

from obcom.comunication.comunication_error import CommunicationTimeoutError, CommunicationRuntimeError
from obsrv.data_colection.resource_manager.errors import ResourceCommandError
from obsrv.data_colection.resource_manager.resource_manager import TelescopeComponentManager
from obsrv.planrunner.errors import PlanBuildError
from obsrv.planrunner.moves.action import Action
from obsrv.planrunner.moves.move import Move

logger = logging.getLogger(__name__.rsplit('.')[-1])


class CameraReadPicture(Move):
    _USED_RESOURCE = TelescopeComponentManager.CAMERA

    def __init__(self, exp_time_start: datetime, plan_data, resource_nr=0, mount_resource_nr=None,
                 exposure_time: float = None, req_ra=None, req_dec=None, epoch=None, type_=None, obj_name=None,
                 filter_=None, observer=None):
        super().__init__(plan_data=plan_data)
        self._resource_nr = resource_nr
        self._mount_resource_nr = mount_resource_nr if mount_resource_nr is not None else self._resource_nr
        self._resource_content = self.res_mngr.get_resource(self._USED_RESOURCE, nr=self._resource_nr)
        self._mount_resource_content = self.res_mngr.get_resource(TelescopeComponentManager.MOUNT,
                                                                  nr=self._mount_resource_nr)
        if self._mount_resource_content is None:
            raise PlanBuildError(f"Can not find mount resource content for camera")
        self._exposure_time: Optional[float] = exposure_time

        # ------- fits file data --------
        self._req_ra = req_ra
        self._req_dec = req_dec
        self._epoch = epoch
        self._type_ = type_
        self._obj_name = obj_name
        self._filter_ = filter_
        self._observer = observer
        # ------------------------------

        self.exp_time_start: datetime = exp_time_start
        if self.exp_time_start is None or not isinstance(self.exp_time_start, datetime):
            raise PlanBuildError(f"Exposition start time iss needed and must be datatime type")

        # todo uregulować sprawę z folederm do zapisów teraz jest zawsze brany ~/Desktop bo nie ma dostępu do base_fits_dir
        self.base_fits_folder = os.path.expanduser(
            self._mount_resource_content.properties.get('base_fits_dir', f'~/Desktop'))
        self.folder = os.path.join(self.base_fits_folder, f'fits_{self._resource_content.telescope_id}')
        self.folder_focusing = os.path.join(self.folder, f'focus')
        self.target_folder = self._provide_target_folder()

        self._focus_id = self._focus_id = random.randrange(0, 100000)  # todo co to jest?

        self._build()

    def _build(self):
        self.add_position(
            Action(f"", timeout=120,
                   custom_action=self._save_fits(self._exposure_time)))

    async def _action(self):
        await self.watch()

    @staticmethod
    def get_oca_date(jd: float) -> float:
        return jd - 2460000

    async def _save_fits(self, sub_time: float) -> bool:

        jd = datetime_to_julian(self.exp_time_start)
        jd_oca = f"{self.get_oca_date(jd):010.5f}"

        # --------------- multi request to alpaca -------------------
        tasks = [asyncio.ensure_future(c) for c in
                 [self._get_request_safe(address=f"{self._resource_content.adr}.imagearray"),
                  self._get_request_safe(address=f"{self._resource_content.adr}.gains"),
                  self._get_request_safe(address=f"{self._resource_content.adr}.gain"),
                  self._get_request_safe(address=f"{self._resource_content.adr}.readoutmodes"),
                  self._get_request_safe(address=f"{self._resource_content.adr}.readoutmode"),
                  self._get_request_safe(address=f"{self._resource_content.adr}.sensorname"),
                  self._get_request_safe(address=f"{self._resource_content.adr}.ccdtemperature"),
                  self._get_request_safe(address=f"{self._resource_content.adr}.binx"),
                  self._get_request_safe(address=f"{self._resource_content.adr}.biny")]]

        try:
            array, gains, gain, readoutmodes, readoutmode, sensorname, ccdtemperature, binx, \
                biny = await asyncio.gather(*tasks)
        except Exception as e:
            raise ResourceCommandError(f"Can not get image array or any of needed parameters from alpaca. Error: {e}")
        finally:
            for t in tasks:
                t.cancel()

        # -----------------------------------------------------------

        gain = self.indexing_value(gain, gains)
        readoutmode = self.indexing_value(readoutmode, readoutmodes)
        header = fits_header(
            obs_lat=self._mount_resource_content.latitude,
            obs_lon=self._mount_resource_content.longitude,
            obs_elev=self._mount_resource_content.elevation,
            tel_id=str(self._resource_content.telescope_id),  # observatory name like zb08
            utc_now=str(self.exp_time_start.isoformat(sep='T', timespec='auto')),
            jd=jd,
            req_ra=self._req_ra,
            req_dec=self._req_dec,
            epoch=self._epoch,
            ra_obj='',
            dec_obj='',
            tel_ra="none",
            tel_dec="none",  # todo wypełnić te paramtry jakimiś danymi
            tel_alt="none",
            tel_az="none",
            airmass='',
            obs_mode='',
            focus="none",
            rotator_pos="none",
            observer=str(self._observer),
            image_type='',
            obs_type=str(self._type_),
            object=str(self._obj_name),
            filter=str(self._filter_),
            ccd_temp=ccdtemperature,
            binx=binx,
            biny=biny,
            read_mod=str(readoutmode),
            gain=str(gain)
        )
        if self._type_ == 'snap':
            file_name = f'snap'
        else:
            file_name = f'{self._resource_content.telescope_id}c_{jd_oca.split(".")[0]}_{jd_oca.split(".")[1]}'
        # stat = fits_stat(array, size=100, median=False, std=False, min=False, max=False)
        save_fits_from_array(array,
                             self.target_folder,
                             file_name,
                             header,
                             overwrite=True,
                             dtyp='sideint16')

        return True

    @staticmethod
    def indexing_value(value: int, values: List[str]) -> int or str:
        if isinstance(value, int) and isinstance(values, list):
            data = values[value]
        else:
            data = str(value)
        return data

    def _provide_target_folder(self) -> str:
        # todo sprawdzić czy nie ma problemu z wydajnością przy zapisywaniu do pliku
        if self._type_ == 'focusing':
            lst = os.listdir(self.folder_focusing)
            lst_2 = [int(n) for n in lst if re.fullmatch(r'\d{6}', n)]
            if len(lst_2) == 0:
                num_last = -1
            else:
                num_last = max(lst_2)
            num = num_last + 1
            if os.path.exists(os.path.join(self.folder_focusing, f'{num_last:06d}', f'{self._focus_id}')):
                target_folder = os.path.join(self.folder_focusing, f'{num_last:06d}')
            else:
                target_folder = os.path.join(self.folder_focusing, f'{num:06d}')
                self._mk_folder(target_folder)
                open(os.path.join(target_folder, f'{self._focus_id}'), 'a').close()
                if os.path.exists(os.path.join(self.folder_focusing, f'actual')):
                    if os.readlink(os.path.join(self.folder_focusing, f'actual')) is not target_folder:
                        os.remove(os.path.join(self.folder_focusing, f'actual'))
                        self._mk_symlink(target_folder, os.path.join(self.folder_focusing, f'actual'))
                else:
                    self._mk_symlink(target_folder, os.path.join(self.folder_focusing, f'actual'))
        elif self._type_ == 'snap':
            target_folder = self.folder
        else:
            jd_tuple = julian_to_tuple(math.floor(datetime_to_julian(str(datetime.utcnow()))) + 0.1)
            mnth = self.add_zero(jd_tuple[1])
            day = self.add_zero(jd_tuple[2])
            simlink_day = f'{jd_tuple[0]}_{mnth}_{day}'
            name = f"{math.floor(self.get_oca_date(datetime_to_julian(str(datetime.utcnow())))):04.0f}"
            target_folder = os.path.join(self.folder, f'{name}')
            if not os.path.exists(target_folder):
                self._mk_folder(target_folder)
            if os.path.exists(os.path.join(self.folder, f'actual')):
                if not os.readlink(os.path.join(self.folder, f'actual')) == target_folder:
                    os.remove(os.path.join(self.folder, f'actual'))
                    self._mk_symlink(target_folder, os.path.join(self.folder, f'actual'))
            else:
                self._mk_symlink(target_folder, os.path.join(self.folder, f'actual'))
            if os.path.exists(os.path.join(self.folder, simlink_day)) is False:
                self._mk_symlink(target_folder, os.path.join(self.folder, simlink_day))
        return target_folder

    @staticmethod
    def _mk_folder(folder: str):
        if not os.path.exists(folder):
            try:
                os.makedirs(folder, exist_ok=True)
            except Exception as e:
                raise PlanBuildError(f"Folder creation failed")

    @staticmethod
    def _mk_symlink(target: str, symlink: str):
        os.symlink(src=target, dst=symlink, dir_fd=None)

    @staticmethod
    def add_zero(numb: int):
        return f'{numb:02d}'
