from typing import Optional, Union

from obsrv.planrunner.errors import PlanBuildError


class SequenceData:
    """
    Class representing camera single sequence parameters
    """
    _SEQUENCE_LEN = 3

    def __init__(self, seq: str):
        self._seq = seq.split('/') if seq else []

    def validate(self):
        """

        raise PlanBuildError
        :return:
        """
        # no seq param
        if len(self._seq) == 0:
            self._seq = [0, None, 0]  # set default empty values

        if len(self._seq) != self._SEQUENCE_LEN:
            raise PlanBuildError(f"The length of the sequence is wrong, should "
                                 f"be {self._SEQUENCE_LEN}")
        try:
            int(self._seq[0])
        except ValueError:
            raise PlanBuildError(f"Wrong first value in sequence, can't cast to int")

        if int(self._seq[0]) <= 0:
            raise PlanBuildError(f"Wrong first value in sequence, can't be 0 or lover")

        if self._seq[2] != 'a':
            try:
                float(self._seq[2])
            except ValueError:
                raise PlanBuildError(f"Wrong third value in sequence, can't cast to float")

    @property
    def exp_quantity(self) -> int:
        return int(self._seq[0])

    @property
    def filter(self) -> Optional[Union[str, int]]:
        return self._seq[1]

    @property
    def exp_time(self) -> float:
        if self._seq[2] == 'a':
            return 0.0
        return float(self._seq[2])

    @property
    def auto_exposure(self) -> bool:
        if self._seq[2] == 'a':
            return True
        else:
            return False
