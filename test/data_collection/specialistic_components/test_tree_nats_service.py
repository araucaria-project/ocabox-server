"""TreeNatsService — the generic NATS protocol driver, against a fake messenger.

Pins: address ↔ subject permutation, push into the cache, liveness by broker clock (a replay after
a TIC restart is not a sign of life), GET from the mirror, PUT as RPC with the gate parameters and
tree bookkeeping stripped, the caller and the deadline forwarded, and every failure mapped onto the
obcom error model.
"""
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
from serverish.messenger.msg_rpc_req import MessengerRequestNoResponders, MessengerRequestTimeout

from obsrv.tree_components.specialized_components import tree_nats_service as module
from obsrv.tree_components.specialized_components.tree_nats_service import TreeNatsService

TS = [2026, 10, 19, 4, 0, 0, 0]


def _meta(subject: str, sent: float) -> dict:
    return {'nats': {'subject': subject, 'timestamp': dt_to_array(datetime.fromtimestamp(sent, timezone.utc))}}


class _FakeSubscriber:
    def __init__(self, subject, deliver_policy):
        self.subject, self.deliver_policy, self.callback, self.closed = subject, deliver_policy, None, False

    async def subscribe(self, callback):
        self.callback = callback

    async def close(self):
        self.closed = True


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
                 (MessengerRequestTimeout(Exception()), 4002, 'did not answer'))
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
