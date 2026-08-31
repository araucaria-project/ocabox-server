"""Tests for obsrv.utils.asyncio_util_functions.wait_for_psce."""
import asyncio
import gc
import unittest

from obsrv.utils.asyncio_util_functions import wait_for_psce


class TestWaitForPsceSemantics(unittest.IsolatedAsyncioTestCase):
    """Normal (non-cancel) semantics must be unchanged."""

    async def test_result_delivery(self):
        """Returns the coroutine's result when it completes within the timeout."""
        async def coro():
            return 42

        result = await wait_for_psce(coro(), timeout=1.0)
        self.assertEqual(result, 42)

    async def test_timeout_propagates(self):
        """Raises TimeoutError when the inner coroutine exceeds the timeout."""
        async def slow():
            await asyncio.sleep(10)

        with self.assertRaises(asyncio.TimeoutError):
            await wait_for_psce(slow(), timeout=0.01)

    async def test_inner_task_cancelled_on_cancellation(self):
        """When the outer await is cancelled, the inner task must be cancelled too."""
        inner_started = asyncio.Event()
        inner_cancelled = asyncio.Event()

        async def long_running():
            inner_started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                inner_cancelled.set()
                raise

        outer = asyncio.ensure_future(wait_for_psce(long_running(), timeout=5.0))
        await inner_started.wait()
        outer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await outer
        await asyncio.sleep(0)   # let callbacks run
        self.assertTrue(inner_cancelled.is_set(), "inner task should have been cancelled")


class TestWaitForPsceNoTaskLeaks(unittest.IsolatedAsyncioTestCase):
    """The cancel-while-timeout race must not produce 'exception never retrieved' noise."""

    async def test_no_never_retrieved_on_cancel_during_timeout_race(self):
        """
        Cancel the outer shield just as the timeout budget expires.
        After a GC/settle cycle the asyncio exception handler must NOT have
        been called with a 'was never retrieved' event.
        """
        leaked_events = []

        def capturing_handler(loop, context):
            leaked_events.append(context)

        loop = asyncio.get_event_loop()
        original_handler = loop.get_exception_handler()
        loop.set_exception_handler(capturing_handler)

        try:
            async def never_completes():
                await asyncio.sleep(100)

            outer = asyncio.ensure_future(
                wait_for_psce(never_completes(), timeout=0.02)
            )
            # Cancel slightly after the timeout would fire
            await asyncio.sleep(0.03)
            outer.cancel()
            with self.assertRaises((asyncio.CancelledError, asyncio.TimeoutError)):
                await outer

            # Settle: allow done-callbacks and GC to run
            gc.collect()
            await asyncio.sleep(0)
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
