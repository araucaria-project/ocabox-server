"""Regression: instrument connectors must NOT silently swallow transient
TCP errors. ECONNREFUSED, broken pipe, and timeouts against the device
must surface as ``TreeOtherError(4005, NORMAL)`` so cycle-query
subscribers running ``ErrorPolicy.SERVICE`` (PMS-style daemons)
auto-recover when the device returns. Device-replied errors (raised as
``RuntimeError`` inside the connector) must surface as
``TreeValueError(2002, NORMAL)`` rather than being swallowed.

See ``doc/errors.md`` "TEMPORARY vs NORMAL — blip vs sustained" for the
convention and issue #20 for the failure mode this test guards against.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from obcom.data_colection.coded_error import TreeOtherError
from obcom.data_colection.response_error import ResponseError
from obcom.data_colection.value import TreeValueError
from obsrv.protocols.iris_ccd.iris_ccd_connector import IrisCcdConnector
from obsrv.protocols.pilar.pilar_connector import PilarConnector


def _make_iris_connector() -> IrisCcdConnector:
    """Build an IrisCcdConnector skipping cfg load — only the fields we
    exercise in the get/put/call paths need to be set."""
    c = IrisCcdConnector.__new__(IrisCcdConnector)
    c._command_map = {
        'camera': {
            'camerastate': {'command': 'STATUS'},
            'binx': {'command': 'BINX'},
        },
    }
    c._actions_map = {
        'startexposure': [{'command': 'EXPOSE', 'value': '{duration}'}],
    }
    return c


def _make_iris_component(kind: str = 'camera', address: str = '127.0.0.1:8888') -> MagicMock:
    component = MagicMock()
    component.kind = kind
    component.sys_id = f'iris.{kind}'
    component.get_option_recursive.return_value = address
    return component


class IrisCcdTransientErrorsTest(unittest.IsolatedAsyncioTestCase):
    async def test_get_connection_refused_raises_4005_normal(self):
        connector = _make_iris_connector()
        connector._execute_command = AsyncMock(
            side_effect=ConnectionRefusedError(111, 'Connection refused')
        )
        with self.assertRaises(TreeOtherError) as ctx:
            await connector.get(_make_iris_component(), 'camerastate')
        self.assertEqual(ctx.exception.code, 4005)
        self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_NORMAL)

    async def test_get_timeout_raises_4005_normal(self):
        connector = _make_iris_connector()
        connector._execute_command = AsyncMock(side_effect=asyncio.TimeoutError())
        with self.assertRaises(TreeOtherError) as ctx:
            await connector.get(_make_iris_component(), 'camerastate')
        self.assertEqual(ctx.exception.code, 4005)
        self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_NORMAL)

    async def test_get_broken_pipe_raises_4005_normal(self):
        connector = _make_iris_connector()
        connector._execute_command = AsyncMock(side_effect=BrokenPipeError())
        with self.assertRaises(TreeOtherError) as ctx:
            await connector.get(_make_iris_component(), 'camerastate')
        self.assertEqual(ctx.exception.code, 4005)
        self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_NORMAL)

    async def test_get_runtime_error_raises_2002_normal(self):
        """Device-replied error (RuntimeError raised inside _execute_command
        when the device returns non-OKAY) surfaces as 2002 NORMAL — real
        instrument-state failure, retryable per client ErrorPolicy."""
        connector = _make_iris_connector()
        connector._execute_command = AsyncMock(
            side_effect=RuntimeError('IRIS CCD error: PARAM_OUT_OF_RANGE')
        )
        with self.assertRaises(TreeValueError) as ctx:
            await connector.get(_make_iris_component(), 'camerastate')
        self.assertEqual(ctx.exception.code, 2002)
        self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_NORMAL)

    async def test_put_connection_refused_raises_4005_normal(self):
        connector = _make_iris_connector()
        connector._execute_command = AsyncMock(
            side_effect=ConnectionRefusedError(111, 'Connection refused')
        )
        with self.assertRaises(TreeOtherError) as ctx:
            await connector.put(_make_iris_component(), 'binx', value=2)
        self.assertEqual(ctx.exception.code, 4005)
        self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_NORMAL)

    async def test_put_runtime_error_raises_2002_normal(self):
        connector = _make_iris_connector()
        connector._execute_command = AsyncMock(
            side_effect=RuntimeError('IRIS CCD error: BUSY')
        )
        with self.assertRaises(TreeValueError) as ctx:
            await connector.put(_make_iris_component(), 'binx', value=2)
        self.assertEqual(ctx.exception.code, 2002)
        self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_NORMAL)

    async def test_call_connection_refused_raises_4005_normal(self):
        connector = _make_iris_connector()
        connector._execute_command = AsyncMock(
            side_effect=ConnectionRefusedError(111, 'Connection refused')
        )
        with self.assertRaises(TreeOtherError) as ctx:
            await connector.call(_make_iris_component(), 'startexposure', duration=1.0)
        self.assertEqual(ctx.exception.code, 4005)
        self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_NORMAL)


def _make_pilar_connector() -> PilarConnector:
    c = PilarConnector.__new__(PilarConnector)
    c._command_map = {'mount': {'rightascension': 'POSITION.MOUNT.RA'}}
    c._actions_map = {}
    c._timeouts = {'get': 1.0, 'set': 1.0}
    c._focuser_multiplier = 1
    return c


class PilarTransientErrorsTest(unittest.IsolatedAsyncioTestCase):
    """Pilar surfaces the same ``_TEMPORARY_IO_ERRORS`` family — verify
    severity matches iris (NORMAL, not TEMPORARY) so the convention is
    consistent across instrument connectors."""

    async def test_get_connection_refused_raises_4005_normal(self):
        connector = _make_pilar_connector()
        connector._get_connection_resources = AsyncMock(
            side_effect=ConnectionError('Pilar at 127.0.0.1:65432 not reachable')
        )
        connector._is_outage = MagicMock(return_value=True)  # suppress the warning log
        component = MagicMock()
        component.kind = 'mount'
        component.sys_id = 'pilar.mount'
        component.get_option_recursive.return_value = '127.0.0.1:65432'

        with self.assertRaises(TreeOtherError) as ctx:
            await connector.get(component, 'rightascension')
        self.assertEqual(ctx.exception.code, 4005)
        self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_NORMAL)

    async def test_get_timeout_raises_4005_normal(self):
        connector = _make_pilar_connector()
        connector._get_connection_resources = AsyncMock(side_effect=asyncio.TimeoutError())
        connector._is_outage = MagicMock(return_value=True)
        component = MagicMock()
        component.kind = 'mount'
        component.sys_id = 'pilar.mount'
        component.get_option_recursive.return_value = '127.0.0.1:65432'

        with self.assertRaises(TreeOtherError) as ctx:
            await connector.get(component, 'rightascension')
        self.assertEqual(ctx.exception.code, 4005)
        self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_NORMAL)
