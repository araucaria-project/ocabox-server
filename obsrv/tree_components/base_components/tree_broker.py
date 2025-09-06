import logging
from typing import List

from obsrv.tree_components.base_components.address_dispatcher import AddressDispatcher
from obsrv.tree_components.base_components.tree_base_broker import TreeBaseBroker
from obsrv.tree_components.base_components.tree_component import AddressedResponderProtocol
from obcom.data_colection.response_error import ResponseError
from obcom.data_colection.value_call import ValueRequest, ValueResponse

logger = logging.getLogger(__name__.rsplit('.')[-1])


class TreeBroker(TreeBaseBroker, AddressDispatcher):
    """
    BX

    :param component_name: this is name of tree component, used for debug
    :param source_name: This is a component name with will be used to decode address
    :param list_providers: list all next providers in tree

    Conforms to:
        AddressedResponderProtocol
    """

    COMPONENT_DEFAULT_NAME: str = 'TreeBroker'

    def __init__(self, component_name: str, source_name: str, list_providers: List[AddressedResponderProtocol] = None,
                 **kwargs):
        super().__init__(component_name=component_name, list_providers=list_providers, source_name=source_name,
                         **kwargs)

    async def get_response(self, request: ValueRequest) -> ValueResponse:
        # docstring is imported from parent
        if not self.is_named(request.address[request.index]):
            logger.warning(f"The request was passed to a block ({'.'.join(self.get_source_names())}) not contained in "
                           f"the address ({request.address.__str__()}).")
            re = ResponseError(1002, f"The request was passed to a block named ({'.'.join(self.get_source_names())}) "
                                     f"not contained in the address ({request.address.__str__()}).", repr(self),
                               severity=ResponseError.SEVERITY_NORMAL)
            return ValueResponse(request.address, None, False, re)  # address is wrong
        request.index += 1
        return await super().get_response(request)

    def get_resources(self):
        names: List[str] = self.get_source_names()
        resource_name = self.get_resource_name()
        if names and resource_name:
            return [(resource_name, names)]
        return []

    def set_tree_path(self, address_to_object: str):
        address_to_object = address_to_object + "." if address_to_object else address_to_object
        self.tree_path = address_to_object + self.get_source_name()
