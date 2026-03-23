import logging
from typing import List

from obsrv.tree_components.base_components.address_dispatcher import AddressDispatcher
from obsrv.tree_components.base_components.tree_base_provider import TreeBaseProvider
from obsrv.tree_components.base_components.tree_component import ProvidesResponseProtocol
from obcom.data_colection.response_error import ResponseError
from obcom.data_colection.value import Value
from obcom.data_colection.value_call import ValueRequest, ValueResponse

logger = logging.getLogger(__name__.rsplit('.')[-1])


class TreeProvider(TreeBaseProvider, AddressDispatcher):
    """
    PX

    :param component_name: this is name of tree component, used for debug
    :param source_name: This is a component name with will be used to decode address
    :param subcontractor: instance of next component in tree

    :ivar _subcontractor: instance of next component in tree

    Conforms to:
        ProvidesResponseProtocol
    """

    COMPONENT_DEFAULT_NAME: str = 'TreeProvider'

    def __init__(self, component_name: str, source_name: str, subcontractor: ProvidesResponseProtocol = None,
                 **kwargs):
        super().__init__(component_name=component_name, subcontractor=subcontractor, source_name=source_name, **kwargs)

    async def get_value(self, request: ValueRequest, **kwargs) -> Value or None:
        # docstring is imported from parent
        return await super().get_value(request=request)

    async def get_response(self, request: ValueRequest) -> ValueResponse:
        # docstring is imported from parent
        if request.index >= len(request.address):
            re = ResponseError(2002, f'Provider was unable to create value. The address is too short to call the '
                                     f'provider. if you want to ask this supplier, the question should contain one of '
                                     f'address: {".".join(self.get_source_names())}.', repr(self),
                               severity=ResponseError.SEVERITY_NORMAL)
            logger.info(f'Wrong format request address {request.address} - make error response')
            return ValueResponse(request.address, None, False, re)  # address is damaged
        if not self.is_named(request.address[request.index], only_main_name=True):
            logger.warning(f"The request was passed to a block ({'.'.join(self.get_source_names())}) not contained in "
                           f"the address ({request.address.__str__()}).")
            re = ResponseError(1002, f"The request was passed to a block ({'.'.join(self.get_source_names())}) not "
                                     f"contained in the address ({request.address.__str__()}).", repr(self),
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
