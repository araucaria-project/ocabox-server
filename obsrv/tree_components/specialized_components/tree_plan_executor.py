import logging
import time

from obcom.data_colection.address import AddressError
from obsrv.tree_components.base_components.tree_provider import TreeProvider
from obcom.data_colection.value import Value
from obcom.data_colection.value_call import ValueRequest

logger = logging.getLogger(__name__.rsplit('.')[-1])


class TreePlanExecutor(TreeProvider):
    """
    This module is responsible for launching and managing alpaca observation plans. The module has several defined
    address commands:
        - method1 - do stuff 1
        - method2 - do stuff 2
    """

    def __init__(self, component_name: str, source_name: str, **kwargs):
        super().__init__(component_name=component_name, source_name=source_name, subcontractor=None, **kwargs)

    async def get_value(self, request: ValueRequest, **kwargs) -> Value or None:
        user = request.user
        request_type = request.request_type
        try:
            command = request.address[request.index]
        except IndexError:
            raise AddressError(code=1001, message='The address does not contain a command.')

        if command == 'method1':
            timeout_control = "response string"
            return Value(v=timeout_control, ts=time.time())

        if command == 'method2':
            timeout_control = "response string"
            return Value(v=timeout_control, ts=time.time())
        raise AddressError(code=1002, message=f'Unrecognised method for module {self.get_name()}')
