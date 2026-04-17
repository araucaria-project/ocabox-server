"""
TreeIrisObservatory - Tree adapter for IRIS Observatory.
"""
import logging
import time
from typing import Optional

from obcom.data_colection.address import AddressError
from obcom.data_colection.coded_error import BaseCodedError
from obcom.data_colection.value import Value, TreeValueError
from obcom.data_colection.value_call import ValueRequest

from obsrv.tree_components.base_components.tree_base_provider import TreeBaseProvider
from obsrv.telescope_devices.device_tree import Observatory
from obsrv.utils.asyncio_util_functions import wait_for_psce

logger = logging.getLogger(__name__.rsplit('.')[-1])


class TreeIrisObservatory(TreeBaseProvider):
    """
    Tree adapter specific for IRIS Observatory.
    Handles mixed protocols (Pilar, IrisCCD, Alpaca).
    
    :param component_name: Name of tree component for debugging
    :param observatory_name: Observatory name used to get configuration from config file
    """
    
    COMPONENT_DEFAULT_NAME: str = 'TreeIrisObservatory'
    
    def __init__(self, component_name: str, observatory_name=None, **kwargs):
        super().__init__(component_name=component_name, subcontractor=None, **kwargs)
        self.observatory_name = observatory_name if observatory_name else component_name
        self._observatory = Observatory()
        self._timeout_multiplier = self._get_timeout_multiplier()
        self._connect_to_observatory()
    
    def _get_timeout_multiplier(self):
        hard_default = 0.8
        timeout_multiplier = self._get_cfg("timeout_multiplier", hard_default)
        if not (0 < timeout_multiplier < 1):
            logger.warning(f"Can not set timeout_multiplier {timeout_multiplier}. Should be greater than 0 and lower "
                          f"than 1. Will be set to default value: {hard_default}")
            timeout_multiplier = hard_default
        return timeout_multiplier
    
    def _connect_to_observatory(self):
        """Connect to the observatory using the universal Observatory class."""
        try:
            # This triggers loading config and creating connectors based on 'protocol' field in config.yaml
            self._observatory.connect(['tree', self.observatory_name])
        except Exception as e:
            logger.warning(f"Could not load configuration for {self.observatory_name}: {e}")
            # Fallback configuration if needed
            self._observatory.observatory_configuration_rare = {"protocol": "pilar"}
    
    async def run(self):
        await super().run()
    
    async def stop(self):
        await super().stop()
    
    async def get_value(self, request: ValueRequest, **kwargs) -> Value or None:
        address = request.address
        index = request.index
        iris_address = address[index:].copy()
        request_type = request.request_type
        request_arguments = request.request_data
        request_timeout = request.request_timeout

        if len(iris_address) <= 0:
            logger.debug(f"Incoming address to the {self._component_name} module is too short. Address: {address}")
            raise AddressError(address=address, code=1001, message="Incoming address is too short")

        try:
            # Navigate to component in the Observatory tree
            component = self._observatory
            for addr_part in iris_address[:-1]:
                component = component.children[addr_part]
            
            method_name = iris_address[-1]
            
            # Execute request - the component will use its assigned connector (Pilar, IrisCCD or Alpaca)
            if request_type == 'PUT':
                result = await wait_for_psce(
                    component.put(method_name, **request_arguments),
                    timeout=(request_timeout - time.time()) * self._timeout_multiplier
                )
            else:
                result = await wait_for_psce(
                    component.get(method_name, **request_arguments),
                    timeout=(request_timeout - time.time()) * self._timeout_multiplier
                )
            
            return Value(result, time.time())
            
        except KeyError:
            raise AddressError(address=address, code=1002, message="Observatory component not found")
        except BaseCodedError:
            raise
        except Exception as e:
            logger.warning(f"Observatory error for {address}: {e}")
            raise TreeValueError(address=None, code=2002, message=str(e))
    
    def get_resources(self):
        if not self._observatory:
            return []
        out = []
        for key, component in self._observatory.children.items():
            out.append((key, [key]))
        return out
    
    def get_configuration(self) -> dict:
        out = self._get_self_configuration()
        if self._observatory:
            obs_cfg = {"observatory": self._observatory.observatory_configuration_rare}
            out.get(self.get_name()).get("config").update({"observatory_config": obs_cfg})
            out.get(self.get_name()).get("config").update({"observatory_config_name": self.observatory_name})
        return out

    def get_publishable_config(self) -> Optional[dict]:
        if not self._observatory:
            return None
        return {
            "role": "observatory",
            "observatory_config_name": self.observatory_name,
            "observatory": self._observatory.observatory_configuration_rare,
        }