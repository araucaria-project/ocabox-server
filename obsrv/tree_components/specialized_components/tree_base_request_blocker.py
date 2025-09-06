
import logging
import time
from typing import Dict

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
    """

    COMPONENT_DEFAULT_NAME: str = 'TreeBaseRequestBlocker'
    SPECIAL_PERMISSION_PARAM: str = "request_special_permission_param"  # use only for requesting inside ocabox

    def __init__(self, component_name: str, subcontractor: ProvidesResponseProtocol = None, **kwargs):
        super().__init__(component_name=component_name, subcontractor=subcontractor, **kwargs)
        self._current_user: BaseTreeUser or None = None
        self._timeout_reservation: float = 0
        self._black_lists: Dict[str, list] = {'GET': [],
                                              'PUT': []}
        self._white_lists: Dict[str, list] = {'GET': [],
                                              'PUT': []}
        self._init_lists_of_special_requests()

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

        # white list always can go ahead
        if self._check_white_list(request=request):
            raise TreeStructureError

        # request witch special flag - can go ahead
        if self._check_has_param(request=request) and isinstance(request.user, TreeServiceUser):
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


class ReservationError(Exception):
    pass
