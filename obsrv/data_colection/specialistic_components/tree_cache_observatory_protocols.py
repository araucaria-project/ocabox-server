from typing import Protocol, runtime_checkable

from obcom.data_colection.address import Address
from obsrv.data_colection.base_components.tree_component import ProvidesResponseProtocol
from obsrv.data_colection.specialistic_components.tree_conditional_freezer_protocol import TreeConditionalFreezerProtocol
from obcom.data_colection.value import Value
from obcom.data_colection.value_call import ValueRequest


@runtime_checkable
class KnownValueProtocol(Protocol):

    def get_change_time(self) -> float:
        pass

    def get_timestamp(self) -> float or None:
        pass

    def get_value(self) -> Value:
        pass


@runtime_checkable
class TreeCacheProtocol(ProvidesResponseProtocol, Protocol):

    def set_conditional_freezer(self, cf: TreeConditionalFreezerProtocol):
        pass

    def remove_conditional_freezer(self):
        pass

    def get_k_val(self, address: Address) -> KnownValueProtocol or None:
        pass

    def is_cachable_request(self, request: ValueRequest) -> bool:
        pass
