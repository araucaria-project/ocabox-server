"""The TPL2 client against the fake Pilar: parsing, replies by command id, late replies, pool, breaker."""
import asyncio
import unittest

from obsrv.protocols.pilar.pilar_tpl2 import (Tpl2Busy, Tpl2Connection, Tpl2Item, Tpl2LinkError, Tpl2Pool,
                                               Tpl2Special, format_value, parse_value, unescape)
from test.protocols.pilar.fake_pilar import FakePilar


class Tpl2ValuesTest(unittest.TestCase):

    def test_parse_value(self):
        self.assertEqual(parse_value('12.5'), 12.5)
        self.assertEqual(parse_value('-3'), -3)
        self.assertIsInstance(parse_value('-3'), int)
        self.assertEqual(parse_value('"ERR_USV_BatteryAlarm [W] (CABINET) USV\\| Alarm"'),
                         'ERR_USV_BatteryAlarm [W] (CABINET) USV| Alarm')
        self.assertEqual(parse_value('FAILED'), Tpl2Special('FAILED'))
        self.assertEqual(parse_value('LOCKEDBY 3199750636807'), Tpl2Special('LOCKEDBY', '3199750636807'))
        self.assertEqual(parse_value('HORIZONTAL'), 'HORIZONTAL')

    def test_unescape_and_format(self):
        self.assertEqual(unescape('a\\"b\\\\c'), 'a"b\\c')
        self.assertEqual(format_value(True), '1')
        self.assertEqual(format_value(4), '4')
        self.assertEqual(format_value(12.25), '12.25')
        self.assertEqual(format_value('a"b'), '"a\\"b"')


class Tpl2ConnectionTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.fake = await FakePilar.start()
        host, port = self.fake.address.split(':')
        self.host, self.port = host, int(port)

    async def asyncTearDown(self):
        await self.fake.close()

    async def connect(self, name='t') -> Tpl2Connection:
        return await Tpl2Connection.open(self.host, self.port, name=name, encoding='latin-1', id_range=(101, 200),
                                         connect_timeout=1.0, greeting_timeout=1.0)

    async def test_greeting_get_and_set(self):
        conn = await self.connect()
        self.assertTrue(conn.greeting.startswith('TPL2'))
        reply = await conn.send('GET POSITION.HORIZONTAL.AZ;TELESCOPE.STATUS.ERROR_LIST;NO.SUCH.VAR').future
        self.assertEqual(reply.item('position.horizontal.az').value, 180.0)
        self.assertIn('BatteryAlarm', reply.item('TELESCOPE.STATUS.ERROR_LIST').value)
        self.assertEqual(reply.item('NO.SUCH.VAR').special.kind, 'UNKNOWN')
        reply = await conn.send('SET NO.SUCH.VAR=1').future
        self.assertEqual(reply.item('NO.SUCH.VAR').special.kind, 'UNKNOWN')
        reply = await conn.send('BOGUS').future
        self.assertEqual(reply.command_error, 'UNKNOWN')
        self.assertTrue(reply.failed)
        conn.close()

    async def test_failed_set_carries_the_event_text(self):
        fake = self.fake
        fake.powered, fake.ready_frac = False, 0.0
        conn = await self.connect()
        reply = await conn.send('SET POINTING.TRACK=1').future
        item = reply.item('POINTING.TRACK')
        self.assertEqual(item.special.kind, 'FAILED')
        self.assertEqual(item.message, 'Telescope not ready')
        conn.close()

    async def test_late_reply_does_not_mix_with_the_next_command(self):
        conn = await self.connect()
        self.fake.freeze(0.4)
        await asyncio.sleep(0.05)
        request = conn.send('GET POSITION.HORIZONTAL.ALT')
        done, _ = await asyncio.wait({request.future}, timeout=0.1)
        self.assertFalse(done)
        conn.abandon(request)
        self.assertFalse(conn.idle)                 # the late reply is still coming
        await asyncio.sleep(0.6)
        self.assertTrue(conn.idle)
        reply = await conn.send('GET POSITION.HORIZONTAL.AZ').future
        self.assertEqual(list(reply.items), ['POSITION.HORIZONTAL.AZ'])
        conn.close()

    async def test_too_many_clients(self):
        self.fake.max_clients = 1
        conn = await self.connect()
        with self.assertRaises(Tpl2LinkError):
            await self.connect('second')
        conn.close()

    async def test_server_closing_fails_the_waiting_command(self):
        conn = await self.connect()
        self.fake.freeze(1.0)
        await asyncio.sleep(0.05)
        request = conn.send('GET POSITION.HORIZONTAL.ALT')
        for writer in list(self.fake.writers):
            writer.close()
        with self.assertRaises(Tpl2LinkError):
            await asyncio.wait_for(request.future, 1.0)
        self.assertTrue(conn.closed)


class Tpl2PoolTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.fake = await FakePilar.start()
        host, port = self.fake.address.split(':')
        self.pool = Tpl2Pool(host, int(port), size=4, reserved=1, long_max=1, id_range=(101, 200),
                             encoding='latin-1', connect_timeout=1.0, greeting_timeout=1.0, wait_timeout=0.3,
                             cooldown=0.5, stalled_limit=5.0)

    async def asyncTearDown(self):
        self.pool.close()
        await self.fake.close()

    async def test_general_connections_and_the_reserved_one(self):
        async with self.pool.lease() as a, self.pool.lease() as b:
            self.assertIsNot(a, b)
            with self.assertRaises(Tpl2Busy):
                async with self.pool.lease():
                    pass
            async with self.pool.lease('priority') as c:       # the reserved connection is still free
                reply = await c.send('GET POSITION.HORIZONTAL.AZ').future
                self.assertTrue(reply.item('POSITION.HORIZONTAL.AZ').ok)
        self.assertLessEqual(self.pool.open_connections(), 3)

    async def test_long_connection_is_closed_after_use(self):
        async with self.pool.lease('long') as conn:
            await conn.send('GET POSITION.HORIZONTAL.AZ').future
        await asyncio.sleep(0.05)
        self.assertEqual(self.pool.open_connections(), 0)
        self.assertTrue(conn.closed)

    async def test_breaker(self):
        await self.fake.close()
        with self.assertRaises(Tpl2LinkError):
            async with self.pool.lease():
                pass
        self.assertTrue(self.pool.down and self.pool.outage)
        with self.assertRaises(Tpl2LinkError) as ctx:          # no new attempt while the breaker is open
            async with self.pool.lease():
                pass
        self.assertIn('next attempt', str(ctx.exception))


class Tpl2ItemTest(unittest.TestCase):

    def test_ok(self):
        self.assertTrue(Tpl2Item('X', 1).ok)
        self.assertFalse(Tpl2Item('X', None, Tpl2Special('FAILED')).ok)


if __name__ == '__main__':
    unittest.main()
