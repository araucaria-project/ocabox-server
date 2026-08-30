import asyncio
import time
import logging
from typing import Optional

from obcom.data_colection.address import Address, AddressError
from obsrv.tree_components.base_components.tree_base_provider import TreeBaseProvider
from obcom.data_colection.coded_error import TreeStructureError, TreeOtherError
from obcom.data_colection.response_error import ResponseError
from obsrv.tree_components.specialized_components.tree_cache_observatory_protocols import TreeCacheProtocol, \
    KnownValueProtocol
from obcom.data_colection.value import Value, TreeValueError
from obcom.data_colection.value_call import ValueRequest
from obsrv.utils.asyncio_util_functions import wait_for_psce

logger = logging.getLogger(__name__.rsplit('.')[-1])


# Cyclic-query bookkeeping fields stamped into `request.request_data` by
# the client (obcom.cycle_query) and consumed by the freezer below. They
# are tree-internal state and must NOT be forwarded to protocol connectors
# — at least one ASCOM driver in production (jk15-tcu) rejects unknown URL
# params with HTTP 400, which then permanently freezes the cache at the
# last successful value (e.g. camera.state stuck at EXPOSING for hours).
TREE_INTERNAL_REQUEST_FIELDS = frozenset({
    'time_of_known_change',
    'no_send_before',
    'nr_of_unsuccessful_refreshes',
    # Reserved for the Staleness Contract (ocabox-common#8, phase 0/#39):
    # future clients send their value policy here; servers of this line strip
    # it so it can never leak to strict ASCOM drivers (the 2.3.12 bug class).
    'value_policy',
})


def strip_tree_internal_fields(request_arguments: dict) -> dict:
    """Return request_arguments with cyclic-query bookkeeping fields removed.

    No-op (returns the same dict) when nothing leaks; otherwise returns a
    fresh dict so the caller's input is not mutated.
    """
    if not request_arguments:
        return request_arguments
    if not TREE_INTERNAL_REQUEST_FIELDS.intersection(request_arguments):
        return request_arguments
    return {k: v for k, v in request_arguments.items() if k not in TREE_INTERNAL_REQUEST_FIELDS}


# todo zapytać  o podwojne zapytania do rutera, jedna wartość może się zmienić szybciej nisz druga i co wtedy?
#  Zdajemy się na inteligęcje urzytkownika? może niech będzie taka opcja ale w client_API się to uniemożliwi?


