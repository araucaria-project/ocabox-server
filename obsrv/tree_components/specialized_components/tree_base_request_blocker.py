
import logging
import time
from typing import Dict, List

from obcom.data_colection.address import AddressError
from obsrv.tree_components.base_components.tree_base_provider import TreeBaseProvider
from obsrv.tree_components.base_components.tree_component import ProvidesResponseProtocol
from obcom.data_colection.coded_error import TreeStructureError, TreeOtherError
from obcom.data_colection.tree_user import BaseTreeUser, TreeServiceUser
from obcom.data_colection.value import Value
from obcom.data_colection.value_call import ValueRequest

logger = logging.getLogger(__name__.rsplit('.')[-1])


class TreeBaseRequestBlocker(TreeBaseProvider):
    """
    This is a module for filtering and blocking some type incoming requests. In order to unlock access, access must
    be reserved by the user for a certain period of time. Reservation is done using a different module.

    Additionally provides a safety cutoff switch that blocks dangerous commands (movement, dome, mirror covers)
    when engaged. The cutoff cannot be bypassed by the special permission parameter or white lists — only by
    the dedicated safety cutoff bypass parameter intended for manual control devices operated inside the dome.
    """

    COMPONENT_DEFAULT_NAME: str = 'TreeBaseRequestBlocker'
    SPECIAL_PERMISSION_PARAM: str = "request_special_permission_param"  # use only for requesting inside ocabox
    SAFETY_CUTOFF_BYPASS_PARAM: str = "request_safety_cutoff_bypass_param"  # for manual dome controllers

    def __init__(self, component_name: str, subcontractor: ProvidesResponseProtocol = None, **kwargs):
        super().__init__(component_name=component_name, subcontractor=subcontractor, **kwargs)
        self._current_user: BaseTreeUser or None = None
        self._timeout_reservation: float = 0
        self._black_lists: Dict[str, list] = {'GET': [],
                                              'PUT': []}
        self._white_lists: Dict[str, list] = {'GET': [],
                                              'PUT': []}
        self._safety_cutoff_engaged: bool = False
        self._safety_cutoff_list: List[str] = []
        self._init_lists_of_special_requests()
        self._init_safety_cutoff_list()

    async def get_value(self, request: ValueRequest, **kwargs) -> Value or None:
        # docstring is importing from parent
        # stop all request from black list
        if self._check_black_list(request=request):
            raise AddressError(code=1004)

        # GET always can go ahead
        if request.request_type == 'GET':
            raise TreeStructureError

        if request.request_type != 'PUT':
            raise TreeOtherError(code=4001, message='Unrecognized request type')

        # safety cutoff — blocks dangerous commands when engaged, only bypassable by dedicated param
        if self._safety_cutoff_engaged and self._is_cutoff_blocked(request=request):
            if not self._check_has_safety_cutoff_bypass(request=request):
                command = '.'.join(request.address[request.index:])
                raise AddressError(code=1005,
                                   message=f'Safety cutoff is engaged — command \'{command}\' blocked for dome entry safety')

        # white list always can go ahead
        if self._check_white_list(request=request):
            raise TreeStructureError

        # request with special flag - can go ahead
        # from 2.1.0 TreeServiceUser is no longer required to bypass the blocker
        if self._check_has_param(request=request):
            logger.debug("Retrieved a message with a flag was bypassing the TreeBaseRequestBlocker")
            raise TreeStructureError

        current_user = self._get_current_reservation()
        if current_user and request.user == current_user:
            raise TreeStructureError
        raise AddressError(code=1004)

    def make_reservation(self, user: BaseTreeUser, timeout_reservation: float = None):
        """
        This method make reservation for given user.

        :param user: user object witch one want to reserve time on this block
        :param timeout_reservation: reservation timeout
        :raise ReservationError: if can not now reserve time this block or reservation is too long
        :return:
        """
        current_user = self._get_current_reservation()
        if current_user is not None and current_user != user:
            raise ReservationError
        timeout_reservation = time.time() + self._get_cfg('default_control_time',
                                                          0) if timeout_reservation is None else timeout_reservation
        if timeout_reservation-time.time() > self._get_cfg('max_control_time', 60):
            raise ReservationError
        self._current_user = user
        self._timeout_reservation = timeout_reservation

    def _get_current_reservation(self) -> None or BaseTreeUser:
        """
        This method return current user witch one now use this block or None if No one use.

        :return: Current user or None
        """
        if self._current_user:
            if self._timeout_reservation <= time.time():
                self.cancel_reservation()
        return self._current_user

    def get_timeout_current_reservation(self) -> float or None:
        """Method return timeout reservation for current user as float value or None if no one make reservation"""
        if self._get_current_reservation():
            return self._timeout_reservation
        return None

    def cancel_reservation(self):
        """Remove current reservation"""
        self._current_user = None
        self._timeout_reservation = 0

    def get_current_user(self) -> None or BaseTreeUser:
        """
        Return Current user witch one was reserved or None if block is free.

        :return: None or current user
        """
        return self._get_current_reservation()

    def add_to_white_list(self, adr: str, type_req: str):
        list_ = self._get_list_of_special_requests(color_list='WHITE', type_req=type_req)
        if list_ is None:
            logger.error(f'Can not find list for this request type: {type_req}')
            raise ValueError
        if adr not in list_:
            list_.append(adr)
        else:
            logger.info(f'This address: {adr} is already on white list')

    def add_to_black_list(self, adr: str, type_req: str):
        list_ = self._get_list_of_special_requests(color_list='BLACK', type_req=type_req)
        if list_ is None:
            logger.error(f'Can not find list for this request type: {type_req}')
            raise ValueError
        if adr not in list_:
            list_.append(adr)
        else:
            logger.info(f'This address: {adr} is already on black list')

    def _is_on_whitelist(self, adr: str, type_req: str):
        list_ = self._get_list_of_special_requests(color_list='WHITE', type_req=type_req)
        if list_ is None:
            return False
        if adr in list_:
            return True
        return False

    def _is_on_black_list(self, adr: str, type_req: str):
        list_ = self._get_list_of_special_requests(color_list='BLACK', type_req=type_req)
        if list_ is None:
            return False
        if adr in list_:
            return True
        return False

    def _check_white_list(self, request: ValueRequest):
        adr = '.'.join(request.address[request.index:])
        return self._is_on_whitelist(adr, request.request_type)

    def _check_black_list(self, request: ValueRequest):
        adr = '.'.join(request.address[request.index:])
        return self._is_on_black_list(adr, request.request_type)

    def _get_list_of_special_requests(self, color_list: str, type_req: str):
        if color_list == 'BLACK':
            return self._black_lists.get(type_req, None)
        elif color_list == 'WHITE':
            return self._white_lists.get(type_req, None)
        return None

    def _init_lists_of_special_requests(self):
        """This method gets the data from the configuration file and initializes the black/white/other list with it."""
        initial_data = self._get_cfg('white_list', {})
        for key, val in self._white_lists.items():
            val.extend(initial_data.get(key, []))
        initial_data = self._get_cfg('black_list', {})
        for key, val in self._black_lists.items():
            val.extend(initial_data.get(key, []))

    def _check_has_param(self, request: ValueRequest) -> bool:
        param = request.request_data.get(self.SPECIAL_PERMISSION_PARAM, None)
        return param is not None and isinstance(param, bool) and param

    # --- Safety cutoff ---

    def engage_safety_cutoff(self):
        """Engage the safety cutoff switch, blocking all dangerous commands."""
        self._safety_cutoff_engaged = True
        logger.info(f"Safety cutoff ENGAGED on {self.get_name()}")

    def disengage_safety_cutoff(self):
        """Disengage the safety cutoff switch, restoring normal operation."""
        self._safety_cutoff_engaged = False
        logger.info(f"Safety cutoff DISENGAGED on {self.get_name()}")

    def is_safety_cutoff_engaged(self) -> bool:
        """Return whether the safety cutoff switch is currently engaged."""
        return self._safety_cutoff_engaged

    def get_safety_cutoff_list(self) -> List[str]:
        """Return the list of commands/addresses blocked when safety cutoff is engaged."""
        return list(self._safety_cutoff_list)

    def _is_cutoff_blocked(self, request: ValueRequest) -> bool:
        """Check if the request targets a command blocked by the safety cutoff.

        Matching rules:
            - Entry without a dot matches the last segment of the address (command name).
            - Entry with a dot matches the full relative address exactly.
        """
        adr = '.'.join(request.address[request.index:])
        command = adr.rsplit('.', 1)[-1]
        for pattern in self._safety_cutoff_list:
            if '.' in pattern:
                if adr == pattern:
                    return True
            else:
                if command == pattern:
                    return True
        return False

    def _check_has_safety_cutoff_bypass(self, request: ValueRequest) -> bool:
        param = request.request_data.get(self.SAFETY_CUTOFF_BYPASS_PARAM, None)
        return param is not None and isinstance(param, bool) and param

    def _init_safety_cutoff_list(self):
        """Load the safety cutoff command list from configuration."""
        self._safety_cutoff_list = list(self._get_cfg('safety_cutoff_list', []))


class ReservationError(Exception):
    pass
