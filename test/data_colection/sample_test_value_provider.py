import asyncio
import random
import time
from typing import List, Tuple

from obcom.data_colection.address import AddressError
from obsrv.data_colection.base_components.tree_component import ProvidesResponseProtocol
from obsrv.data_colection.base_components.tree_provider import TreeProvider
from obcom.data_colection.coded_error import TreeStructureError
from obcom.data_colection.value import Value, TreeValueError
from obcom.data_colection.value_call import ValueRequest


class SampleTestValueProvider(TreeProvider):
    """
    This is sample ValueProvider class who return sample value. Return values are defined in the list SAMPLE_VALUES or
    create a new one from 1 to 1000 by calling `new_val` or return always the same value by calling `static_val`. It
    can return errors by calling the appropriate address: [`error`,`structure_error`,`value_error`,`address_error`]
    or by specifying the request parameter.

    This block handles the request parameters:
        - raise_value_error - return response with TreeValueError
        - raise_address_error - return response with AddressError
        - raise_structure_error - return response with TreeStructureError
    """
    RESPONSE_DELAY = 0

    SAMPLE_VALUES: List[Tuple[str, Value]] = [('val1', Value(1, 1661349399.030824)),
                                              ('val2', Value(2, 1661349399.030824 - 152))]

    def __init__(self, component_name: str, source_name: str, test_values: List[Tuple[str, Value]],
                 subcontractor: ProvidesResponseProtocol = None, **kwargs):
        self.response_delay = self.RESPONSE_DELAY
        # statistic
        self.nr_requests = 0
        self.static_val = 55
        self.test_values: List[Tuple[str, Value]] = test_values if test_values else self.SAMPLE_VALUES
        super().__init__(component_name=component_name, source_name=source_name, subcontractor=subcontractor,
                         **kwargs)

    async def get_value(self, request: ValueRequest, **kwargs) -> Value or None:
        self.nr_requests += 1
        if request.request_data.get('raise_value_error', False):
            raise TreeValueError
        elif request.request_data.get('raise_address_error', False):
            raise AddressError
        elif request.request_data.get('raise_structure_error', False):
            raise TreeStructureError
        elif request.request_data.get('raise_value_error_temporary', False):
            raise TreeValueError(code=4005, severity=TreeValueError.SEVERITY_TEMPORARY)

        address = request.address
        value_name = address[address.get_last_index()]
        await asyncio.sleep(self.response_delay)
        for n, v in self.test_values:
            if value_name == n:
                return v
        if value_name == 'error':
            raise TreeStructureError
        elif value_name == 'structure_error':
            raise TreeStructureError
        elif value_name == 'value_error':
            raise TreeValueError
        elif value_name == 'value_error_coded':
            raise TreeValueError(None, 10, '')
        elif value_name == 'address_error':
            raise AddressError
        elif value_name == 'address_error_coded':
            raise AddressError(request.address, 20, '')
        elif value_name == 'wrong_type':
            return 34
        if value_name == 'crash':
            raise ValueError
        elif value_name == 'new_val':
            return Value(random.randint(0, 1000), time.time())
        elif value_name == 'static_val':
            return Value(55, time.time())
        return None
