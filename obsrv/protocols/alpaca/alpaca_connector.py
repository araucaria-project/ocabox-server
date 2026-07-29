import asyncio
import random
import aiohttp as aiohttp
import logging
from typing import Iterable, Callable, Tuple

from aiohttp import ServerConnectionError, ClientConnectionError
from aiohttp.resolver import DefaultResolver
from obcom.data_colection.address import AddressError
from obcom.data_colection.coded_error import TreeOtherError
from obcom.data_colection.value import TreeValueError

from obsrv.protocols.alpaca.alpaca_exceptions import AlpacaError, AlpacaHttpError, RequestConnectionError, \
    AlpacaHttp400Error, AlpacaHttp500Error, AlpacaContentTypeError

logger = logging.getLogger(__name__.rsplit('.')[-1])


# Vendor SDK error codes that map to TreeOtherError(4008) "device busy".
# 20072 = Andor DRV_ACQUIRING (acquisition in progress).
_DEVICE_BUSY_ERRNOS = frozenset({20072})

# --- HTTP session tuning -----------------------------------------------------
# A single long-lived ClientSession per connector (instead of a fresh session per
# request) gives us HTTP keep-alive and a connector-level DNS cache. Together they
# collapse the per-request getaddrinfo storm that used to saturate the resolver
# thread-pool: a brief DNS hiccup on the upstream server would block every poll and
# the process never recovered without a restart. See doc/errors.md and the
# DNS-instability report for the full failure analysis.
#
# Per-request timeout. aiohttp's default is 5 minutes, which lets a slow/hung
# resolve or connect occupy a connection (and a resolver thread) far too long.
# ALPACA telemetry resolves in well under a second; 10s total / 5s connect is
# generous but bounded.
_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=10.0, connect=5.0, sock_connect=5.0)
# Cap concurrent connections to a single ALPACA host. Keeps the per-host driver
# queue shallow (also mitigates the ASCOM filterwheel "code=1026" queue-depth
# timeouts) while leaving ample parallelism for normal telemetry polling.
_DEFAULT_LIMIT_PER_HOST = 8
# Connector-level DNS cache lifetime (seconds). Resolutions are reused for this
# long instead of hitting the resolver on every request.
_DEFAULT_DNS_CACHE_TTL = 30


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
        # Guards lazy creation of the shared session so concurrent first requests
        # don't each spin up their own. Bound to the running loop on first await.
        self._session_lock = asyncio.Lock()
        logger.info('Alpaca connector created, ClientId=%d', self.client_id)
        super().__init__(**kwargs)

    def _create_permanent_http_session(self, loop=None) -> None or aiohttp.ClientSession:
        if self._http_session and not self._http_session.closed:
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
        # The TCPConnector binds to the running loop; keep-alive + DNS cache live here.
        # DefaultResolver is the async (c-ares) resolver when aiodns is installed,
        # falling back to the blocking ThreadedResolver otherwise.
        connector = aiohttp.TCPConnector(
            limit_per_host=_DEFAULT_LIMIT_PER_HOST,
            use_dns_cache=True,
            ttl_dns_cache=_DEFAULT_DNS_CACHE_TTL,
        )
        self._http_session = aiohttp.ClientSession(connector=connector, timeout=_DEFAULT_TIMEOUT)
        logger.info("Alpaca HTTP session created (limit_per_host=%d, ttl_dns_cache=%.0fs, "
                    "timeout=%.0fs/connect=%.0fs, resolver=%s)",
                    _DEFAULT_LIMIT_PER_HOST, _DEFAULT_DNS_CACHE_TTL,
                    _DEFAULT_TIMEOUT.total, _DEFAULT_TIMEOUT.connect, DefaultResolver.__name__)
        return self._http_session

    async def _ensure_session(self) -> aiohttp.ClientSession or None:
        """Return the shared HTTP session, creating it lazily on first use.

        Connectors are built synchronously at tree-build time (no running loop),
        so the session can only be created once requests start flowing inside the
        loop. The lock collapses a thundering-herd of concurrent first requests
        into a single session creation.
        """
        if self._http_session is not None and not self._http_session.closed:
            return self._http_session
        async with self._session_lock:
            if self._http_session is None or self._http_session.closed:
                self._create_permanent_http_session()
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
        from obsrv.telescope_devices.device_tree import _component_classes
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
        session = await self._ensure_session()
        try:
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
        url = None
        try:
            url = self._url(component=component, variable=variable, kind=kind)
            resp = await self._get(url, **data)
            return resp
        except Exception as e:
            self.raise_tree_exeption(e, address=url)

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

        url = None
        try:
            url = self._url(component=component, variable=variable, kind=kind)
            resp = await self._put(url, **data)
            return resp
        except Exception as e:
            self.raise_tree_exeption(e, address=url)

    def raise_tree_exeption(self, exception: Exception, address: str | None):
        try:
            raise exception
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
            logger.warning(f"Alpaca numeric error {e.error_number} for request {address}: {e.message}")
            if e.error_number in _DEVICE_BUSY_ERRNOS:
                raise TreeOtherError(address=None, code=4008, message=e.message,
                                     severity=TreeOtherError.SEVERITY_TEMPORARY)
            # Device/driver reported a fault (e.g. ASCOM InvalidOperationException
            # 1035 "Telescope is not ready, please clear Error"). TIC worked; the
            # device said no. Surface as 4009 carrying the driver's numeric code so
            # the client distinguishes this from a TIC-internal value-build failure
            # (2002) and reads the errno without parsing the message string.
            raise TreeOtherError(address=None, code=4009, message=e.message,
                                 severity=TreeOtherError.SEVERITY_NORMAL,
                                 device_errno=e.error_number)
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
        session = await self._ensure_session()
        try:
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
