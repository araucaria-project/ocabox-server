"""Publishing the observatory configuration to NATS reports any publish failure instead of raising."""
import unittest
from unittest import mock

from serverish.base import (MessengerConnectionClosed, MessengerNotConnected, MessengerPublishAckTimeout,
                            MessengerTransportError)

from obsrv.communication import base_request_solver
from obsrv.communication.base_request_solver import BaseRequestSolver


class TestNatsConfigPublish(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.solver = mock.Mock(spec=BaseRequestSolver)
        self.solver._collect_telescope_entries.return_value = {}
        self.solver._get_site_cfg.return_value = {}
        self.publisher = mock.AsyncMock()
        patcher = mock.patch.object(base_request_solver, 'get_publisher', return_value=self.publisher)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _publish(self) -> bool:
        return await BaseRequestSolver._nats_update_config_observatories(self.solver)

    async def test_published(self):
        self.assertTrue(await self._publish())
        self.publisher.publish.assert_awaited_once()

    async def test_every_serverish_failure_is_reported_not_raised(self):
        failures = (MessengerNotConnected('not connected'),
                    MessengerPublishAckTimeout('no ack'),  # escaped while nats-py TimeoutError shadowed the builtin
                    MessengerConnectionClosed('closed'),
                    MessengerTransportError('transport'))
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                self.publisher.publish.side_effect = failure
                with self.assertLogs(base_request_solver.logger, level='ERROR'):
                    self.assertFalse(await self._publish())

    async def test_programming_errors_are_not_swallowed(self):
        self.publisher.publish.side_effect = AttributeError('a bug')
        with self.assertRaises(AttributeError):
            await self._publish()


if __name__ == "__main__":
    unittest.main()
