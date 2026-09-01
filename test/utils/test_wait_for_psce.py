"""Cancellation-semantics tests for the asyncio.timeout pattern.

These tests replace the former wait_for_psce tests after the migration to
``asyncio.timeout`` (py ≥ 3.11).  The middle-task leak that test_wait_for_psce
previously guarded against no longer exists — there is no middle task.  A
regression test is kept that asserts no ``Task exception was never retrieved``
events fire when a cancelled awaitable raises from its teardown.
"""
import asyncio
import contextlib
import gc
import unittest


class TestAsyncioTimeoutSemantics(unittest.IsolatedAsyncioTestCase):
    """Normal (non-cancel) semantics of the asyncio.timeout pattern."""

    async def test_result_delivery(self):
        """Returns the coroutine's result when it completes within the timeout."""
        async def coro():
            return 42

        async with asyncio.timeout(1.0):
            result = await coro()
        self.assertEqual(result, 42)

    async def test_timeout_propagates(self):
        """Raises TimeoutError when the inner coroutine exceeds the timeout."""
        async def slow():
            await asyncio.sleep(10)

        with self.assertRaises(asyncio.TimeoutError):
            async with asyncio.timeout(0.01):
                await slow()

    async def test_inner_task_cancelled_on_cancellation(self):
        """When the outer await is cancelled, the inner coroutine must be cancelled too."""
        inner_started = asyncio.Event()
        inner_cancelled = asyncio.Event()

        async def long_running():
            inner_started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                inner_cancelled.set()
                raise

        async def wrapper():
            async with asyncio.timeout(5.0):
                await long_running()

        outer = asyncio.ensure_future(wrapper())
        await inner_started.wait()
        outer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await outer
        await asyncio.sleep(0)   # let callbacks run
        self.assertTrue(inner_cancelled.is_set(), "inner coroutine should have been cancelled")


class TestNoTaskLeaksOnCancelDuringTimeout(unittest.IsolatedAsyncioTestCase):
    """Regression: no 'Task exception was never retrieved' noise with asyncio.timeout."""

    async def test_no_never_retrieved_on_cancel_during_timeout_race(self):
        """
        Cancel an asyncio.timeout-wrapped coroutine whose teardown converts
        CancelledError into TimeoutError (the aiohttp pattern seen on dead Alpaca
        hosts).  After dropping all references and running a GC/settle cycle the
        asyncio exception handler must NOT have been called with a
        'was never retrieved' event.

        With asyncio.timeout there is no middle task, so nothing can be orphaned.
        """
        leaked_events = []

        def capturing_handler(loop, context):
            leaked_events.append(context)

        loop = asyncio.get_event_loop()
        original_handler = loop.get_exception_handler()
        loop.set_exception_handler(capturing_handler)

        try:
            async def aiohttp_style_refresh():
                """Simulates aiohttp converting CancelledError → TimeoutError during teardown."""
                try:
                    await asyncio.sleep(100)
                except asyncio.CancelledError:
                    raise TimeoutError("aiohttp-style teardown")

            async def wrapper():
                async with asyncio.timeout(50):
                    await aiohttp_style_refresh()

            outer = asyncio.ensure_future(wrapper())
            # Cancel BEFORE the timeout fires
            await asyncio.sleep(0.01)
            outer.cancel()
            with contextlib.suppress(BaseException):
                await outer
            del outer   # drop the masking reference so Task.__del__ can fire

            # Settle: allow done-callbacks and GC to trigger Task.__del__
            for _ in range(4):
                gc.collect()
                await asyncio.sleep(0)

        finally:
            loop.set_exception_handler(original_handler)

        never_retrieved = [
            ctx for ctx in leaked_events
            if "was never retrieved" in ctx.get("message", "")
        ]
        self.assertEqual(
            never_retrieved,
            [],
            f"'exception never retrieved' events fired: {never_retrieved}",
        )


if __name__ == "__main__":
    unittest.main()
