import asyncio
import re
from dataclasses import dataclass
import logging
from asyncio import Task
from typing import List
from obcom.data_colection.address import Address
from obsrv.tree_components.base_components.tree_base_provider import TreeBaseProvider
from obsrv.tree_components.base_components.tree_component import ProvidesResponseProtocol
from obcom.data_colection.coded_error import TreeStructureError
from obsrv.tree_components.specialized_components.tree_cache_observatory_protocols import KnownValueProtocol
from obsrv.tree_components.specialized_components.tree_conditional_freezer_protocol import TreeConditionalFreezerProtocol
from obcom.data_colection.value import Value
from obcom.data_colection.value_call import ValueRequest, ValueResponse

logger = logging.getLogger(__name__.rsplit('.')[-1])


class TreeCache(TreeBaseProvider):
    """
    This class is responsible for responding to request with data contained in the cache of this object.
    New _KnownValue in the list '_known_values' should only be created once at the beginning for each address and only
    edited afterwards, because an object of that value is temporarily stored elsewhere in the code.

    :param component_name: this is name of tree component, used for debug
    :param subcontractor: instance of next component in tree
    """

    COMPONENT_DEFAULT_NAME: str = 'TreeCache'

    def __init__(self, component_name: str, subcontractor: ProvidesResponseProtocol = None, **kwargs):
        super().__init__(component_name=component_name, subcontractor=subcontractor, **kwargs)
        self._known_values: List[TreeCache._KnownValue] = []  # list (address :Address, value: Value, task: Task)
        self._max_recall = 1  # After how many times he waits for the previous task, he asks yourself.
        if self._max_recall < 1:
            logger.warning(f"The _max_recall value is lover than one. It is unacceptable so will be set to 1 !")
            self._max_recall = 1
        self._conditional_freezer: TreeConditionalFreezerProtocol or None = None
        # self._no_cachable_address = []
        self._no_cachable_regex = []
        self._load_no_cachable_address()

    @dataclass
    class _KnownValue:
        address: Address
        value: Value or None
        task: Task or None
        change_time: float

        def get_change_time(self) -> float:
            return self.change_time

        def get_timestamp(self) -> float or None:
            if self.value:
                return self.value.ts
            return None

        def get_value(self) -> Value:
            return self.value

    def _load_no_cachable_address(self):
        # self._no_cachable_address = self._get_cfg("no_cachable_address", [])
        self._no_cachable_regex = self._get_cfg("no_cachable_regex", [])

    async def get_value(self, request: ValueRequest, **kwargs) -> Value or None:
        # docstring is imported from parent
        recall = kwargs.get('recall', 0)
        address = request.address
        # skip cache if request is not cachable all other values should be initialized in cache
        if not self.is_cachable_request(request=request):
            raise TreeStructureError
        known_value = self._find_in_known_values(address)
        if not known_value:
            # Initializing this value even when it cannot be updated later means the request is cachable
            known_value = self._KnownValue(address=address, value=None, task=None, change_time=0)
            self._known_values.append(known_value)
        value = known_value.value if self._value_meets_requirements(known_value, request.time_of_data,
                                                                    request.time_of_data_tolerance) else None
        # found in known values
        if value:
            return value
        else:
            if recall > 0:
                # The newly downloaded value does not meet the requirements
                logger.info(f'Retry retrieving content from the cache but a value was not supplied by the previous '
                            f'task. Nr recall {recall} / {self._max_recall}')
        task = known_value.task
        # not found and no one asks about it
        if not task or task.done():
            known_value.task = None
            known_value.task = asyncio.current_task()
            raise TreeStructureError
        # not found but someone asks about it and waiting for answer
        if task and not task.done():
            if recall < self._max_recall:
                # Here we're waiting for task but by using asyncio.wait(). which makes us wait for the task to
                # finish, but we are not interested in the result or whether it throws an error. Only we wait for end.
                # -----
                # Other way to do this is created Future object and make the task set future to True after end and
                # wait for Future not for task.
                await asyncio.wait([task])
                return await self.get_value(request, recall=recall + 1)
        logger.info(f"stop waiting for other task and try ask by yourself")
        raise TreeStructureError

    def is_cachable_request(self, request: ValueRequest) -> bool:
        if request.request_type != 'GET':
            return False
        # if request.address.__str__() == self._no_cachable_address:
        #     return False
        for r in self._no_cachable_regex:
            if re.match(r, request.address.__str__()):
                return False
        return True

    async def _on_subcontractor_return(self, result: ValueResponse, request: ValueRequest):
        # docstring is imported from parent
        if not self.is_cachable_request(request=request):
            return
        kv = self._find_in_known_values(result.address)
        if not kv:
            logger.error(f'Can not find current value in list cached values and should be')
        await self._update_known_value(result.address, result.value, kv)
        self._remove_the_value_lock(result.address, kv)

    def _find_in_known_values(self, address: Address) -> _KnownValue or None:
        """
        This method check if value for given address exists in known values and return it.

        :param address: Address
        :return: object representing stored value for given address or None if not exists
        """
        # check if the value is in cache
        for kv in self._known_values:
            if kv.address == address:
                return kv
        #  return None if value is not in cache
        return None

    @staticmethod
    def _value_meets_requirements(kv: _KnownValue, ts: float, delta: float):
        """
        Method check if known value meets requirements and can be used to send client.

        :param kv: _KnownValue object
        :param ts: timestamp
        :param delta: allowable delay
        :return: Returns True if the conditions are met and False if not
        """
        #  check that the value meets the requirements
        t_delta = delta
        if not kv.value:
            return False
        return not kv.value.is_expired(ts, t_delta)

    async def _update_known_value(self, address: Address, value: Value, known_value: _KnownValue = None):
        """
        This method update known values.

        :param address: Address
        :param value: Value
        :param known_value: _KnownValue object. It is optional, it can be specified to limit the amount of searching a
            list of known values
        :return: None
        """
        # warning value can be None if response got error so check this first
        if value:
            kv = known_value if known_value else self._find_in_known_values(address)
            # if value isn't on list yet
            if not kv:
                kv = self._KnownValue(address=address, value=value, task=None, change_time=value.ts)  # first initial
                self._known_values.append(kv)
                return
            # if new provided data is earlier than the date currently stored in list
            if not kv.value:
                # initial know value after create it
                kv.value = value
                kv.change_time = value.ts
            else:
                if kv.value.ts < value.ts:
                    if self._is_changed(new_v=value, old_v=kv.value):
                        kv.change_time = value.ts
                        await self._report_new_value()  # report that there is new value if conditional_freezer is known
                    kv.value = value

    def _remove_the_value_lock(self, address, known_value: _KnownValue = None):
        kv = known_value if known_value else self._find_in_known_values(address)
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            logger.warning(f'Can not find current task for request')
            return
        if not kv:
            return
        if kv.task == current_task:  # if not that mean current task no wait and ask by yourself
            kv.task = None

    @staticmethod
    def _is_changed(new_v, old_v):
        # compare value
        if new_v == old_v:
            return False
        return True

    def set_conditional_freezer(self, cf: TreeConditionalFreezerProtocol):
        self._conditional_freezer = cf

    def remove_conditional_freezer(self):
        self._conditional_freezer = None

    async def _report_new_value(self):
        if self._conditional_freezer is not None:
            await self._conditional_freezer.set_change_event()

    def get_k_val(self, address: Address) -> KnownValueProtocol or None:
        return self._find_in_known_values(address=address)
