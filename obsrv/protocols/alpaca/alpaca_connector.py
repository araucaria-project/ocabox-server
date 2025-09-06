import asyncio
import random
import aiohttp as aiohttp
import logging
from typing import Iterable, Callable, Tuple
from obsrv.protocols.alpaca.alpaca_exceptions import AlpacaError, AlpacaHttpError, RequestConnectionError, \
    AlpacaHttp400Error, AlpacaHttp500Error, AlpacaContentTypeError

logger = logging.getLogger(__name__.rsplit('.')[-1])


class Connector:
    """Base connector class for all telescope protocols."""

    async def get(self, component: 'Component', variable: str, kind=None, **data):
        raise NotImplementedError

    async def put(self, component: 'Component', variable: str, kind=None, **data):
        raise NotImplementedError

    async def call(self, component: 'Component', function: str, **data):
        raise NotImplementedError

    async def subscribe(self, variables: Iterable[Tuple[str, str]], callback: Callable):
        raise NotImplementedError

    def __del__(self):
        pass


class AlpacaConnector(Connector):

    def __init__(self, **kwargs) -> None:
        self.client_id = random.randint(0, 65535)  # alternative (0, 4294967295)
        self.session_id = 0
        self._session_loop = None
        self._http_session: aiohttp.ClientSession or None = None
        logger.info('Alpaca connector created, ClientId=%d', self.client_id)
        super().__init__(**kwargs)

    def _create_permanent_http_session(self, loop=None) -> None or aiohttp.ClientSession:
        if self._http_session:
            logger.warning(f"One session is already exist, close it before create a new one.")
            return self._http_session
        if loop:
            self._session_loop = loop
        else:
            try:
                self._session_loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.error(f"Can not create permanent session because can not find running async loop")
                return None
        self._http_session = aiohttp.ClientSession(loop=self._session_loop)
        return self._http_session

    def create_http_session_sync(self, loop):
        """
        This method is synchronous version of method 'create_http_session()'. It is not recommended to use this method
        as the connection requires a loop to work properly. It is recommended to use method create_http_session()
        instead.

        :param loop: async loop
        :return: http session or None if it cannot get or create session.
        """
        logger.warning(f"DeprecationWarning. Creating a new connection should be created within an async function, It "
                       f"is suggested to use the create_http_session() method.")
        self._create_permanent_http_session(loop=loop)

    async def create_http_session(self, loop=None) -> None or aiohttp.ClientSession:
        """
        This method create new async http session if not exists yet and return it. If the session already exists it
        will be returned. This method should be run in a running async loop. As a parameter, you can select the loop in
        which the connection is to be created.

        :param loop: async loop, by default, the currently running loop is taken into account
        :return: http session or None if it cannot get or create session.
        """
        return self._create_permanent_http_session(loop)

    async def _close_permanent_http_session(self):
        if not self._http_session:
            logger.info(f"The session is already close or never created")
            return
        await self._http_session.close()
        if self._http_session.closed:
            logger.info(f"The http session was successfully closed")
            self._http_session = None
            self._session_loop = None
        else:
            logger.error(f"the session was not closed for unknown reasons")

    async def close(self):
        """
        This method initiates a connection closure and waits for the connection to end. This is the recommended way
        to close a connection.

        :return:
        """
        await self._close_permanent_http_session()

    def is_session_closed(self):
        """
        This method check if http session is closed.

        :return: True if session is closed and False if not
        """
        if not self._http_session:
            return True
        return self._http_session.closed

    def _close_http_session(self) -> asyncio.Task or None:
        if self.is_session_closed():
            return None
        if not self._session_loop:
            # this should never happen
            logger.error(f"The session was not closed and a loop could not be found")
            return None
        logger.warning(f"Connection shutdown initiated, wait for the shutdown task to complete. It is recommended to "
                       f"close the connection manually by calling close()")
        task = self._session_loop.create_task(self.close())
        return task

    def __del__(self):
        self._close_http_session()
        super().__del__()

    async def scan_connection(self, address: str = 'http://localhost:80/api/v1'):
        """

        :param address:
        :raise AlpacaContentTypeError: if server alpaca return data in wrong format
        :raise AlpacaError: when server alpaca throws an error with a numeric value
        :raise RequestConnectionError: when can not connect to alpaca
        :return:
        """
        properties = [
            'name',
            'description',
            'connected',
            'driverinfo',
            'driverversion',
            'interfaceversion',
        ]
        from .observatory import _component_classes
        alpaca_devices = _component_classes.keys()
        devices = []

        for device in alpaca_devices:
            i = 0
            try:
                while True:
                    info = {'device': device, 'devicenumber': i}

                    list_coro = []
                    for prop in properties:
                        url = '/'.join([
                            address,
                            device,
                            str(i),
                            prop
                        ])
                        list_coro.append(self._get(url=url))

                    out = await asyncio.gather(*list_coro, return_exceptions=False)  # return_exceptions=False - one
                    # error stop all gather if true gather don't raise errors
                    for j in range(len(properties)):
                        info[properties[j]] = out[j]
                    i += 1
                    devices.append(info)
            except AlpacaHttpError:
                pass
        return devices

    async def _get(self, url, **data):
        """

        :param url: url address
        :param data: dict of parameters to pass in request
        :raise AlpacaHttp400Error: if server alpaca return 400 error
        :raise AlpacaHttp500Error: if server alpaca return 500 error
        :raise AlpacaContentTypeError: if server alpaca return data in wrong format
        :raise AlpacaError: when server alpaca throws an error with a numeric value
        :raise AlpacaHttpError: if server alpaca return unresolved error
        :raise RequestConnectionError: when can not connect to alpaca
        :return: requested value or none
        """

        async def get_response(s):
            async with s.get(url, params=data, allow_redirects=False) as response:
                await self.__check_error(response)
                r = await response.json()
                return r

        data.update(self._base_data_for_request())
        try:
            if self._http_session:
                resp = await get_response(self._http_session)
            else:
                async with aiohttp.ClientSession() as session:
                    resp = await get_response(session)
        except IOError as exc:
            logger.error(f'Connection to {url} failed')
            raise RequestConnectionError from exc
        return resp.get("Value", None)

    async def get(self, component: 'Component', variable: str, kind=None, **data):
        """
        Send an HTTP GET request to an Alpaca server and check response for errors.

        :param component: Calling component
        :param variable: Attribute to get from server
        :param kind: Different kind of component if needed
        :raise AlpacaHttp400Error: if server alpaca return 400 error
        :raise AlpacaHttp500Error: if server alpaca return 500 error
        :raise AlpacaContentTypeError: if server alpaca return data in wrong format
        :raise AlpacaError: when server alpaca throws an error with a numeric value
        :raise AlpacaHttpError: if server alpaca return unresolved error
        :raise RequestConnectionError: when can not connect to alpaca
        :return: requested value or none
        """
        url = self._url(component=component, variable=variable, kind=kind)
        return await self._get(url, **data)

    async def put(self, component: 'Component', variable: str, kind=None, **data):
        """
        Send an HTTP PUT request to an Alpaca server and check response for errors.

        :param component: Calling component
        :param variable: Attribute to get from server
        :param kind: Different kind of component if needed
        :raise AlpacaHttp400Error: if server alpaca return 400 error
        :raise AlpacaHttp500Error: if server alpaca return 500 error
        :raise AlpacaContentTypeError: if server alpaca return data in wrong format
        :raise AlpacaError: when server alpaca throws an error with a numeric value
        :raise AlpacaHttpError: if server alpaca return unresolved error
        :raise RequestConnectionError: when can not connect to alpaca
        :return: response or none
        """
        url = self._url(component=component, variable=variable, kind=kind)
        return await self._put(url, **data)

    async def _put(self, url, **data):
        """

        :param url: url address
        :param data: dict of parameters to pass in request
        :raise AlpacaHttp400Error: if server alpaca return 400 error
        :raise AlpacaHttp500Error: if server alpaca return 500 error
        :raise AlpacaContentTypeError: if server alpaca return data in wrong format
        :raise AlpacaError: when server alpaca throws an error with a numeric value
        :raise AlpacaHttpError: if server alpaca return unresolved error
        :raise RequestConnectionError: when can not connect to alpaca
        :return: requested value or none
        """

        async def get_response(s: aiohttp.ClientSession):
            async with s.put(url, data=data) as response:
                await self.__check_error(response)
                r = await response.json()
                return r

        data.update(self._base_data_for_request())
        try:
            if self._http_session:
                resp = await get_response(self._http_session)
            else:
                async with aiohttp.ClientSession() as session:
                    resp = await get_response(session)
        except IOError as exc:
            logger.error(f'Connection to {url} failed')
            raise RequestConnectionError from exc
        return resp.get("Value", None)

    async def call(self, component: 'Component', function: str, **data):
        raise NotImplementedError

    async def subscribe(self, variables: Iterable[Tuple[str, str]], callback: Callable):
        raise NotImplementedError

    def _base_data_for_request(self):
        self.session_id += 1
        return {
            'ClientID': self.client_id,
            'ClientTransactionID': self.session_id
        }

    @staticmethod
    def _url(component: 'Component', variable: str, kind=None):
        url = '/'.join([
            component.get_option_recursive('address'),
            kind if kind else component.kind,
            str(component.device_nr),
            variable
        ])
        return url

    @staticmethod
    async def __check_error(response: aiohttp.ClientResponse):
        """Check response from Alpaca server for Errors.

        :param response: Response from Alpaca server to check.
        :raise AlpacaHttp400Error: if server alpaca return 400 error
        :raise AlpacaHttp500Error: if server alpaca return 500 error
        :raise AlpacaContentTypeError: if server alpaca return data in wrong format
        :raise AlpacaError: when server alpaca throws an error with a numeric value
        :raise AlpacaHttpError: if server alpaca return unresolved error
        :return: None
        """
        try:
            url = response.url
        except Exception:
            url = 'unknown-url'
        if response.status == 400:
            logger.error(f'Alpaca HTTP 400 error, ({response.reason}) for {url}')
            raise AlpacaHttp400Error(response.reason)
        elif response.status == 500:
            logger.error(f'Alpaca HTTP 500 error, ({response.reason}) for {url}')
            raise AlpacaHttp500Error(response.reason)
        # other errors like for example 404
        try:
            response.raise_for_status()
        except aiohttp.ClientResponseError as e:
            logger.error(f'Alpaca HTTP {e.status} error for {e.request_info.real_url}')
            raise AlpacaHttpError(str(e.message))
        # try to convert to json and get errors
        try:
            j = await response.json()
        except aiohttp.ContentTypeError as e:
            logger.error(f'Alpaca content type error. Status {response.status} error for {url}')
            raise AlpacaContentTypeError from e
        if j["ErrorNumber"] != 0:
            logger.error(f'Alpaca error, code={j["ErrorNumber"]}, msg={j["ErrorMessage"]} for {url}')
            raise AlpacaError(j["ErrorNumber"], j["ErrorMessage"])


# ALPACA-specific connector implementation only
# Factory logic moved to protocols/connector_factory.py