class TreeConditionalFreezer(TreeBaseProvider):
    COMPONENT_DEFAULT_NAME: str = 'TreeConditionalFreezer'

    def __init__(self, component_name: str, subcontractor: TreeCacheProtocol = None, **kwargs):
        super().__init__(component_name=component_name, subcontractor=subcontractor, **kwargs)
        self._subcontractor: TreeCacheProtocol = subcontractor
        subcontractor.set_conditional_freezer(cf=self)
        self._condition_change_data: Optional[asyncio.Condition] = None
        # How many times it will keep trying to update the value with one can not be updated before returning an error
        self._max_unsuccessful_refreshes: int = self._get_cfg('max_unsuccessful_refreshes')
        # how many seconds before timeout expires component is to return an empty message to the clone
        self._alarm_timeout_offset: float = self._get_cfg('alarm_timeout')
        self._min_time_of_data_tolerance = self._get_cfg('min_time_of_data_tolerance')

    def set_max_refreshes(self, max_: int):
        self._max_unsuccessful_refreshes = max_

    async def get_value(self, request: ValueRequest, **kwargs) -> Value or None:
        if not request.cycle_query:
            raise TreeStructureError  # this is no subscribe request so push is forward

        # have to be connected witch cache
        if not self._subcontractor:
            raise TreeStructureError

        # check request is cachable
        if not self._subcontractor.is_cachable_request(request=request):
            logger.info(f'Retrieve a cycle query for a non-cacheable value {request.address}')
            # Permanent under current server config — no_cachable_regex is config, not state.
            raise TreeOtherError(code=4003, severity=ResponseError.SEVERITY_CRITICAL)

        time_of_known_change = request.request_data.get('time_of_known_change', None)
        # Staleness Contract (ocabox-common#8, phase 2): a client that declares
        # its value policy is governed by the tolerance clock instead of the
        # failure counter. Unknown values are treated as undeclared so an old
        # server line never breaks a newer client outright.
        value_policy = request.request_data.get('value_policy', None)
        if value_policy is not None and value_policy not in ('none', 'raise', 'last_good'):
            logger.warning(f"Unknown value_policy '{value_policy}' for {request.address} — ignoring")
            value_policy = None
        t_tolerance = request.time_of_data_tolerance
        if t_tolerance < self._min_time_of_data_tolerance:
            logger.warning(f"the time_of_data_tolerance for the request {request.address} is too short. "
                           f"Should be greater than {self._min_time_of_data_tolerance} (now is {t_tolerance})")
            t_tolerance = self._min_time_of_data_tolerance
        # Temporal model of the Staleness Contract: T0 = delay/no_send_before
        # (rate cap, handled by _delayer), T1 = time_of_data_tolerance (healthy
        # refresh cadence: "fresher than T1 — serve it, older — please
        # refresh"), T2 = time_of_data_max_age ("older than T2 must not be
        # presented"). The healthy system runs on T1 exactly as it always has;
        # (T1, T2] is the repair window — failures are masked and retried on a
        # quick cadence there; a stale-None/error is due only past T2.
        max_age = request.time_of_data_max_age
        if value_policy is not None:
            if max_age is None:
                # old/new client mix or hand-built request: same default the
                # client library applies — one full missed refresh cycle
                max_age = 2 * t_tolerance
            elif max_age < t_tolerance:
                logger.warning(f"time_of_data_max_age ({max_age}) below the effective tolerance "
                               f"({t_tolerance}) for {request.address} — clamping; the server cannot "
                               f"guarantee freshness finer than its polling resolution")
                max_age = t_tolerance
            # Quick repairs inside the window, floored by the server's polling
            # resolution (device protection): when the window is narrower than
            # 4x the floor, fewer repair attempts fit — by design.
            retry_offset = max(self._min_time_of_data_tolerance, (max_age - t_tolerance) / 4)
        else:
            max_age = None
            retry_offset = t_tolerance
        wait_offset_error: float = 0
        try:
            nr_of_unsuccessful_refreshes: int = int(request.request_data.get('nr_of_unsuccessful_refreshes', 0))
        except Exception as e:
            raise AddressError(code=1003)
        timeout = request.request_timeout
        # sanity check but probably never happened
        if not isinstance(timeout, float):
            raise TreeOtherError(code=4001, message='Wrong type timeout in request')
        waiting_timeout = timeout - self._alarm_timeout_offset
        wait_to = request.request_data.get('no_send_before', 0)

        # wait some before doing anything, no send message to fast
        # At this level, we do not check if there is a timeout because we assume that the client has configured the
        # query correctly. If not then he will get a no answer error
        await self._delayer(wait_to=wait_to, waiting_timeout=waiting_timeout)
        k_value: KnownValueProtocol or None = None
        highest_update_error_severity = None

        while True:
            # trying to initialize the k_value first
            if k_value is None:
                try:
                    k_value: KnownValueProtocol or None = self._get_value_from_cache(request.address)
                except ValueError:
                    raise TreeValueError(code=2002, message='Cache for this request is not response.')

            # k_value is corrupted. Behave as if no value has been get - try refresh and get again
            if k_value is not None and k_value.get_value() is None:
                k_value = None

            # whether k_value is ready to be sent
            if k_value is not None and (
                    time_of_known_change is None or time_of_known_change < k_value.get_change_time()):
                if (value_policy is None or value_policy == 'last_good'
                        or not self._breaks_max_age(k_value, max_age)):
                    # last_good is exempt from the T2 gate by definition: that
                    # client wants the aging value, however old (a fresh
                    # last_good subscriber must still receive the cache).
                    returned_value = k_value.get_value().copy()
                    returned_value.tags['from_cf'] = True  # Add a tag to the value that it comes from ConditionalFreezer
                    return returned_value
                # Declared policy and the cached value is already older than the
                # client's T2 — sending it would be a lie. Fall through to the
                # refresh loop, which decides value vs stale-None vs error.

            # whether the number of re-refreshes has been exceeded
            # (historical counter path — declared-policy requests are governed
            # by the tolerance clock in _stale_verdict instead)
            if value_policy is None and nr_of_unsuccessful_refreshes >= self._max_unsuccessful_refreshes:
                logger.info(f'Too many failed attempts to refresh a value {request.address}')
                # ``severity=None`` resolves to ``SEVERITY_NORMAL`` via the ResponseError
                # constructor — used when no underlying connector error supplied a
                # severity (e.g. a connector that swallowed the exception and returned
                # None). Connectors MUST surface real errors with explicit severity so
                # this fallback is reached only for legitimate "no signal at all" cases.
                raise TreeValueError(code=2003, severity=highest_update_error_severity)

            await asyncio.sleep(0)  # let other tasks do work
            # waiting logic
            result_waiter = await self._waiter(k_value=k_value,
                                               t_tolerance=t_tolerance,
                                               waiting_timeout=waiting_timeout,
                                               min_wait=wait_offset_error)  # can raise TreeValueError
            # if event was call - that mean some other task refreshes value
            if result_waiter:
                continue  # continue because event was call so is not necessary to update value again

            # check is time to return anything because timeout is coming
            await self._expire_checker(waiting_timeout, nr_of_unsuccessful_refreshes)  # can raise TreeOtherError

            # update value
            try:
                logger.debug(f"Update value ({request.address})")
                status_update, err = await wait_for_psce(self._update_value(request), waiting_timeout - time.time())
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                logger.debug(f"A timeout occurred while waiting for a value update")
                await self._expire_checker(waiting_timeout, nr_of_unsuccessful_refreshes)  # can raise TreeOtherError
                logger.error(f"An attempt to refresh the content was interrupted by timeout but the timeout condition "
                             f"was not met. Timeout calculated incorrectly. Address: {request.address}")
                raise TreeOtherError(code=4006,
                                     message=f"An attempt to refresh the content was interrupted by timeout but the "
                                             f"timeout condition was not met. Timeout calculated incorrectly. "
                                             f"Address: {request.address}",
                                     severity=TreeOtherError.SEVERITY_CRITICAL)
            if status_update:
                wait_offset_error = 0
                nr_of_unsuccessful_refreshes = 0
                highest_update_error_severity = None
            else:
                logger.info(f'Can not update value in cache: {request.address}')
                wait_offset_error = retry_offset
                if value_policy is not None and k_value is not None:
                    last_ts = k_value.get_timestamp()
                    if last_ts is not None:
                        remaining = last_ts + max_age - time.time()
                        if 0 < remaining <= retry_offset:
                            # land the final probe just past T2 — the stale
                            # verdict must be punctual, not late by a retry step
                            wait_offset_error = remaining + 0.01
                if err is not None and err.kwargs.get('from_negative_cache'):
                    # Echo of an already-counted failure served from TreeCache's
                    # negative cache — pacing applies, but it is not new device
                    # evidence, so it must not advance the failure counter
                    # (otherwise fail-fast errors would race the counter to 2003
                    # during blips that today ride through unnoticed).
                    pass
                else:
                    nr_of_unsuccessful_refreshes += 1
                if err is not None:
                    if highest_update_error_severity is None or \
                            ResponseError.compare_severity(err.severity, highest_update_error_severity):
                        highest_update_error_severity = err.severity
                if value_policy is not None:
                    verdict = self._stale_verdict(request=request, k_value=k_value,
                                                  max_age=max_age, value_policy=value_policy,
                                                  err=err, time_of_known_change=time_of_known_change,
                                                  highest_severity=highest_update_error_severity)
                    if verdict is not None:
                        return verdict

    @staticmethod
    def _breaks_max_age(k_value: KnownValueProtocol, max_age: float) -> bool:
        """True when the cached value is older than the client's T2 bound."""
        ts = k_value.get_timestamp()
        return ts is None or (time.time() - ts) > max_age

    def _stale_verdict(self, request: ValueRequest, k_value: KnownValueProtocol or None,
                       max_age: float, value_policy: str, err: ResponseError or None,
                       time_of_known_change: float or None,
                       highest_severity: str or None) -> Value or None:
        """Truth-axis decision after a failed refresh (Staleness Contract).

        Masking a failure is allowed only while the cached value is younger
        than the client's T2 (``time_of_data_max_age``) — the repair window
        between the preferred freshness T1 and the truth bound T2. Past T2:

        * ``none``      → deliver a rich ``Value(None, ts=now, tags={reason,
          last_good, last_good_ts})`` as a value change — at most once per
          staleness episode (dedup below);
        * ``raise``     → the failure surfaces immediately as 2003;
        * ``last_good`` → keep masking, the client wants the aging value.

        CRITICAL failures are permanent conditions: they surface immediately
        for every declared policy, tolerance notwithstanding.

        Returns a Value to deliver, None to keep nursing, or raises.

        Dedup is stateless: the client echoes the delivered None's ts as
        ``time_of_known_change``, which is then *newer* than the cache's
        frozen refresh timestamp — that inversion means "this client already
        knows". Recovery is delivered because TreeCache bumps ``change_time``
        on the first successful refresh after a failure episode.
        """
        if err is not None and err.severity == ResponseError.SEVERITY_CRITICAL:
            logger.info(f'{request.address}: permanent failure (code {err.code}) — '
                        f'surfacing immediately (value_policy={value_policy})')
            raise TreeValueError(code=2003,
                                 message=f'Refresh failed permanently (underlying code {err.code}: {err.message})',
                                 severity=ResponseError.SEVERITY_CRITICAL)
        last_value = k_value.get_value() if k_value is not None else None
        last_ts = k_value.get_timestamp() if k_value is not None else None
        if last_ts is not None and (time.time() - last_ts) <= max_age:
            return None  # still truthful for this client (within T2) — mask and keep nursing
        if value_policy == 'last_good':
            return None
        if value_policy == 'raise':
            raise TreeValueError(code=2003,
                                 message=f'Value stale beyond client max age ({max_age}s); '
                                         f'last failure code {err.code if err else "?"}',
                                 severity=highest_severity)
        # value_policy == 'none'
        if time_of_known_change is not None:
            if last_ts is not None and time_of_known_change > last_ts:
                # The client already saw something newer than our last
                # successful refresh — necessarily this episode's stale-None.
                return None
            if last_ts is None and time_of_known_change > time.time() - max_age:
                # Cache holds no value at all (server restarted mid-outage):
                # nothing to anchor dedup to, so a None the client saw within
                # the current tolerance window counts as "already knows". A
                # client whose knowledge is older (a pre-restart real value)
                # is owed the None. Worst case this re-delivers one None per
                # tolerance period until the device recovers.
                return None
        now = time.time()
        tags = {'reason': err.code if err is not None else None, 'from_cf': True}
        if last_value is not None:
            tags['last_good'] = last_value.v
            tags['last_good_ts'] = last_value.ts
        logger.info(f'{request.address}: max data age ({max_age}s) exceeded — '
                    f'delivering stale-None (reason={tags["reason"]})')
        return Value(None, ts=now, tags=tags)

    async def _expire_checker(self, waiting_timeout, nr_of_unsuccessful_refreshes):
        """
        The method checks if the timeout is about to expire and throws an error if so.
        :param waiting_timeout: timeout with buffer
        :param nr_of_unsuccessful_refreshes:

        :raise TreeOtherError:
        """
        if waiting_timeout - time.time() <= 0:
            # response timeout so stop refreshing value and send empty message
            # Send subscription details so that it can be reopened when will be requested again
            raise TreeOtherError(code=4004, nr_of_unsuccessful_refreshes=nr_of_unsuccessful_refreshes)

    async def _waiter(self, k_value: KnownValueProtocol or None, t_tolerance: float, waiting_timeout: float,
                      min_wait: float = 0) -> bool:
        """
        This method is used to wait for the value in the cache or time to change before the specified expiration
        date will expire

        :param k_value: Object of known value from cache block
        :param t_tolerance: the allowable difference between the present moment and timestamp of the value
        :param waiting_timeout: timeout before which an empty message must be sent to the client
        :param min_wait: minimum wait time
        :raise TreeValueError: When can not find _condition_change_data
        :return: True if waiter finish by event call
        """
        while True:
            current_time = time.time()
            if k_value is None or k_value.get_timestamp() is None:
                waiting_time = 0
            else:
                waiting_time = k_value.get_timestamp() + t_tolerance - current_time
            # check and set min waiting time
            if min_wait != 0:
                if waiting_time < min_wait:
                    waiting_time = min_wait
                min_wait = 0  # Restore 0 because the minimum expectation can only be the first time the loop passes
            if waiting_time <= 0:
                return False  # it's time to update value - return False because it is not event call
            # Compare waiting_time to timeout before waiting
            time_to_timeout = waiting_timeout - current_time
            if time_to_timeout <= 0:  # sanity check
                return False
            if time_to_timeout < waiting_time:
                waiting_time = time_to_timeout
            condition = self._condition_change_data
            if condition is None:
                raise TreeValueError(code=2002, message="For Unknown reasons condition is None",
                                     severity=TreeValueError.SEVERITY_CRITICAL)
            # WARNING Before this method there can be no place (await) where the task will lose focus. If it will be
            # necessary to add such a method, you should get a lock on the condition before (you will need to
            # rebuild the _condition_wait() method). The point is that condition.wait() must follow the wait
            # time calculation
            is_event = await self._condition_wait(condition, waiting_time)
            await asyncio.sleep(0)
            if is_event:  # event call
                return True  # value was changed

    async def _delayer(self, wait_to, waiting_timeout):
        """The method implements the query delay, taking care not to exceed the timeout"""
        current_time = time.time()
        if current_time > wait_to:
            return
        if wait_to < waiting_timeout:
            w = wait_to
        else:
            w = waiting_timeout
        await asyncio.sleep(w - current_time)

    async def _update_value(self, request: ValueRequest) -> (bool, None or ResponseError):
        """
        This method call subcontractor to get response and update cache module.

        :param request: request object
        :raise ValueError: when can not update value
        :return: return True if value was updated
        """

        request.time_of_data = time.time()
        result = await self._subcontractor.get_response(request=request.copy())
        if result.status:
            return True, result.error
        else:
            return False, result.error

    def _get_value_from_cache(self, address: Address) -> None or KnownValueProtocol:
        """
        Method search and return value from cache.

        :param address:
        :raise ValueError: when subcontractor is not defined
        :return: known value from cache
        """
        if not self._subcontractor:
            raise ValueError
        k_value = self._subcontractor.get_k_val(address)
        return k_value

    @staticmethod
    async def _event_wait(evt: asyncio.Event, timeout):
        try:
            await wait_for_psce(evt.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    @staticmethod
    async def _condition_wait(con: asyncio.Condition, timeout):
        try:
            async with con:
                await wait_for_psce(con.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def set_change_event(self):
        """Method set the event"""
        condition = self._condition_change_data
        if condition is not None:
            async with condition:
                condition.notify_all()

    async def run(self):
        if self._condition_change_data is None:
            self._condition_change_data = asyncio.Condition()
        await super().run()

    async def stop(self):
        self._condition_change_data = None
        await super().stop()
