import asyncio
import logging
import time
from typing import List, Tuple

from obcom.data_colection.address import AddressError
from obsrv.data_collection.alpaca_api.connector import Connector, AlpacaConnector
from obsrv.data_collection.alpaca_api.exceptions import AlpacaHttp400Error, AlpacaHttp500Error, AlpacaContentTypeError, \
    AlpacaError, AlpacaHttpError, RequestConnectionError
from obsrv.telescope_devices.device_tree import Observatory
from obsrv.tree_components.base_components.tree_base_provider import TreeBaseProvider
from obcom.data_colection.coded_error import TreeOtherError
from obsrv.data_collection.resource_manager.resource_manager import TelescopeComponentManagerAlpaca
from obcom.data_colection.value import Value, TreeValueError
from obcom.data_colection.value_call import ValueRequest
from obsrv.utils.asyncio_util_functions import wait_for_psce
from aiohttp.client_exceptions import ClientConnectionError, ServerConnectionError

logger = logging.getLogger(__name__.rsplit('.')[-1])

# todo usunac nazwe 'Alpaca' z nazwy
class TreeAlpacaObservatory(TreeBaseProvider):
    """
    This object represents an alpaca module in a tree structure. Responsible for redirecting queries to the alpaca
    server with the api device. The object hasn't an address in the tree structure.

    :param component_name: this is name of tree component, used for debug
    :param observatory_name: This is observatory name, it is used to get configuration from config file.
    """
    DEFAULT_PROTOCOL = 'alpaca'
    COMPONENT_DEFAULT_NAME: str = 'TreeAlpacaObservatory'

    def __init__(self, component_name: str, observatory_name=None, **kwargs):
        super().__init__(component_name=component_name, subcontractor=None, **kwargs)
        self.observatory_name = observatory_name if observatory_name else component_name
        self._observatory = Observatory()
        self._connector: Connector
        self._connect_to_observatory()
        self._timeout_multiplier = self._get_timeout_multiplier()
        self._resource_manager = None

    def _get_timeout_multiplier(self):
        hard_default = 0.8
        timeout_multiplier = self._get_cfg("timeout_multiplier", hard_default)
        if not (0 < timeout_multiplier < 1):
            logger.warning(f"Can not set timeout_multiplier {timeout_multiplier}. Should be greater than 0 and lover "
                           f"than 1. Will be set to default value: {hard_default}")
            timeout_multiplier = hard_default
        return timeout_multiplier

    def _connect_to_observatory(self):
        self._connector = AlpacaConnector()
        self._observatory.connect(['tree', self.observatory_name], connector=self._connector)

    async def run(self):
        result = await self._connector.create_http_session()
        if result:
            logger.info(f"Permanent http session was successfully create for {self}")
        else:
            logger.warning(f"Can not create permanent http session for {self}. A 'on-demand session' will be used")
        await super().run()

    async def stop(self):
        await self._connector.close()
        await super().stop()

    def _get_alpaca_method(self, alpaca_address: list):
        """
        Get method or attribute from alpaca connector if exists

        :param alpaca_address: path to alpaca method
        :return: alpaca method or attribute
        """
        method = None
        for atr in alpaca_address:
            try:
                method = getattr(method if method else self._observatory, atr)
            except AttributeError:
                logger.info(f"Alpaca hasn't method {'.'.join(alpaca_address)}")
                raise
        return method

    async def get_value(self, request: ValueRequest, **kwargs) -> Value or None:
        address = request.address
        index = request.index
        alpaca_address = address[index:].copy()
        request_type = request.request_type
        request_arguments = request.request_data
        request_timeout = request.request_timeout

        if len(alpaca_address) <= 0:
            logger.debug(f"Incoming address to the {self._component_name} module is too short. Address: {address}")
            raise AddressError(address=address, code=1001, message="Incoming address is too short")

        alpaca_method_name = alpaca_address[len(alpaca_address) - 1]
        alpaca_address[len(alpaca_address) - 1] = 'put' if request_type == 'PUT' else 'get'
        try:
            method = self._get_alpaca_method(alpaca_address)
        except AttributeError:
            raise AddressError(address=address, code=1002, message="Alpaca driver does not have such a method")

        if not method or not callable(method):
            raise AddressError(address=address, code=1002, message="Can not call this alpaca method in driver")

        try:
            result = await wait_for_psce(method(alpaca_method_name, **request_arguments),
                                         timeout=(request_timeout-time.time())*self._timeout_multiplier)
        except asyncio.CancelledError:
            raise
        except AlpacaHttp400Error as e:
            # if server alpaca return 400 error
            logger.warning(f"Alpaca throw error 400 for request {address}")
            raise TreeValueError(address=None, code=2002, message=e.message)
        except AlpacaHttp500Error as e:
            # if server alpaca return 500 error
            logger.warning(f"Alpaca throw error 500 for request {address}")
            raise TreeValueError(address=None, code=2002, message=e.message)
        except AlpacaContentTypeError as e:
            # if server alpaca return data in wrong format
            logger.warning(f"Alpaca throw error AlpacaContentTypeError for request {address}")
            raise TreeValueError(address=None, code=2002, message=e.message)
        except AlpacaError as e:
            # when server alpaca throws an error with a numeric value
            logger.warning(f"Alpaca throw numeric error for request {address}")
            raise TreeValueError(address=None, code=2002, message=e.message)
        except AlpacaHttpError as e:
            # if server alpaca return unresolved error
            logger.warning(f"Alpaca throw AlpacaHttpError for request {address}")
            raise TreeValueError(address=None, code=2002, message=e.message)
        except RequestConnectionError:
            # when can not connect to alpaca
            logger.warning(f"Server alpaca is not responding at {address}")
            raise TreeOtherError(address=None, code=4005, message=f"Server alpaca is not responding at {address}",
                                 severity=TreeOtherError.SEVERITY_TEMPORARY)
        except asyncio.TimeoutError:
            # Catching error TimeoutError does NOT conflict with the main timeout on task in Router
            logger.warning(f"Server alpaca is not responding at address {address} before timeout")
            raise TreeOtherError(address=None, code=4005, message=f"Server alpaca is not responding at {address}",
                                 severity=TreeOtherError.SEVERITY_TEMPORARY)
        except (TypeError, ValueError):
            # when given arguments is wrong
            logger.warning(f"Alpaca driver get wrong arguments to run function")
            raise AddressError(address=address, code=1003, message=f"Wrong arguments for method")
        except ServerConnectionError as e:
            logger.warning(f"AioHTTP error throw error {str(e)} for request {address}")
            raise TreeValueError(address=None, code=2002, message=str(e), severity=TreeOtherError.SEVERITY_TEMPORARY)
        except ClientConnectionError as e:
            logger.warning(f"AioHTTP error throw error {str(e)} for request {address}")
            raise TreeValueError(address=None, code=2002, message=str(e))
        return Value(result, time.time())

    def get_resources(self):
        # WARNING, resources are not searched deep into the alpaca tree. Duplicates in the config leading to the same
        # elements are not checked.
        map_ = self._get_resources_simple_map()
        out = []
        for key, adr, kind, nr, prop in map_:
            out.append((key, [adr]))
        return out

    def _get_resources_simple_map(self) -> List[Tuple[str, str, str, int, dict]]:
        obs_children = self._observatory.children
        out = []
        for key, val in obs_children.items():
            out.append((key + "_RESOURCE", key, val.kind, val.device_nr,
                        {"observatory_name": self.observatory_name, **self._observatory.component_options, **val.component_options}))
        return out

    async def get_res_manager(self) -> TelescopeComponentManagerAlpaca:
        if self._resource_manager is None:
            self._resource_manager = TelescopeComponentManagerAlpaca(target=self.target_requests, path=self.tree_path,
                                                                     observatory_name=self.observatory_name)
            await self._resource_manager.initiate_resource_manager(self._get_resources_simple_map())
        return self._resource_manager

    def get_configuration(self) -> dict:
        out = self._get_self_configuration()
        obs_cfq = {"observatory": self._observatory.observatory_configuration_rare}
        out.get(self.get_name()).get("config").update({"observatory_config": obs_cfq})
        out.get(self.get_name()).get("config").update({"observatory_config_name": self.observatory_name})
        return out

    def __del__(self):
        self._connector.__del__()
