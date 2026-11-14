"""TreeNatsService — the generic NATS protocol driver, against a fake messenger.

Pins: silence verified by asking the service directly (for ever, with what it said in the error),
the end of a reader reported, address ↔ subject permutation, push into the cache, liveness by broker clock (a replay after
a TIC restart is not a sign of life), GET from the mirror, PUT as RPC with the gate parameters and
tree bookkeeping stripped, the caller and the deadline forwarded, and every failure mapped onto the
obcom error model.
"""
import asyncio
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from datamodels.messages import RpcRequest
from obcom.data_colection.address import Address, AddressError
from obcom.data_colection.coded_error import TreeOtherError
from obcom.data_colection.tree_user import TreeServiceUser
from obcom.data_colection.value import TreeValueError
from obcom.data_colection.value_call import ValueRequest
from serverish.base.datetime import dt_to_array
from serverish.base import (MessengerConnectionClosed, MessengerNotConnected, MessengerReaderConfigError,
                            MessengerRequestNoResponders, MessengerRequestTimeout)

from obsrv.tree_components.specialized_components import tree_nats_service as module
from obsrv.tree_components.specialized_components.tree_nats_service import Finding, TreeNatsService

TS = [2026, 10, 19, 4, 0, 0, 0]


def _meta(subject: str, sent: float) -> dict:
    return {'nats': {'subject': subject, 'timestamp': dt_to_array(datetime.fromtimestamp(sent, timezone.utc))}}


class _FakeReading:
    """What serverish returns from subscribe(): the task which reads, as ``.task``."""

    def __init__(self):
        self.task = asyncio.get_running_loop().create_future()


class _FakeSubscriber:
    def __init__(self, subject, deliver_policy):
        self.subject, self.deliver_policy, self.callback, self.closed = subject, deliver_policy, None, False
        self.reading = None

    async def subscribe(self, callback):
        self.callback = callback
        self.reading = _FakeReading()
        return self.reading

    async def close(self):
        self.closed = True
        self.reading.task.cancel()


class TreeNatsServiceTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.subscribers = {}
        self.requester = mock.MagicMock()
        self.requester.request = mock.AsyncMock(return_value=({'status': 'ok', 'result': {'accepted': True}}, {}))
        self.rpc_subjects = []

        def subscriber(subject, deliver_policy):
            self.subscribers[subject] = _FakeSubscriber(subject, deliver_policy)
            return self.subscribers[subject]

        def requester(subject):
            self.rpc_subjects.append(subject)
            return self.requester

        patches = [mock.patch.object(module, 'get_callbacksubscriber', side_effect=subscriber),
                   mock.patch.object(module, 'get_rpcrequester', side_effect=requester)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.sink = mock.AsyncMock()
        self.svc = TreeNatsService('nats-optics-jk15', 'optics', scope='jk15', keepalive=10)
        self.svc.set_value_sink(self.sink)
        await self.svc.run()
        self.status = self.subscribers['status.optics.jk15.>']
        self.heartbeat = self.subscribers['svc.heartbeat.optics.jk15']

    async def asyncTearDown(self):
        await self.svc.stop()

    async def _publish(self, item_path: str, value, sent: float | None = None):
        subject = f'status.optics.jk15.{item_path}'
        await self.status.callback({'value': value, 'ts': TS}, _meta(subject, sent if sent is not None else time.time()))

    def _request(self, item_path: str, request_type='GET', data=None, timeout=10.0):
        request = ValueRequest(Address(f'jk15.optics.{item_path}'), time.time(), request_type=request_type,
                               request_data=data, request_timeout=time.time() + timeout,
                               user=TreeServiceUser(name='observer'))
        request.index = 2
        return request

    # ---------------------------------------------------------------- subscriptions

    def test_subscribes_status_last_per_subject_and_heartbeat(self):
        self.assertEqual(self.status.deliver_policy, 'last_per_subject')
        self.assertEqual(self.heartbeat.deliver_policy, 'last')

    async def test_stop_closes_subscriptions(self):
        await self.svc.stop()
        self.assertTrue(self.status.closed and self.heartbeat.closed)

    # ---------------------------------------------------------------- reads

    async def test_live_status_is_pushed_to_the_cache_and_served(self):
        before = time.time()
        await self._publish('camera.sees', [{'class': 'sky.science', 'terminal': 'sky', 'via': ['tertiary']}])
        address, value = self.sink.await_args.args
        self.assertEqual(str(address), 'jk15.optics.camera.sees')
        self.assertGreaterEqual(value.ts, before)
        got = await self.svc.get_value(self._request('camera.sees'))
        self.assertEqual(got.v[0]['class'], 'sky.science')

    async def test_replay_after_restart_is_not_a_sign_of_life(self):
        an_hour_ago = time.time() - 3600
        await self._publish('camera.sees', 'dark', sent=an_hour_ago)
        _, value = self.sink.await_args.args
        self.assertAlmostEqual(value.ts, an_hour_ago, delta=1)
        with self.assertRaises(TreeOtherError) as ctx:
            await self.svc.get_value(self._request('camera.sees'))
        self.assertEqual(ctx.exception.code, 4005)
        self.assertIn('silent', ctx.exception.message)

    async def test_heartbeat_keeps_the_service_alive(self):
        await self._publish('camera.sees', 'dark', sent=time.time() - 3600)
        await self.heartbeat.callback({}, _meta('svc.heartbeat.optics.jk15', time.time()))
        self.assertEqual((await self.svc.get_value(self._request('camera.sees'))).v, 'dark')

    async def test_silent_for_three_keepalives_is_dead(self):
        await self._publish('camera.sees', 'dark', sent=time.time() - 29)
        self.assertTrue(self.svc.is_alive())
        await self._publish('camera.sees', 'dark', sent=time.time() - 31)  # older: contact stays at -29 s
        self.assertTrue(self.svc.is_alive())
        self.svc._contact = time.time() - 31
        self.assertFalse(self.svc.is_alive())

    async def test_never_heard_from(self):
        with self.assertRaises(TreeOtherError) as ctx:
            await self.svc.get_value(self._request('camera.sees'))
        self.assertIn('never heard from', ctx.exception.message)

    async def test_unpublished_item_while_alive_is_4005(self):
        await self._publish('camera.sees', 'dark')
        with self.assertRaises(TreeOtherError) as ctx:
            await self.svc.get_value(self._request('camera.route'))
        self.assertEqual(ctx.exception.code, 4005)
        self.assertIn('camera.route', ctx.exception.message)

    async def test_malformed_status_is_ignored(self):
        with self.assertLogs('tree_nats_service', level='ERROR'):
            await self.status.callback({'value': 1}, _meta('status.optics.jk15.camera.sees', time.time()))
            await self.status.callback({'value': 1, 'ts': TS}, _meta('status.optics.jk15.camera', time.time()))
        self.sink.assert_not_awaited()

    async def test_address_must_be_element_and_item(self):
        for path in ('camera', 'camera.sees.extra'):
            with self.subTest(path=path), self.assertRaises(AddressError) as ctx:
                await self.svc.get_value(self._request(path))
            self.assertEqual(ctx.exception.code, 1001)

    # ---------------------------------------------------------------- writes

    async def test_put_is_an_rpc_with_gate_and_tree_fields_stripped(self):
        data = {'function': 'object', 'request_special_permission_param': True,
                'request_safety_cutoff_bypass_param': True, 'time_of_known_change': 1.0}
        got = await self.svc.get_value(self._request('camera.setpath', 'PUT', data, timeout=10))
        self.assertEqual(got.v, {'accepted': True})
        self.assertEqual(self.rpc_subjects, ['rpc.optics.jk15.camera.setpath'])
        sent = RpcRequest.model_validate(self.requester.request.await_args.kwargs['data'])
        self.assertEqual(sent.parameters, {'function': 'object'})
        self.assertEqual(sent.caller.name, 'observer')
        self.assertTrue(0 < sent.timeout <= 8.0)
        self.assertEqual(self.requester.request.await_args.kwargs['timeout'], sent.timeout)

    async def test_put_without_data(self):
        await self.svc.get_value(self._request('self.reset', 'PUT', None))
        self.assertEqual(RpcRequest.model_validate(self.requester.request.await_args.kwargs['data']).parameters, {})

    async def test_service_refusal_keeps_its_code_and_severity(self):
        self.requester.request.return_value = ({'status': 'error', 'code': 4009, 'severity': 'NORMAL',
                                                'message': 'covercalibrator: not enabled for automatic actuation'}, {})
        with self.assertRaises(TreeOtherError) as ctx:
            await self.svc.get_value(self._request('camera.setpath', 'PUT', {'function': 'dark'}))
        self.assertEqual((ctx.exception.code, ctx.exception.severity), (4009, 'NORMAL'))
        self.assertIn('not enabled for automatic actuation', ctx.exception.message)

    async def test_error_codes_map_to_their_groups(self):
        for code, cls in ((1002, AddressError), (2001, TreeValueError), (4008, TreeOtherError)):
            with self.subTest(code=code):
                self.requester.request.return_value = ({'status': 'error', 'code': code, 'severity': 'TEMPORARY', 'message': 'x'}, {})
                with self.assertRaises(cls) as ctx:
                    await self.svc.get_value(self._request('camera.setpath', 'PUT'))
                self.assertEqual(ctx.exception.code, code)

    async def test_transport_failures(self):
        cases = ((MessengerRequestNoResponders(Exception()), 4005, 'not running'),
                 (MessengerRequestTimeout(Exception()), 4002, 'did not answer'),
                 (MessengerNotConnected('NATS not connected'), 4005, 'cannot reach'),
                 (MessengerConnectionClosed('connection closed'), 4005, 'cannot reach'))
        for exc, code, text in cases:
            with self.subTest(code=code):
                self.requester.request.side_effect = exc
                with self.assertRaises(TreeOtherError) as ctx:
                    await self.svc.get_value(self._request('camera.setpath', 'PUT'))
                self.assertEqual(ctx.exception.code, code)
                self.assertIn(text, ctx.exception.message)

    async def test_reply_outside_the_contract_is_2002(self):
        self.requester.request.return_value = ({'ok': True}, {})
        with self.assertRaises(TreeValueError) as ctx:
            await self.svc.get_value(self._request('camera.setpath', 'PUT'))
        self.assertEqual(ctx.exception.code, 2002)

    async def test_expired_deadline_is_shed_before_sending(self):
        with self.assertRaises(TreeOtherError) as ctx:
            await self.svc.get_value(self._request('camera.setpath', 'PUT', timeout=-1))
        self.assertEqual(ctx.exception.code, 4004)
        self.requester.request.assert_not_awaited()

    # ---------------------------------------------------------------- silence is a suspicion

    async def _silent_service(self, answer) -> TreeNatsService:
        """A service with a short keepalive, silent from the start, whose direct answer is ``answer``."""
        await self.svc.stop()
        if isinstance(answer, BaseException):
            self.requester.request.side_effect = answer
        self.rpc_subjects.clear()
        self.svc = TreeNatsService('nats-optics-jk15', 'optics', scope='jk15', keepalive=0.02)
        self.svc.ASK_FIRST_PAUSE, self.svc.ASK_LONGEST_PAUSE = 0.01, 0.04
        await self.svc.run()
        self.status = self.subscribers['status.optics.jk15.>']
        self.heartbeat = self.subscribers['svc.heartbeat.optics.jk15']
        return self.svc

    async def _silence_explained(self) -> str:
        async with asyncio.timeout(2):
            while self.svc._finding is None:
                await asyncio.sleep(0.005)
        with self.assertRaises(TreeOtherError) as ctx:
            await self.svc.get_value(self._request('camera.sees'))
        self.assertEqual(ctx.exception.code, 4005)
        return ctx.exception.message

    async def test_silent_service_is_asked_directly_and_the_error_says_what_it_said(self):
        cases = ((MessengerRequestNoResponders(Exception()), Finding.NOT_RUNNING, 'it is not running'),
                 (MessengerRequestTimeout(Exception()), Finding.UNANSWERED, 'does not answer'),
                 (MessengerNotConnected('NATS not connected'), Finding.UNREACHABLE, 'cannot be asked (NATS not connected)'))
        for answer, finding, text in cases:
            with self.subTest(finding=finding):
                await self._silent_service(answer)
                message = await self._silence_explained()
                self.assertIs(self.svc._finding[0], finding)
                self.assertIn('never heard from', message)
                self.assertIn(text, message)
                self.assertEqual(set(self.rpc_subjects), {'rpc.optics.jk15.self.alive'})

    async def test_service_which_answers_but_is_not_heard_is_a_loud_fault_of_the_readers(self):
        with self.assertLogs('tree_nats_service', level='ERROR') as logs:
            await self._silent_service(answer=None)      # the service answers the question
            message = await self._silence_explained()
        self.assertIs(self.svc._finding[0], Finding.UNHEARD)
        self.assertIn('its status does not reach TIC', message)
        self.assertIn("fault of TIC's NATS readers", logs.output[0])

    async def test_a_refusal_is_an_answer_too(self):
        self.requester.request.return_value = ({'status': 'error', 'code': 1002, 'severity': 'CRITICAL',
                                                'message': 'no method self.alive'}, {})
        await self._silent_service(answer=None)
        await self._silence_explained()
        self.assertIs(self.svc._finding[0], Finding.UNHEARD)

    async def test_absent_service_is_asked_for_ever_with_growing_pauses(self):
        await self._silent_service(MessengerRequestNoResponders(Exception()))
        asked = []
        self.requester.request.side_effect = lambda **kw: asked.append(time.monotonic()) or (_ for _ in ()).throw(
            MessengerRequestNoResponders(Exception()))
        await asyncio.sleep(0.4)
        self.assertGreaterEqual(len(asked), 6)                    # it did not give up
        pauses = [b - a for a, b in zip(asked, asked[1:])]
        self.assertGreater(pauses[-1], 0.03)                      # ... and slowed down to the longest pause
        self.assertLess(max(pauses), 0.2)

    async def test_asking_ends_when_the_service_is_heard_again(self):
        await self._silent_service(MessengerRequestNoResponders(Exception()))
        await self._silence_explained()
        self.svc._keepalive = 10                                  # from now on one sign of life lasts
        with self.assertLogs('tree_nats_service', level='INFO') as logs:
            await self._publish('camera.sees', 'dark')
            async with asyncio.timeout(2):
                while self.svc._finding is not None:
                    await asyncio.sleep(0.005)
        self.assertTrue(any('heard again' in line for line in logs.output))
        asked = self.requester.request.await_count
        await asyncio.sleep(0.15)
        self.assertEqual(self.requester.request.await_count, asked)
        self.assertEqual((await self.svc.get_value(self._request('camera.sees'))).v, 'dark')

    async def test_a_stopped_reader_is_reported_and_named_in_the_error(self):
        with self.assertLogs('tree_nats_service', level='ERROR') as logs:
            self.status.reading.task.set_exception(MessengerReaderConfigError("No stream stores 'status.optics.jk15.>'"))
            await asyncio.sleep(0)
        self.assertIn('the status reader of TIC stopped', logs.output[0])
        with self.assertRaises(TreeOtherError) as ctx:
            await self.svc.get_value(self._request('camera.sees'))
        self.assertIn('the status reader of TIC stopped: No stream stores', ctx.exception.message)

    async def test_state_is_not_served_after_the_status_reader_stopped_even_with_a_live_heartbeat(self):
        await self._publish('camera.sees', 'dark')
        with self.assertLogs('tree_nats_service', level='ERROR'):
            self.status.reading.task.set_exception(MessengerReaderConfigError('stream gone'))
            await asyncio.sleep(0)
        await self.heartbeat.callback({}, _meta('svc.heartbeat.optics.jk15', time.time()))
        self.assertTrue(self.svc.is_alive())
        with self.assertRaises(TreeOtherError) as ctx:
            await self.svc.get_value(self._request('camera.sees'))
        self.assertEqual(ctx.exception.code, 4005)
        self.assertIn('is not known: the status reader of TIC stopped: stream gone', ctx.exception.message)

    async def test_a_reader_cancelled_by_somebody_else_is_a_stopped_reader(self):
        await self._publish('camera.sees', 'dark')
        with self.assertLogs('tree_nats_service', level='ERROR') as logs:
            self.status.reading.task.cancel()
            await asyncio.sleep(0)
        self.assertIn('the status reader of TIC stopped: it was cancelled', logs.output[0])
        await self.heartbeat.callback({}, _meta('svc.heartbeat.optics.jk15', time.time()))
        with self.assertRaises(TreeOtherError) as ctx:
            await self.svc.get_value(self._request('camera.sees'))
        self.assertIn('it was cancelled', ctx.exception.message)

    async def test_state_is_served_after_the_heartbeat_reader_stopped_while_status_arrives(self):
        with self.assertLogs('tree_nats_service', level='ERROR') as logs:
            self.heartbeat.reading.task.set_exception(MessengerReaderConfigError('stream gone'))
            await asyncio.sleep(0)
        self.assertIn('the heartbeat reader of TIC stopped', logs.output[0])
        await self._publish('camera.sees', 'dark')         # status and its repeats are signs of life too
        self.assertEqual((await self.svc.get_value(self._request('camera.sees'))).v, 'dark')

    async def test_sign_of_life_during_the_question_leaves_no_finding(self):
        await self._silent_service(MessengerRequestNoResponders(Exception()))
        await self._silence_explained()
        self.svc._finding = None
        self.svc._keepalive = 10                                  # from now on one sign of life lasts

        async def heard_while_asked(**kwargs):
            await self._publish('camera.sees', 'dark')
            return {'status': 'ok', 'result': None}, {}

        self.requester.request.side_effect = heard_while_asked
        with self.assertNoLogs('tree_nats_service', level='ERROR'):
            async with asyncio.timeout(2):
                while not self.svc.is_alive():
                    await asyncio.sleep(0.005)
            await asyncio.sleep(0.1)
        self.assertIsNone(self.svc._finding)

    async def test_readers_closed_by_stop_are_not_reported(self):
        with self.assertNoLogs('tree_nats_service', level='ERROR'):
            await self.svc.stop()
            await asyncio.sleep(0)
        self.assertEqual(self.svc._reader_faults, {})

    # ---------------------------------------------------------------- discovery and construction

    def test_publishable_config(self):
        cfg = self.svc.get_publishable_config()
        self.assertEqual((cfg['role'], cfg['key'], cfg['address'], cfg['type']), ('service', 'optics', 'jk15.optics', 'nats_service'))
        self.assertEqual(cfg['subjects']['rpc'], 'rpc.optics.jk15.>')

    def test_realm_and_scope_must_be_subject_tokens(self):
        for realm, scope in (('optical-path', 'jk15'), ('optics', 'jk15.beso')):
            with self.subTest(realm=realm, scope=scope), self.assertRaises(ValueError):
                TreeNatsService('x', realm, scope=scope)


if __name__ == '__main__':
    unittest.main()
