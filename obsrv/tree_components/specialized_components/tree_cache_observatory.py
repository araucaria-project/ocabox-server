import asyncio
import re
import time
from dataclasses import dataclass
import logging
from asyncio import Task
from typing import Dict, Optional
from obcom.data_colection.address import Address
from obcom.data_colection.response_error import ResponseError
from obsrv.tree_components.base_components.tree_base_provider import TreeBaseProvider
from obsrv.tree_components.base_components.tree_component import ProvidesResponseProtocol
from obcom.data_colection.coded_error import TreeStructureError, TreeOtherError
from obsrv.tree_components.specialized_components.tree_cache_observatory_protocols import KnownValueProtocol
from obsrv.tree_components.specialized_components.tree_conditional_freezer_protocol import TreeConditionalFreezerProtocol
from obcom.data_colection.value import Value, TreeValueError
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
        # Keyed by str(address): lookups are hot (every GET) and a linear list
        # scan with Address.__eq__ showed up in production profiles.
        self._known_values: Dict[str, TreeCache._KnownValue] = {}
        self._max_recall = 1  # After how many times he waits for the previous task, he asks yourself.
        if self._max_recall < 1:
            logger.warning(f"The _max_recall value is lover than one. It is unacceptable so will be set to 1 !")
            self._max_recall = 1
        self._conditional_freezer: TreeConditionalFreezerProtocol or None = None
        # self._no_cachable_address = []
        self._no_cachable_regex = []
        self._load_no_cachable_address()
        # Negative caching (rollout flag, default off): remember a *failure* from
        # the subcontractor and serve it back (fail-fast, same error code) until
        # its TTL passes, instead of letting every request penetrate to a dead
        # device. TTL escalates per consecutive failure and resets on success.
        # See the TIC Hardening 2026-08-29 register.
        neg_cfg = self._get_cfg("negative_cache", None) or {}
        self._neg_enabled: bool = bool(neg_cfg.get('enabled', False))
        self._neg_ttl_initial: float = float(neg_cfg.get('ttl_initial', 1.0))
        self._neg_ttl_max: float = float(neg_cfg.get('ttl_max', 10.0))
        # Only device/transport-shaped failures are negative-cachable. Permanent
        # config errors (1xxx, 3002) and per-user denials must never be cached.
        self._neg_codes = set(neg_cfg.get('codes', [2002, 2003, 4002, 4005, 4009]))

    @dataclass
    class _KnownValue:
        address: Address
        value: Value or None
        task: Task or None
        change_time: float
        # negative-cache state (active when neg_error is not None and
        # time.monotonic() < neg_until — monotonic, immune to clock steps)
        neg_error: Optional[ResponseError] = None  # last ResponseError from the subcontractor
        neg_until: float = 0.0  # time.monotonic() deadline
        fail_count: int = 0
        # True while the last subcontractor answer for this address was an
        # error. Independent of the negative-cache flag: it drives the
        # recovery-as-change semantics below, not failure caching.
        had_failure: bool = False

        def get_change_time(self) -> float:
            return self.change_time

        def get_timestamp(self) -> float or None:
            if self.value:
                return self.value.ts
            return None

        def get_value(self) -> Value:
            return self.value

    def _add_known_value(self, kv: '_KnownValue') -> '_KnownValue':
        """Insert a _KnownValue under its address key and return it."""
        self._known_values[str(kv.address)] = kv
        return kv

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
            known_value = self._add_known_value(
                self._KnownValue(address=address, value=None, task=None, change_time=0))
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
        if self._neg_enabled and known_value.neg_error is not None:
            remaining = known_value.neg_until - time.monotonic()
            if remaining > 0:
                # The source failed fail_count times in a row and its negative TTL
                # has not passed — answer with the remembered error immediately
                # instead of queuing more device I/O behind a known failure.
                # `from_negative_cache` marks this as an echo of an already-counted
                # failure: the freezer must not treat it as new device evidence.
                ne = known_value.neg_error
                message = (f"{ne.message} [negative-cache: {known_value.fail_count} consecutive "
                           f"failures, next probe in {remaining:.1f}s]")
                exc_cls = TreeValueError if 2000 <= ne.code < 3000 else TreeOtherError
                raise exc_cls(code=ne.code, message=message, severity=ne.severity,
                              from_negative_cache=True)
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
            logger.error(f'Can not find current value among cached known values and should be')
        # Staleness Contract: the first successful refresh after a failure
        # episode is a *change* even when the payload is equal — during the
        # outage the value was unknown, so "unknown → v" must wake conditional
        # subscribers (it also refreshes their timestamp after a 2.6+ freezer
        # delivered a stale-None).
        recovered = kv is not None and kv.had_failure and result.status
        if kv is not None:
            kv.had_failure = not result.status
        await self._update_known_value(result.address, result.value, kv, force_change=recovered)
        if self._neg_enabled and kv is not None:
            if result.status:
                if kv.fail_count:
                    logger.info(f'{result.address}: source recovered after {kv.fail_count} '
                                f'consecutive failures, negative cache cleared')
                kv.neg_error = None
                kv.neg_until = 0.0
                kv.fail_count = 0
            elif result.error is not None and result.error.code in self._neg_codes:
                kv.fail_count += 1
                # cap the exponent: 2**large overflows float and fail_count grows
                # unbounded during a long outage (one probe per ttl_max)
                exponent = min(kv.fail_count - 1, 16)
                ttl = min(self._neg_ttl_initial * (2 ** exponent), self._neg_ttl_max)
                kv.neg_until = time.monotonic() + ttl
                kv.neg_error = result.error
                if kv.fail_count == 1:
                    logger.info(f'{result.address}: failure cached (code {result.error.code}), '
                                f'serving it for {ttl:.1f}s before next probe')
        self._remove_the_value_lock(result.address, kv)

    def _find_in_known_values(self, address: Address) -> _KnownValue or None:
        """
        This method check if value for given address exists in known values and return it.

        :param address: Address
        :return: object representing stored value for given address or None if not exists
        """
        return self._known_values.get(str(address))

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

    async def _update_known_value(self, address: Address, value: Value, known_value: _KnownValue = None,
                                  force_change: bool = False):
        """
        This method update known values.

        :param address: Address
        :param value: Value
        :param known_value: _KnownValue object. It is optional, it can be specified to limit the amount of searching a
            list of known values
        :param force_change: treat this update as a value change even when the
            payload is unchanged (recovery after a failure episode)
        :return: None
        """
        # warning value can be None if response got error so check this first
        if value:
            kv = known_value if known_value else self._find_in_known_values(address)
            # if value isn't on list yet
            if not kv:
                kv = self._KnownValue(address=address, value=value, task=None, change_time=value.ts)  # first initial
                self._add_known_value(kv)
                return
            # if new provided data is earlier than the date currently stored in list
            if not kv.value:
                # initial know value after create it
                kv.value = value
                kv.change_time = value.ts
            else:
                if kv.value.ts < value.ts:
                    if force_change or self._is_changed(new_v=value, old_v=kv.value):
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
