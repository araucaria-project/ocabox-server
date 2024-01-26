import logging
from collections import deque
from typing import Deque

logger = logging.getLogger(__name__.rsplit('.')[-1])


class MessageIdDeque:
    _DEFAULT_MIN_SIZE = 5

    def __init__(self, min_size=_DEFAULT_MIN_SIZE, **kwargs):
        super().__init__(**kwargs)
        if min_size == 0:
            logger.warning(f"The minimum size of free ids is 0 so this can lead to lower efficiency,")
        elif min_size > 0:
            pass
        else:
            logger.warning(f"Invalid minimum queue size. The default value of {self._DEFAULT_MIN_SIZE} will be set.")
            min_size = self._DEFAULT_MIN_SIZE
        self._min_size = min_size
        self._size: int = 0
        self._free_id: Deque = deque()
        self._initialize_deque()

    def _initialize_deque(self):
        for i in range(self._min_size):
            self._free_id.append(i)
            self._size += 1

    def _add_new_id(self):
        """
        This method add new id to pool of free ids

        :return: None
        """
        self._free_id.append(self._size)
        self._size += 1

    def _get_new_id(self) -> int:
        """
        This method create new id and return it. The id pool will be increased by this address when freed.

        :return: new unique id
        """
        n = self._size
        self._size += 1
        return n

    def get_id(self) -> int:
        """
        Method return one id from list of free ids and remove it from this list. If list is empty, than create
        increases id pools

        :return: unique id
        """
        if self._free_id:
            return self._free_id.popleft()
        else:
            return self._get_new_id()

    def release_id(self, id_):
        """
        This method add again id to list of free ids. This id can be used again later.

        :param id_: id to release
        :return: None
        """
        if self._size > id_ >= 0:
            if self._is_above_state(id_):
                self._size -= 1
            else:
                self._free_id.append(id_)

    def _is_above_state(self, id_):
        if id_ == self._size - 1 and self._size > self._min_size:
            return True
        return False
