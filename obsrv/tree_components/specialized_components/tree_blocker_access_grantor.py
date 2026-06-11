import logging
import time
from typing import Awaitable, Callable, ClassVar, List, Optional

from obcom.data_colection.address import AddressError
from obsrv.tree_components.base_components.tree_provider import TreeProvider
from obcom.data_colection.coded_error import TreeOtherError
from obsrv.tree_components.specialized_components.tree_base_request_blocker import TreeBaseRequestBlocker, \
    ReservationError
from obcom.data_colection.value import Value
from obcom.data_colection.value_call import ValueRequest

logger = logging.getLogger(__name__.rsplit('.')[-1])


class TreeBlockerAccessGrantor(TreeProvider):
    """
    This module is responsible for reserving access to the locking module for users. The module has several defined
    address commands:
        - take_control - this command reserves access for the user, is possible to give specific timeout in request
            data, use 'timeout_reservation' parameter
        - return_control - this command cancel reserves access for the user
        - break_control - this command cancel control from the current user, every one can send this
        - current_user - this command return current user witch one is currently accessing
        - timeout_current_control - this command return timeout for currently accessing user
        - is_access - this method checks if the requesting user has control and return True if so
        - engage_safety_cutoff - engage the safety cutoff switch blocking dangerous commands
        - disengage_safety_cutoff - disengage the safety cutoff switch restoring normal operation
        - safety_cutoff_state - return the current safety cutoff state and list of blocked commands
    """

    COMPONENT_DEFAULT_NAME: str = 'TreeBlockerAccessGrantor'

    PUSH_DRIVEN: ClassVar[bool] = True

    def __init__(self, component_name: str, source_name: str, target_blocker: TreeBaseRequestBlocker, **kwargs):
        super().__init__(component_name=component_name, source_name=source_name, subcontractor=None, **kwargs)
        self._target_blocker: TreeBaseRequestBlocker = target_blocker
        self._unsubs: List[Callable[[], None]] = []

    def set_change_notifier(self, notify: Callable[[], Awaitable[None]]) -> None:
        # Drop any prior wiring (idempotent rewire).
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        super().set_change_notifier(notify)

        async def _on_change(_v: bool) -> None:
            await notify()

        self._unsubs.append(self._target_blocker.subscribe_safety_cutoff_change(_on_change))

    async def get_value(self, request: ValueRequest, **kwargs) -> Value or None:
        user = request.user
        request_type = request.request_type
        timeout_reservation = request.request_data.get('timeout_reservation', None)
        # do nothing if request hasn't user
        if user is None:
            raise TreeOtherError(code=4001, message='No user in request')

        try:
            command = request.address[request.index]
        except IndexError:
            raise AddressError(code=1001, message='The address does not contain a command.')

        if command == 'take_control' and request_type == 'PUT':
            try:
                self._target_blocker.make_reservation(user=user, timeout_reservation=timeout_reservation)
                logger.info(f"The user: {user} successfully take control of the blocker.")
                return Value(v=True, ts=time.time())
            except ReservationError:
                logger.info(f"The user: {user} failed to take control of the blocker. Blocker is already in use.")
                return Value(v=False, ts=time.time())

        if command == 'break_control' and request_type == 'PUT':
            current_user = self._target_blocker.get_current_user()
            if current_user is None:
                logger.info(f"The user: {user} tried to take control of the blocker, but no one had it.")
                return Value(v=True, ts=time.time())
            else:
                logger.info(f"The user: {user} cancel control of the blocker for current user {current_user}.")
                self._target_blocker.cancel_reservation()
                return Value(v=True, ts=time.time())

        if command == 'return_control' and request_type == 'PUT':
            current_user = self._target_blocker.get_current_user()
            if current_user is None or current_user == user:
                logger.info(f"The user: {user} successfully return control of the blocker.")
                self._target_blocker.cancel_reservation()
                return Value(v=True, ts=time.time())
            logger.info(f"The user: {user} failed return control of the blocker.")
            return Value(v=False, ts=time.time())

        if command == 'current_user':
            # this must be first before get user otherwise may hit the moment when the user expires
            timeout_control = self._target_blocker.get_timeout_current_reservation()
            current_user = self._target_blocker.get_current_user()
            out = {'name': None,
                   'login_date': None,
                   'timeout_control': None}
            if current_user is not None:
                out['name'] = current_user.name
                out['login_date'] = current_user.login_date
                out['timeout_control'] = timeout_control
            return Value(v=out, ts=time.time())

        if command == 'timeout_current_control':
            # this must be first before get user otherwise may hit the moment when the user expires
            timeout_control = self._target_blocker.get_timeout_current_reservation()
            return Value(v=timeout_control, ts=time.time())

        if command == 'is_access':
            current_user = self._target_blocker.get_current_user()
            if current_user is not None and current_user == user:
                return Value(v=True, ts=time.time())
            else:
                return Value(v=False, ts=time.time())

        if command == 'engage_safety_cutoff' and request_type == 'PUT':
            self._target_blocker.engage_safety_cutoff()
            logger.info(f"The user: {user} engaged the safety cutoff.")
            return Value(v=True, ts=time.time())

        if command == 'disengage_safety_cutoff' and request_type == 'PUT':
            self._target_blocker.disengage_safety_cutoff()
            logger.info(f"The user: {user} disengaged the safety cutoff.")
            return Value(v=True, ts=time.time())

        if command == 'safety_cutoff_state':
            out = {
                'engaged': self._target_blocker.is_safety_cutoff_engaged(),
                'blocked_commands': self._target_blocker.get_safety_cutoff_list(),
            }
            return Value(v=out, ts=time.time())

        raise AddressError(code=1002, message=f'Unrecognised method for module {self.get_name()}',
                           severity=AddressError.SEVERITY_CRITICAL)

    def get_publishable_config(self) -> Optional[dict]:
        source_name = self.get_source_name()
        return {
            "role": "service",
            "key": source_name,
            "address": source_name,
            "type": type(self).__name__,
        }
