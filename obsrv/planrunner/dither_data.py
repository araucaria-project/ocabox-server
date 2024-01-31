import random
from obsrv.planrunner.errors import PlanBuildError
from obsrv.util_functions.enum_map_base_class import BaseArgsMap, StrVal


class DitherModes(BaseArgsMap):

    @classmethod
    def is_mode_exist(cls, mode: str) -> bool:
        return cls.has_item(mode)

    # ------- here declare method for type dithering -------------
    @staticmethod
    def _count_dit_basic(coord: str, ra: float, dec: float, dither_frequency_nr: int, dith_val: float):
        if coord == "ra":
            return ra + random.uniform(-dith_val / 60, dith_val / 60)
        if coord == "dec":
            return dec + random.uniform(-dith_val / 60, dith_val / 60)

    # ------------------------------------------------------------
    # ------ here create name - method map for dither mode -------
    BASIC = StrVal("basic", _count_dit_basic)

    @classmethod
    def ra_dither(cls, ra: float, dec: float, dither_frequency_nr: int, dith_val: float, mode: str = BASIC):
        item = cls.get_item_by_name(mode)
        method = item.val().__func__
        return method(coord="ra", ra=ra, dec=dec, dither_frequency_nr=dither_frequency_nr, dith_val=dith_val)

    @classmethod
    def dec_dither(cls, ra: float, dec: float, dither_frequency_nr: int, dith_val: float, mode: str = BASIC):
        item = cls.get_item_by_name(mode)
        method = item.val().__func__
        return method(coord="dec", ra=ra, dec=dec, dither_frequency_nr=dither_frequency_nr, dith_val=dith_val)


class DitherData:
    """
    Class representing dither single sequence parameters
    """
    _DITHER_SEQUENCE_LEN = 3
    PARAM_VAL_OFF_DEFAULT_DITHER = "off"  # parameter witch turn off default dithering

    def __init__(self, seq: str):
        # check param option witch turning off dithering
        if seq == self.PARAM_VAL_OFF_DEFAULT_DITHER:
            seq = ""
        self._seq = seq.split('/') if seq else []

    def validate(self):
        """

        raise PlanBuildError
        :return:
        """
        if self._seq:
            if len(self._seq) != self._DITHER_SEQUENCE_LEN:
                raise PlanBuildError(f"The length of the dither sequence is wrong, should "
                                     f"be {self._DITHER_SEQUENCE_LEN}")
            try:
                t = str(self._seq[0])
            except ValueError:
                raise PlanBuildError(f"Wrong first value in dither sequence, can't cast to str")
            try:
                int(self._seq[1])
            except ValueError:
                raise PlanBuildError(f"Wrong first value in dither sequence, can't cast to int")
            try:
                float(self._seq[2])
            except ValueError:
                raise PlanBuildError(f"Wrong second value in dither sequence, can't cast to float")

    @property
    def dither_mode(self) -> str:
        if self._seq:
            return str(self._seq[0])
        return DitherModes.BASIC

    @property
    def dither_every_sub(self) -> int:
        if self._seq:
            return int(self._seq[1])
        return 1

    @property
    def dither_distance(self) -> float:
        if self._seq:
            return float(self._seq[2])
        return 1.0

    def is_enable(self):
        return bool(self._seq)

    def dither_this_exp(self, ex: int) -> bool:
        if ex % self.dither_every_sub == 0:
            return True
        return False
