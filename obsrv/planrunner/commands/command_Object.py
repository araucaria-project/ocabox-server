import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple, List

from obsrv.data_colection.resource_manager.errors import ResourceCommandError
from obsrv.planrunner.command import Command
from obsrv.planrunner.commands.commands_args_map import CommandArgsMap
from obsrv.planrunner.commands.commands_names import CommandsNames
from obsrv.planrunner.commands.commands_types import CommandsTypes
from obsrv.planrunner.errors import ResourceError, PlanBuildError
from obsrv.planrunner.moves.custom_moves.camera_exposure import CameraExposure
from obsrv.planrunner.moves.custom_moves.camera_read_picture import CameraReadPicture
from obsrv.planrunner.moves.custom_moves.dome_slew import DomeSlew
from obsrv.planrunner.moves.custom_moves.dome_slew_ra_dec import DomeSlewRaDec
from obsrv.planrunner.moves.custom_moves.filter_wheel import FilterWheel
from obsrv.planrunner.moves.custom_moves.focuser_focus import FocuserFocus
from obsrv.planrunner.moves.custom_moves.mirror_cover_cls_opn import MirrorCoverClsOpn
from obsrv.planrunner.moves.custom_moves.mount_dither import MountDither, DitherShareStartPosition
from obsrv.planrunner.moves.custom_moves.mount_slew_alt_az import MountSlewAltAz
from obsrv.planrunner.moves.custom_moves.mount_slew_ra_dec import MountSlewRaDec
from obsrv.planrunner.moves.move import Move
from obsrv.planrunner.plan_status_map import PlanStatusMap

logger = logging.getLogger(__name__.rsplit('.')[-1])


