import asyncio
import unittest
import logging

from obsrv.data_colection.alpaca_api.connector import AlpacaConnector
from obsrv.ob_config import SingletonConfig


logger = logging.getLogger(__name__.rsplit('.')[-1])


class AlpacaConnectorTest(unittest.TestCase):

    def _setUpLoop(self):
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def setUp(self):
        super().setUp()
        self._setUpLoop()

    def _tearDownLoop(self) -> None:
        try:
            all_tasks = asyncio.all_tasks(self.loop)
            for t in all_tasks:
                t.cancel()
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            all_tasks = asyncio.all_tasks(self.loop)
            if not all_tasks:
                logger.info('All task in loop is finished')
            else:
                logger.error('Some of the tasks in current loop is still running')
                raise RuntimeError
        finally:
            asyncio.set_event_loop(None)
            self.loop.close()

    def tearDown(self) -> None:
        self._tearDownLoop()
        super().tearDown()

    def test_create_http_connection_from_sync(self):
        """
        Test create connection from synchronous method.

        :return:
        """
        alpaca = AlpacaConnector()
        self.assertIsNone(alpaca._http_session)
        alpaca.create_http_session_sync(loop=self.loop)
        self.assertIsNotNone(alpaca._http_session)

        async def coro():
            await alpaca._http_session.close()
            self.assertTrue(alpaca._http_session.closed)
        self.loop.run_until_complete(coro())

    def test_create_http_connection(self):
        """
        Test create connection from asynchronous method.

        :return:
        """
        alpaca = AlpacaConnector()
        self.assertIsNone(alpaca._http_session)

        async def coro():
            await alpaca.create_http_session()
            self.assertIsNotNone(alpaca._http_session)
            await alpaca._http_session.close()
            self.assertTrue(alpaca._http_session.closed)
        self.loop.run_until_complete(coro())

    def test_close_session(self):
        """
        Test close session

        :return:
        """
        alpaca = AlpacaConnector()

        async def coro():
            await alpaca.create_http_session()
            self.assertIsNotNone(alpaca._http_session)
            session = alpaca._http_session
            await alpaca.close()
            self.assertIsNone(alpaca._http_session)
            self.assertTrue(session.closed)
        self.loop.run_until_complete(coro())

    def test_forgot_close_session(self):
        """
        Test session closing automatically after the object has been destroyed.

        :return:
        """
        alpaca = AlpacaConnector()

        async def coro():
            await alpaca.create_http_session()
            self.assertIsNotNone(alpaca._http_session)
            session = alpaca._http_session
            alpaca.__del__()
            self.assertFalse(session.closed)
            await asyncio.sleep(0)
            self.assertTrue(session.closed)
        self.loop.run_until_complete(coro())

    def test__get(self):
        alpaca = AlpacaConnector()
        url_address_from_config = SingletonConfig.get_config()['tree']['test_observatory']['observatory']['address'].get()
        url = f"{url_address_from_config}/telescope/0/altitude?ClientID=0&ClientTransactionID=0"

        async def coro():
            await alpaca.create_http_session()
            self.assertIsNotNone(alpaca._http_session)
            # call url
            result = await alpaca._get(url)
            self.assertIsNotNone(result)
            session = alpaca._http_session
            await alpaca.close()
            self.assertIsNone(alpaca._http_session)
            self.assertTrue(session.closed)
        self.loop.run_until_complete(coro())

    def test_scan_connection(self):
        alpaca = AlpacaConnector()
        url_address_from_config = SingletonConfig.get_config()['tree']['test_observatory']['observatory']['address'].get()
        url = url_address_from_config

        async def coro():
            await alpaca.create_http_session()
            self.assertIsNotNone(alpaca._http_session)
            # call url
            result = await alpaca.scan_connection(url)
            self.assertTrue(result)
            self.assertTrue(isinstance(result, list))
            for item in result:
                self.assertTrue(item)
            session = alpaca._http_session
            await alpaca.close()
            self.assertIsNone(alpaca._http_session)
            self.assertTrue(session.closed)

        self.loop.run_until_complete(coro())


if __name__ == '__main__':
    unittest.main()
