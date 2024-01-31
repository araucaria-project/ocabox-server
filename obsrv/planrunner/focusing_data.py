import random
from typing import List, Optional
from obsrv.planrunner.errors import PlanBuildError


class FocusingData:
    """
    Class representing focuser parameters
    """

    _FOCUSING_SEQUENCE_LEN = 2

    def __init__(self, seq_foc: str, defocus: int, expositions_quantity: int):
        self._seq = seq_foc.split('/') if seq_foc else []
        self._defocus = defocus
        self._expositions_quantity = expositions_quantity
        self._focus_id: int = FocusingData.generate_focus_id()

    def validate(self):
        """

        raise PlanBuildError
        :return:
        """
        if self._seq:
            if len(self._seq) != self._FOCUSING_SEQUENCE_LEN:
                raise PlanBuildError(f"The length of the focusing sequence is wrong, should "
                                     f"be {self._FOCUSING_SEQUENCE_LEN}")
            try:
                int(self._seq[0])
            except ValueError:
                raise PlanBuildError(f"Wrong first value in focus sequence, can't cast to int")

            try:
                int(self._seq[1])
            except ValueError:
                raise PlanBuildError(f"Wrong second value in focus sequence, can't cast to int")

    @property
    def focus_id(self) -> int:
        return self._focus_id

    @staticmethod
    def generate_focus_id():
        return random.randrange(0, 100000)

    @property
    def defocus(self):
        return int(self._defocus)

    @property
    def focus_target(self) -> Optional[int]:
        return int(self._seq[0]) if self._seq else None

    @property
    def focus_step(self) -> Optional[int]:
        return int(self._seq[1]) if self._seq else None

    def focus_position(self, exp_no: int) -> Optional[int]:
        fvl = self.focus_values_list
        if fvl and len(fvl) > exp_no >= 0:
            return fvl[exp_no]
        else:
            return None

    @property
    def focus_min(self) -> Optional[int]:
        if self.focus_target is not None and self.focus_step is not None:  # can be 0 and it is OK
            return int(round(self.focus_target - (self.focus_step * ((self._expositions_quantity - 1) / 2))))
        return None

    @property
    def focus_values_list(self) -> List[int]:
        fm = self.focus_min
        fs = self.focus_step
        f = []
        if fs is None or fm is None:
            return []
        for n in range(0, self._expositions_quantity):
            f.append(fm + (fs * n))
        return f