class CommandObject(Command):
    _NAME = CommandsNames.OBJECT
    _POSSIBLE_DITHER = True

    @dataclass
    class _ExecuteSeqObject:
        filter: FilterWheel
        exp_list: List[Tuple[FocuserFocus, CameraExposure, CameraReadPicture, MountDither]]

    def __init__(self, command_dict: dict, plan_data, previous_command, id_: str = None,
                 loop: asyncio.AbstractEventLoop = None, **kwargs):
        super().__init__(command_dict=command_dict, plan_data=plan_data, id_=id_, previous_command=previous_command,
                         loop=loop, **kwargs)
        # ------ subcommands ------
        self._move_mount: Optional[Move] = None
        self._move_dome: Optional[Move] = None
        self._mirror_close: Optional[Move] = None
        self._mirror_open: Optional[Move] = None
        self._execute_seq_subcomands: List[CommandObject._ExecuteSeqObject] = []

        # ------ build subcommands ------
        self._create_modules_from_args()

    def validate(self):
        if not (len(self._args) == 1 or len(self._args) == 3):
            raise PlanBuildError(f"Wrong number of args")
        try:
            int(self.defocus)
        except ValueError:
            raise PlanBuildError(f"Wrong 'defocuser' value, can't cast to int")

    def _create_modules_from_args(self):
        """

        :raise PlanBuildError:
        :return:
        """
        try:
            # ----------- mount ----------
            if self.plan_alt is not None and self.plan_az is not None:
                self._move_mount = MountSlewAltAz(resource_nr=0, alt_az=(self.plan_alt,
                                                                         self.plan_az),
                                                  plan_data=self._plan_data)
            else:
                self._move_mount = MountSlewRaDec(resource_nr=0, ra_dec=(self.plan_ra,
                                                                         self.plan_dec),
                                                  plan_data=self._plan_data)

            # ----------- dome ----------
            if self.plan_alt is not None and self.plan_az is not None:
                self._move_dome = DomeSlew(resource_nr=0, az=self.plan_az,
                                           plan_data=self._plan_data)
            else:
                self._move_dome = DomeSlewRaDec(resource_nr=0, ra_dec=(self.plan_ra,
                                                                       self.plan_dec),
                                                plan_data=self._plan_data)

            # ----------- mirror cover ----------
            if self.mirror_cover_mode in (CommandArgsMap.MIRROR_COVER.OPEN, CommandArgsMap.MIRROR_COVER.AUTO):
                self._mirror_open = MirrorCoverClsOpn(action=True, resource_nr=0, plan_data=self._plan_data)
            if self.mirror_cover_mode in (CommandArgsMap.MIRROR_COVER.CLOSE, CommandArgsMap.MIRROR_COVER.AUTO):
                self._mirror_close = MirrorCoverClsOpn(action=False, resource_nr=0, plan_data=self._plan_data)
            # if None don't do anything with mirror

            # =============== EXPOSITION SEQUENCE ==============
            sharing_date_between_dithering = DitherShareStartPosition()
            for s, f in self.plan_seq_focus_combination:
                # ----------- change filter ------------
                _filter = FilterWheel(filter_=s.filter, plan_data=self._plan_data)
                exp_list = []
                # If no seq is given then this loop will not execute so there is no need to create virtual
                # camera commands
                for i in range(0, s.exp_quantity):
                    # ----------- set focus ----------------
                    _focuser = FocuserFocus(focus=f.focus_position(exp_no=i), plan_data=self._plan_data)
                    # ----------- camera exposure ----------
                    _camera_exposure = CameraExposure(exposure_time=s.exp_time, light=True,
                                                      plan_data=self._plan_data)
                    _camera_read = CameraReadPicture(exp_time_start=datetime.utcnow(), plan_data=self._plan_data,
                                                     resource_nr=0, mount_resource_nr=0, exposure_time=s.exp_time,
                                                     req_ra=self.plan_ra, req_dec=self.plan_dec, epoch=self.epoch,
                                                     type_=CommandsTypes.SCIENCE, obj_name=self.object_name,
                                                     filter_=s.filter, observer=self.observer)
                    dit_on = self.dither_on and self.dither.dither_this_exp(i)
                    _dither = MountDither(dither_frequency_nr=i,
                                          dither_mode=self.dither.dither_mode,
                                          dither_distance=self.dither.dither_distance, plan_data=self._plan_data,
                                          resource_nr=0, dither_share_start_position=sharing_date_between_dithering,
                                          virtual_move=not dit_on)
                    exp_list.append((_focuser, _camera_exposure, _camera_read, _dither))
                self._execute_seq_subcomands.append(CommandObject._ExecuteSeqObject(filter=_filter, exp_list=exp_list))
            # -----------

        except PlanBuildError:
            raise

    def _get_res(self, name: str, nr: int):
        m = self.res_mngr.get_resource(name=name, nr=nr)
        # TODO W przyszłości mogą być inne sterowniki teleskopów które będą mieć inne zasoby dlatego dorobić kiedyś
        #  trzeba kontrolę typu zasobu, np czy jest alpaca czy inny
        if m:
            return m
        raise ResourceError(f"Can't find resource")

    async def _is_loaded(self, resource_name: str, nr: int = 0):
        m = self.res_mngr.get_resource(name=resource_name, nr=nr)
        return m is not None

    async def _run_body(self):
        await self._wait_for_permission_to_locking()
        await self.status_pub.update_status(cmd_id=self.get_id(), data={PlanStatusMap.STATUS: PlanStatusMap.STATUS.RUN})
        try:
            #  --------- prepare step - check all needed resources is available ----------
            if self.res_mngr.get_resource(self.res_mngr.MOUNT) is None or \
                    self.res_mngr.get_resource(self.res_mngr.DOME) is None or \
                    self.res_mngr.get_resource(self.res_mngr.CAMERA) is None or \
                    self.res_mngr.get_resource(self.res_mngr.COVERCALIBRATOR) is None or \
                    self.res_mngr.get_resource(self.res_mngr.FOCUSER) is None or \
                    self.res_mngr.get_resource(self.res_mngr.FILTERWHEEL) is None:
                self.log("Some of the required resource is not available")
                return

            #  ---------  first step ----------
            async with self.res_mngr.get_resource(self.res_mngr.MOUNT), \
                    self.res_mngr.get_resource(self.res_mngr.DOME), \
                    self.res_mngr.get_resource(self.res_mngr.COVERCALIBRATOR):

                await self._move_mount.run()
                await self._move_dome.run()
                if self._mirror_open:
                    await self._mirror_open.run()

                await self._move_mount.wait()
                await self._move_dome.wait()
                if self._mirror_open:
                    await self._mirror_open.wait()

            #  ---------  second step ----------
            async with self.res_mngr.get_resource(self.res_mngr.CAMERA), \
                    self.res_mngr.get_resource(self.res_mngr.FOCUSER), \
                    self.res_mngr.get_resource(self.res_mngr.FILTERWHEEL), \
                    self.res_mngr.get_resource(self.res_mngr.COVERCALIBRATOR):
                last_read = None
                for ess in self._execute_seq_subcomands:
                    await ess.filter.run()

                    for foc, cam, read, dithering in ess.exp_list:
                        await foc.run()
                        await dithering.run()
                        await ess.filter.wait()  # attention ! in first loop step here will be wait but all the
                        # next steps always finish immediately because Move will be already finished. In first time
                        # we will save time
                        await foc.wait()
                        await dithering.wait()
                        if last_read is not None:
                            await last_read.wait()
                        await cam.run()
                        await cam.wait()

                        await read.run()
                        last_read = read

                self._unlock_next_command()  # unlock next command faster, before this command do last thing
                if self._mirror_close:
                    await self._mirror_close.run()
                if last_read is not None:  # last wait
                    await last_read.wait()
                if self._mirror_close:
                    await self._mirror_close.wait()

        except ResourceCommandError:
            await self.status_pub.update_status(cmd_id=self.get_id(),
                                                data={PlanStatusMap.STATUS: PlanStatusMap.STATUS.ERROR})
            self.log("Command have error")

    def _submoves(self):
        if self._move_mount: yield self._move_mount
        if self._move_dome: yield self._move_dome
        if self._mirror_close: yield self._mirror_close
        if self._mirror_open: yield self._mirror_open
        for x in self._execute_seq_subcomands:
            if x.filter: yield x.filter
            for focuserFocus, cameraExposure, cameraReadPicture, ditherModes in x.exp_list:
                if focuserFocus: yield focuserFocus
                if cameraExposure: yield cameraExposure
                if cameraReadPicture: yield cameraReadPicture
                if ditherModes: yield ditherModes

    async def reset(self):
        await super().reset()
        for m in self._submoves():
            if m is not None:
                await m.reset()

    async def a_init(self):
        await super().a_init()
        for m in self._submoves():
            if m is not None:
                await m.a_init()
