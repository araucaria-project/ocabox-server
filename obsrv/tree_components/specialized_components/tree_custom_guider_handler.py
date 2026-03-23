import logging
from typing import Optional

from obcom.data_colection.address import AddressError
from obsrv.tree_components.base_components.tree_provider import TreeProvider
from obsrv.tree_components.specialized_components.tree_alpaca import TreeAlpacaObservatory
from obcom.data_colection.value import Value
from obcom.data_colection.value_call import ValueRequest

logger = logging.getLogger(__name__.rsplit('.')[-1])



class TreeCustomGuiderHandler(TreeProvider):
    """
    This module is responsible for managing custom telescope guider.
    The module has several defined address commands:
        - method1 - cos ...
        - method2 - cos ...
    """

    COMPONENT_DEFAULT_NAME: str = 'TreeCustomGuiderHandler'
    _CFG_PRP_GDR_SRC_NAME = "guider_source_name"

    def __init__(self, component_name: str, source_name: str, target_alpaca: TreeAlpacaObservatory, **kwargs):
        logger.warning("Deprecated module TreeCustomGuiderHandler loaded, it will be removed in future releases")
        super().__init__(component_name=component_name, source_name=source_name, **kwargs)

    async def get_value(self, request: ValueRequest, **kwargs) -> Optional[Value]:
        # deprecated

        raise AddressError(code=1002, message=f'Deprecated module {self.get_name()}')

