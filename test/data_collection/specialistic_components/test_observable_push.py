"""Push-on-change semantics for local-source values.

Verifies that mutating the safety-cutoff state through the public API
(``engage_safety_cutoff``) wakes a registered change-notifier within an
event-loop tick, rather than within ``time_of_data_tolerance`` of the
freezer. Detects missing ``set_change_notifier`` wiring and regressions
in the Observable[T] coalescing / equality / unsubscribe semantics.

Companion to ``Observable Pattern for ocabox-server`` in the ecosystem
vault.
"""
from __future__ import annotations

import asyncio
import time
import unittest

from obsrv.tree_components.specialized_components import TreeBaseRequestBlocker
from obsrv.tree_components.specialized_components import TreeBlockerAccessGrantor
from obsrv.utils.observable import Observable
from obsrv.utils.push_wiring import assert_push_wiring


class ObservableSemanticsTest(unittest.IsolatedAsyncioTestCase):
    async def test_set_coalesces_within_tick(self):
        obs: Observable[int] = Observable(0)
        fired: list[int] = []

        async def watcher(v: int) -> None:
            fired.append(v)

        obs.subscribe(watcher)
        obs.set(1)
        obs.set(2)
        obs.set(3)
        # One tick allows ``call_soon`` to fire ``_fire``; another tick lets
        # the async watcher's ``create_task`` body run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(fired, [3])

    async def test_equality_guards_no_op_set(self):
        obs: Observable[int] = Observable(7)
        fired: list[int] = []
        obs.subscribe(lambda v: fired.append(v))
        obs.set(7)
        await asyncio.sleep(0)
        self.assertEqual(fired, [])

    async def test_touch_fires_without_changing_value(self):
        obs: Observable[list[int]] = Observable([])
        fired: list[list[int]] = []
        obs.subscribe(lambda v: fired.append(list(v)))
        obs.get().append(42)  # in-place mutation — equality-guard would suppress
        obs.touch()
        await asyncio.sleep(0)
        self.assertEqual(fired, [[42]])

    async def test_unsubscribe_is_idempotent(self):
        obs: Observable[int] = Observable(0)
        fired: list[int] = []
        unsub = obs.subscribe(lambda v: fired.append(v))
        unsub()
        unsub()  # idempotent — second call must not raise
        obs.set(1)
        await asyncio.sleep(0)
        self.assertEqual(fired, [])

    def test_set_outside_running_loop_raises(self):
        obs: Observable[int] = Observable(0)
        with self.assertRaises(RuntimeError):
            obs.set(1)


class SafetyCutoffPushWakeupTest(unittest.IsolatedAsyncioTestCase):
    """End-to-end check: engage_safety_cutoff() wakes the grantor's
    change-notifier within an event-loop tick.

    This is the property that turns "polite retry-forever / t_tolerance
    polling" into "single CRITICAL terminal callback / instant push".
    """

    def setUp(self) -> None:
        super().setUp()
        self.blocker = TreeBaseRequestBlocker(component_name='blocker_under_test')
        self.grantor = TreeBlockerAccessGrantor(
            component_name='grantor_under_test',
            source_name='access_grantor',
            target_blocker=self.blocker,
        )

    async def test_engage_fires_notifier_within_tick(self):
        notifier_calls: list[float] = []

        async def notifier() -> None:
            notifier_calls.append(time.monotonic())

        self.grantor.set_change_notifier(notifier)
        t0 = time.monotonic()
        self.blocker.engage_safety_cutoff()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(len(notifier_calls), 1, 'notifier should fire exactly once')
        self.assertLess(notifier_calls[0] - t0, 0.05,
                        'notifier should fire within ~50ms of state change')

    async def test_disengage_also_fires(self):
        self.blocker.engage_safety_cutoff()  # set initial state True
        notifier_calls: list[float] = []

        async def notifier() -> None:
            notifier_calls.append(time.monotonic())

        self.grantor.set_change_notifier(notifier)
        # First tick: drain any pending fire from the engage above.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        notifier_calls.clear()

        self.blocker.disengage_safety_cutoff()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(len(notifier_calls), 1)

    async def test_idempotent_engage_does_not_fire(self):
        self.blocker.engage_safety_cutoff()
        notifier_calls: list[float] = []

        async def notifier() -> None:
            notifier_calls.append(time.monotonic())

        self.grantor.set_change_notifier(notifier)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        notifier_calls.clear()

        # Engaging again with the same value is a no-op (Observable equality guard).
        self.blocker.engage_safety_cutoff()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(notifier_calls, [], 'no-op set must not fire watchers')


class AssertPushWiringTest(unittest.TestCase):
    """The ``assert_push_wiring`` invariant: every PUSH_DRIVEN provider
    in the tree must have ``set_change_notifier`` called on it during
    assembly. Catches "added a push-driven provider, forgot the wiring"
    at server start instead of at runtime via t_tolerance polling."""

    def test_unwired_push_driven_provider_fails(self):
        blocker = TreeBaseRequestBlocker(component_name='blocker')
        grantor = TreeBlockerAccessGrantor(
            component_name='grantor',
            source_name='access_grantor',
            target_blocker=blocker,
        )
        # No set_change_notifier called.
        with self.assertRaises(AssertionError) as ctx:
            assert_push_wiring(grantor)
        self.assertIn('grantor', str(ctx.exception))
        self.assertIn('TreeBlockerAccessGrantor', str(ctx.exception))

    def test_wired_push_driven_provider_passes(self):
        blocker = TreeBaseRequestBlocker(component_name='blocker')
        grantor = TreeBlockerAccessGrantor(
            component_name='grantor',
            source_name='access_grantor',
            target_blocker=blocker,
        )

        async def notify() -> None:
            return None

        grantor.set_change_notifier(notify)
        # Should not raise.
        assert_push_wiring(grantor)
