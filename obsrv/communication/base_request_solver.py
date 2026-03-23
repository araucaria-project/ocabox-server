import asyncio
import logging
from typing import List
from abc import ABC, abstractmethod
import confuse
from nats.errors import TimeoutError
from serverish.base import dt_utcnow_array, MessengerNotConnected
from serverish.messenger import get_publisher
from obsrv.communication.nats_streams import NatsStreams
from obcom.data_colection.response_error import ResponseError
from obsrv.utils.tree_data import TreeData
from obcom.data_colection.tree_user import TreeUser, TreeServiceUser
from obcom.data_colection.value_call import ValueRequest, ValueResponse
from obsrv.tree_components.base_components.tree_component import ProvidesResponseProtocol
from obsrv.ob_config import SingletonConfig

logger = logging.getLogger(__name__.rsplit('.')[-1])


class BaseRequestSolver(ABC):

    def __init__(self, data_provider: ProvidesResponseProtocol, **kwargs):
        self.data_provider: ProvidesResponseProtocol = data_provider
        self._tree_data = TreeData(target_requests=self)
        if self.data_provider is not None:
            self.data_provider.post_init_tree(tree_data=self._tree_data, tree_path="")
        else:
            logger.warning(f"RequestSolver has not any provider, can not initialize provider")
        self._nats_host = SingletonConfig.get_config()['nats']['host'].get()
        self._nats_port = SingletonConfig.get_config()['nats']['port'].get()

    @abstractmethod
    async def get_answer(self, request: List[bytes], user_id: bytes, timeout=None) -> List[bytes]:
        """
        This method is designed to provide a response to given request by calling to data_provider. The received query
        is a list of minor queries for single values. This method runs a separate task for each query in order to
        reduce the waiting time. Created tasks are associated with this method and if the task that called this method
        is canceled, all tasks created by it will also be canceled.

        :param request: List of bytes representing ValueRequest
        :param user_id: User id
        :param timeout: request timeout
        :return: List of bytes representing ValueResponse
        """
        pass

    @abstractmethod
    async def get_single_answer(self, request: bytes, user_id: bytes, timeout=None) -> bytes:
        """
        The method reads the query details and calls the provider for an answer for a single query.

        :param request: bytes representing ValueRequest
        :param user_id: User id
        :param timeout: request timeout
        :return: bytes representing ValueResponse
        """
        pass

    async def run_tree(self):
        """
        This method starts cascading asynchronous tree initialization.

        :return:
        """
        await self._tree_data.nats_messenger.open(host=self._nats_host, port=self._nats_port, wait=10)
        # try:
        #     await wait_for_psce(self._tree_data.nats_messenger.open(host=self._nats_host, port=self._nats_port), 10)
        # except asyncio.TimeoutError:
        #     logger.error(f"Can not connect to server NATS")
        #     raise RuntimeError("Can not connect to server NATS")
        await self.data_provider.run()
        await self._nats_update_config_alpaca()

    async def stop_tree(self):
        """
        This method stops cascading asynchronous tree elements.

        :return:
        """
        try:
            await self.data_provider.stop()
        finally:
            await self._tree_data.nats_messenger.close()

    async def get_answer_internal(self, request: List[ValueRequest], timeout=None) -> List[ValueResponse]:
        response = []
        coroutines = []
        for r in request:
            coroutines.append(self.get_single_answer_internal(r, timeout=timeout))
        result = await asyncio.gather(*coroutines, return_exceptions=True)
        for r in result:
            if isinstance(r, ValueResponse):
                response.append(r)
            elif isinstance(r, BaseException):
                logger.error(f"CRITICAL One of the sub-tasks raise some unresolved exception - {type(r)}: {r}.")
                re = ResponseError(4001, 'There were unexpected problems trying to respond to the request', repr(self),
                                   ResponseError.SEVERITY_CRITICAL)
                v_response = ValueResponse('', None, False, re)
                response.append(v_response)
            else:
                logger.error(f"CRITICAL One of the sub-tasks return not supported type response - {type(r)}: {r}.")
                re = ResponseError(4001, 'There were unexpected problems trying to respond to the request', repr(self),
                                   ResponseError.SEVERITY_CRITICAL)
                v_response = ValueResponse('', None, False, re)
                response.append(v_response)
        return response

    async def get_single_answer_internal(self, v_request: ValueRequest, timeout=None) -> ValueResponse:
        if isinstance(v_request.user, TreeUser):
            logger.warning(f'Internal request has normal ({TreeUser.__name__}) user instance, should be '
                           f'{TreeServiceUser.__name__} instead. Will be created new correct user and will be change')
            v_request.user = TreeServiceUser(**v_request.user.to_dict())
        if v_request.request_timeout != timeout and timeout:
            v_request.request_timeout = timeout
        v_response = await self._get_single_answer(v_request=v_request)
        return v_response

    async def _get_single_answer(self, v_request: ValueRequest):
        # Can not find value provider application
        if not self.data_provider or not isinstance(self.data_provider, ProvidesResponseProtocol):
            logger.error('Can not find value provider application.')
            re = ResponseError(4002, '', repr(self), ResponseError.SEVERITY_CRITICAL)
            v_response = ValueResponse(v_request.address, None, False, re)
            return v_response
        # try to get response
        # get_response method shouldn't raise any errors !!!
        try:
            v_response = await self.data_provider.get_response(v_request)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f'{str(e)}')
            re = ResponseError(4002, '', repr(self), ResponseError.SEVERITY_CRITICAL)
            v_response = ValueResponse(v_request.address, None, False, re)
        return v_response

    def get_tree_configuration(self) -> dict:
        """
        Method return dictionary witch tree structure and configuration every single block in tree.

        :return: dictionary
        """
        return self.data_provider.get_configuration()

    def _get_alpaca_modules_configuration(self) -> dict:
        configuration_tree = self.get_tree_configuration()
        out = {}
        alpacas = self._find_alpacas(configuration_tree)
        for a in alpacas:
            out.update(a)
        return out

    def _find_alpacas(self, config) -> list:
        from obsrv.tree_components.specialized_components.tree_alpaca import TreeAlpacaObservatory
        li = []
        for key, val in config.items():
            if val.get("type", "") == TreeAlpacaObservatory.__name__:
                name = val.get("config", None).get("observatory_config_name", key) if val.get("config", None) else key
                li.append({name: val.get("config", {}).get("observatory_config")})
            else:
                li = li + self._find_alpacas(config=val.get("child", []))
        return li

    @staticmethod
    def _get_site_cfg() -> dict:
        out = {}
        try:
            site = SingletonConfig.get_config()['site'].get()
            if site is not None and isinstance(site, dict):
                out = site
        except confuse.exceptions.NotFoundError:
            logger.warning("Can not find key: site  in configuration")
        return out

    async def _nats_update_config_alpaca(self) -> bool:
        publisher = get_publisher(NatsStreams.ALPACA_CONFIG)
        cfg = self._get_alpaca_modules_configuration()
        site_cfg = self._get_site_cfg()
        try:
            await publisher.publish(data={'version': "",  # todo uzupełnić
                                          'published': dt_utcnow_array(),
                                          'config': {'telescopes': cfg,
                                                     'site': site_cfg}},
                                    meta={
                                        "message_type": "config",  # IMPORTANT type message, one of pre declared types
                                        "tags": ["config_alpaca"],
                                        'sender': 'Ocabox server',
                                    })
            out = True
        except MessengerNotConnected as e:
            logger.error(f"Can not publish config alpaca to nats, error: {e}")
            out = False
        except TimeoutError as e:
            logger.error(f"Can not publish config alpaca to nats, error: {e}")
            out = False
        return out

    async def reload_nats_config(self) -> bool:
        logger.debug(f"Resending configuration to nats")
        SingletonConfig.get_config(rebuild=True).get()  # reload configuration data
        return await self._nats_update_config_alpaca()
