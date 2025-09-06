import logging

from obsrv.tree_components.base_components.tree_component import AddressedResponderProtocol
from obsrv.tree_components.base_components.tree_provider import TreeProvider
from obcom.data_colection.value import Value
from obcom.data_colection.value_call import ValueRequest, ValueResponse

logger = logging.getLogger(__name__.rsplit('.')[-1])


class TreeProviderFrontend(TreeProvider):
    COMPONENT_DEFAULT_NAME: str = 'TreeProviderFrontend'

    def __init__(self, component_name: str, subcontractor: AddressedResponderProtocol = None,
                 **kwargs):
        if subcontractor:
            source_name = subcontractor.get_source_name()
            auxiliary_source_names = subcontractor.get_source_names()
        else:
            logger.warning(f'No subcontractor has been indicated, this module {type(self).__name__} is '
                           f'closely related to its subcontractor and will not work properly without it.')
            source_name = 'no_named'
            auxiliary_source_names = []

        super().__init__(component_name=component_name, subcontractor=subcontractor, source_name=source_name,
                         auxiliary_source_names=auxiliary_source_names, **kwargs)
        self._subcontractor: AddressedResponderProtocol = subcontractor

    def get_source_name(self) -> str:
        """
        The method returns the name of the subcontractor to use when decoding the query address.
        This class copies the name from the subcontractor.

        :return: name of that component with will be used to decode address
        """
        if self._subcontractor:
            return self._subcontractor.get_source_name()
        else:
            return self._source_name

    def get_source_names(self) -> list:
        if self._subcontractor:
            return self._subcontractor.get_source_names()
        else:
            return self._source_names

    async def get_value(self, request: ValueRequest, **kwargs) -> Value or None:
        # docstring is imported from parent
        return await super(TreeProvider, self).get_value(request=request)

    async def get_response(self, request: ValueRequest) -> ValueResponse:
        # docstring is imported from parent
        return await super(TreeProvider, self).get_response(request)
