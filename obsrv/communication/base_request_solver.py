import asyncio
import logging
from typing import Iterable, List, Optional
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
from obsrv.tree_components.base_components.tree_component import ProvidesResponseProtocol, TreeComponent
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
        await self._nats_update_config_observatories()

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

    def _collect_telescope_entries(self) -> dict:
        """
        Walk the runtime tree and collect publishable config entries, grouped
        by telescope target. Replaces the previous class-name-whitelisted walk
        that only recognized ``TreeAlpacaObservatory``.

        Each ``TreeComponent`` can opt in via
        :meth:`TreeComponent.get_publishable_config` by returning a dict with a
        ``role`` of ``target`` (establishes a target boundary),
        ``observatory`` (contributes the ``observatory`` block), or
        ``service`` (attaches under ``services[<key>]``).

        Return shape::

            {
                "<target>": {
                    "observatory": <rare observatory dict>,  # if provided
                    "services": {                             # if any
                        "<key>": {"type": ..., "address": ..., ...},
                    },
                },
                ...
            }

        The ``observatory`` key alone (no wrapper) preserves backward
        compatibility with clients that read
        ``cfg['telescopes'][name]['observatory']``.
        """
        entries: dict = {}
        if isinstance(self.data_provider, TreeComponent):
            self._walk_publishable(self.data_provider, current_target=None, entries=entries)
        return entries

    def _walk_publishable(self, component, current_target: Optional[str], entries: dict) -> None:
        pub = None
        if isinstance(component, TreeComponent):
            pub = component.get_publishable_config()

        new_target = current_target
        if pub is not None:
            role = pub.get("role")
            if role == "target":
                new_target = pub.get("target") or new_target
            elif role == "observatory":
                # Fall back to observatory_config_name when no enclosing target
                # (e.g. unit tests that don't wrap the observatory in a provider).
                target = current_target or pub.get("observatory_config_name")
                if target is not None:
                    entries.setdefault(target, {})["observatory"] = pub.get("observatory", {})
            elif role == "service":
                if current_target is not None:
                    key = pub.get("key") or pub.get("type") or "unknown"
                    service_entry = {k: v for k, v in pub.items() if k != "role"}
                    entries.setdefault(current_target, {}).setdefault("services", {})[key] = service_entry

        for child in self._iter_tree_children(component):
            self._walk_publishable(child, new_target, entries)

    @staticmethod
    def _iter_tree_children(component) -> Iterable:
        """
        Yield immediate tree children of a component without requiring each
        class to expose a uniform interface. Covers the three child-holding
        shapes in use: ``TreeBaseBroker`` (list providers),
        ``TreeBaseBrokerDefaultTarget`` (default provider, not in list), and
        ``TreeBaseProvider`` (subcontractor).
        """
        list_providers = getattr(component, "get_list_providers", None)
        providers = list_providers() if callable(list_providers) else []
        for child in providers:
            yield child

        default_provider_getter = getattr(component, "get_default_provider", None)
        if callable(default_provider_getter):
            default = default_provider_getter()
            if default is not None and default not in providers:
                yield default

        subcontractor = getattr(component, "_subcontractor", None)
        if subcontractor is not None:
            yield subcontractor

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

    async def _nats_update_config_observatories(self) -> bool:
        publisher = get_publisher(NatsStreams.ALPACA_CONFIG)
        cfg = self._collect_telescope_entries()
        site_cfg = self._get_site_cfg()
        try:
            await publisher.publish(data={'version': "",  # todo version!
                                          'published': dt_utcnow_array(),
                                          'config': {'telescopes': cfg,
                                                     'site': site_cfg}},
                                    meta={
                                        "message_type": "config",  # IMPORTANT type message, one of pre declared types
                                        "tags": ["config_observatories"],
                                        'sender': 'Ocabox server',
                                    })
            out = True
        except MessengerNotConnected as e:
            logger.error(f"Can not publish observatory config to nats, error: {e}")
            out = False
        except TimeoutError as e:
            logger.error(f"Can not publish observatory config to nats, error: {e}")
            out = False
        return out

    async def reload_nats_config(self) -> bool:
        logger.debug(f"Resending configuration to nats")
        SingletonConfig.get_config(rebuild=True).get()  # reload configuration data
        return await self._nats_update_config_observatories()
