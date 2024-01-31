from abc import ABC, abstractmethod
from typing import Optional, List, Tuple

from pyaraucaria.coordinates import ra_to_decimal, dec_to_decimal

from obsrv.planrunner.commands.commands_args_map import CommandArgsMap
from obsrv.planrunner.commands.commands_names import CommandsNames
from obsrv.planrunner.commands.commands_types import CommandsTypes
from obsrv.planrunner.dither_data import DitherData
from obsrv.planrunner.errors import PlanBuildError
from obsrv.planrunner.focusing_data import FocusingData
from obsrv.planrunner.sequence_data import SequenceData


class CommandArgsReader(ABC):
    _NAME: str = "DEFAULT"
    DEFAULT_TRACKING_ON = CommandArgsMap.TRACKING.OFF.val()
    _DEFAULT_DITHER_ON = False
    _POSSIBLE_DITHER = False

    def __init__(self, arg, kwarg, **kwargs):
        super().__init__(**kwargs)
        self._args = arg
        self._kwargs = kwarg
        self._seq: List[Tuple[SequenceData, FocusingData]] = self._create_seq_list()  # can raise PlanBuildError
        self._dither_data: DitherData = self._create_dither_data()
        self._mapped_args: dict = {CommandArgsMap.RA: CommandArgsMap.RA.val(),
                                   CommandArgsMap.DEC: CommandArgsMap.DEC.val(),
                                   CommandArgsMap.OBJ_NAME: CommandArgsMap.OBJ_NAME.val()}
        self.validate()
        # ==== object name ====
        if len(self._args) >= 1:
            self._mapped_args[CommandArgsMap.OBJ_NAME] = self._args[0]
        # ==== ra dec ====
        if len(self._args) >= 3:
            self._mapped_args[CommandArgsMap.RA] = ra_to_decimal(self._args[1])
            self._mapped_args[CommandArgsMap.DEC] = dec_to_decimal(self._args[2])

    @abstractmethod
    def validate(self):
        """
        Method validate command arguments

        :raise PlanBuildError
        :return:
        """
        raise NotImplementedError

    def _create_seq_list(self) -> List[Tuple[SequenceData, FocusingData]]:
        """

        :raise PlanBuildError
        :return:
        """
        lis = []
        for s in self._plan_seq_rare_list:
            sd = SequenceData(seq=s)
            sd.validate()
            fd = FocusingData(seq_foc=self.focus_list_rare, expositions_quantity=sd.exp_quantity,
                              defocus=self.defocus)
            fd.validate()
            lis.append((sd, fd))
        return lis

    def _create_dither_data(self):
        """

        :raise PlanBuildError
        :return:
        """
        # validate is dither possible
        if not self._POSSIBLE_DITHER and self.dither_rare:
            raise PlanBuildError(f"Find argument: {CommandArgsMap.DITHER} but in this command is not available")
        d = DitherData(self.dither_rare)
        d.validate()
        return d

    @property
    def defocus(self) -> int:
        return int(self._kwargs.get(CommandArgsMap.FOCUSING_OFFSET, CommandArgsMap.FOCUSING_OFFSET.val()))

    @property
    def uobi(self) -> int:
        v = self._kwargs.get(CommandArgsMap.UOBI, None)
        if v is not None:
            return self._kwargs[CommandArgsMap.UOBI]
        else:
            return CommandArgsMap.UOBI.val()

    @property
    def _dome_follow(self) -> bool:
        v = self._kwargs.get(CommandArgsMap.DOME_FOLLOW, None)
        if v is not None:
            if CommandArgsMap.DOME_FOLLOW.has_item(v):
                return CommandArgsMap.DOME_FOLLOW.get_item_by_name(v).val()
        return CommandArgsMap.DOME_FOLLOW.ON.val()

    @property
    def epoch(self) -> str:
        return str(self._kwargs.get(CommandArgsMap.EPOCH, CommandArgsMap.EPOCH.val()))

    @property
    def dither_on(self) -> bool:
        """
        Do dithering or not.

        :return: bool
        """
        if not self._POSSIBLE_DITHER:
            return False

        dit = self._kwargs.get(CommandArgsMap.DITHER, CommandArgsMap.DITHER.val())
        if self._DEFAULT_DITHER_ON:
            if not dit:
                return True
            elif dit != DitherData.PARAM_VAL_OFF_DEFAULT_DITHER:
                return True
            elif dit == DitherData.PARAM_VAL_OFF_DEFAULT_DITHER:
                return False

        if not dit:
            return False
        elif dit != DitherData.PARAM_VAL_OFF_DEFAULT_DITHER:
            return True
        elif dit == DitherData.PARAM_VAL_OFF_DEFAULT_DITHER:
            return False
        return False

    @property
    def mirror_cover_mode(self):
        v = self._kwargs.get(CommandArgsMap.MIRROR_COVER, None)
        if v is not None:
            if CommandArgsMap.MIRROR_COVER.has_item(v):
                return v
        return None

    @property
    def observer(self) -> str:
        return self._kwargs.get(CommandArgsMap.OBSERVER, CommandArgsMap.OBSERVER.val())

    @property
    def screen_light_mode(self):
        v = self._kwargs.get(CommandArgsMap.SCREEN_LIGHT, None)
        if v is not None:
            if CommandArgsMap.SCREEN_LIGHT.has_item(v):
                return v
        return None

    # def _op_id(self, id_next: int) -> str:
    #     return f'{self.id}_{self.numb(id_next)}'

    # def _add_comp(self, sub: SubCommand, id_next: int) -> int:
    #     self.subcomponents[f'{self.id}_{self.numb(id_next)}'] = sub
    #     return id_next + 1

    @property
    def dither_rare(self) -> str:
        return str(self._kwargs.get(CommandArgsMap.DITHER, CommandArgsMap.DITHER.val()))

    @property
    def plan_object_name(self) -> Optional[str]:
        return self._mapped_args.get(CommandArgsMap.OBJ_NAME, CommandArgsMap.OBJ_NAME.val())

    @property
    def plan_ra(self) -> Optional[float]:
        return self._mapped_args.get(CommandArgsMap.RA, CommandArgsMap.RA.val())

    @property
    def plan_dec(self) -> Optional[float]:
        return self._mapped_args.get(CommandArgsMap.DEC, CommandArgsMap.DEC.val())

    @property
    def plan_az(self) -> Optional[float]:
        return self._kwargs.get(CommandArgsMap.AZ, CommandArgsMap.AZ.val())

    @property
    def plan_alt(self) -> Optional[float]:
        return self._kwargs.get(CommandArgsMap.ALT, CommandArgsMap.ALT.val())

    @property
    def plan_seq_rare(self) -> str:
        return str(self._kwargs.get(CommandArgsMap.SEQ, CommandArgsMap.SEQ.val()))

    @property
    def _plan_seq_rare_list(self) -> List[str]:
        return [i for i in self.plan_seq_rare.split(",") if i]

    @property
    def plan_seq(self) -> List[SequenceData]:
        return [a for a, b in self._seq]

    @property
    def plan_seq_focus_combination(self) -> List[Tuple[SequenceData, FocusingData]]:
        return self._seq

    @property
    def focus_seq(self) -> List[FocusingData]:
        return [b for a, b in self._seq]

    @property
    def dither(self):
        return self._dither_data

    @property
    def domeflat_dome_az(self) -> float or None:
        # TODO take this value from settings
        if CommandsNames.get_item_by_name(self._NAME).val() == CommandsTypes.FLAT:
            return 0.0
        else:
            return None

    @property
    def domeflat_mount_az(self) -> float or None:
        # TODO take this value from settings
        if CommandsNames.get_item_by_name(self._NAME).val() == CommandsTypes.FLAT:
            return self.domeflat_dome_az + 180.0
        else:
            return None

    @property
    def domeflat_mount_alt(self) -> float or None:
        # TODO take this value from settings
        if CommandsNames.get_item_by_name(self._NAME).val() == CommandsTypes.FLAT:
            return 15.0
        else:
            return None

    @property
    def tracking(self) -> bool:
        v = self._kwargs.get(CommandArgsMap.TRACKING, None)
        if v is not None:
            if CommandArgsMap.SCREEN_LIGHT.has_item(v):
                return CommandArgsMap.TRACKING.get_item_by_name(v).val()
        return self.DEFAULT_TRACKING_ON

    @property
    def object_name(self) -> str:
        name = ""
        if self.plan_object_name:
            name = self.plan_object_name
        else:
            if self._NAME in [CommandsNames.SKYFLAT, CommandsNames.FOCUS, CommandsNames.DOMEFLAT, CommandsNames.DARK,
                              CommandsNames.ZERO, CommandsNames.SNAP]:
                name = self._NAME.lower()
        return name

    @property
    def focus_list_rare(self) -> str:
        """
        Input format focus_target/focus_step
        :return: [focus_target, focus_step]
        """
        return self._kwargs.get(CommandArgsMap.FOCUSING, CommandArgsMap.FOCUSING.val())
