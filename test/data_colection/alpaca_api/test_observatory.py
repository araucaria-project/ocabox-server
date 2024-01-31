import random
import unittest
import logging

from obsrv.data_colection.alpaca_api.observatory import Observatory

logger = logging.getLogger(__name__.rsplit('.')[-1])


class ObservatoryTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        super().setUp()
        self.observatory = Observatory()
        self.observatory.connect(['tree','test_observatory'])

    async def test_get_from_alpaca(self):
        """Test get sample data from alpaca with no process"""
        tel = self.observatory.dibi
        value = await tel.get('name')
        right_value = await tel._get("name")
        self.assertEqual(value, right_value)


class TelescopeTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        super().setUp()
        self.observatory = Observatory()
        self.observatory.connect(['tree','test_observatory'])

    async def test_get_rightascension(self):
        tel = self.observatory.dibi
        method_name = 'rightascension'
        value = await tel.get(method_name)
        right_value = await tel._get(method_name) / 24 * 360
        self.assertEqual(value, right_value)

    async def test_put_targetdeclination(self):
        tel = self.observatory.dibi
        method_name = 'targetdeclination'
        sample_value = random.random() + 1
        sample_value_str = f'-{int(random.random()*10)}:{int(random.random()*10)}.{int(random.random()*10)}'

        await tel.put(method_name, TargetDeclination=sample_value)
        right_value = await tel._get(method_name)
        data = tel._target_declination_processor(None, TargetDeclination=sample_value)
        self.assertAlmostEqual(data.get('TargetDeclination', None), right_value)

        await tel.put(method_name, TargetDeclination=sample_value_str)
        right_value = await tel._get(method_name)
        data = tel._target_declination_processor(None, TargetDeclination=sample_value_str)
        self.assertAlmostEqual(data.get('TargetDeclination', None), right_value)

    async def test_get_targetrightascension(self):
        tel = self.observatory.dibi
        method_name = 'targetrightascension'
        sample_value = random.random() + 1
        # set some value because is empty when starting
        await tel._put(method_name, TargetRightAscension=sample_value)
        value = await tel.get(method_name)
        right_value = await tel._get(method_name) / 24 * 360
        self.assertEqual(value, right_value)

    async def test_set_targetrightascension(self):
        tel = self.observatory.dibi
        method_name = 'targetrightascension'
        sample_value = random.random() + 1
        sample_value_str = f'{int(random.random() * 10)}:{int(random.random() * 10)}.{int(random.random() * 10)}'
        await tel.put(method_name, TargetRightAscension=sample_value)
        right_value = await tel._get(method_name)
        data = tel._target_rightascension_processor(None, TargetRightAscension=sample_value)
        self.assertAlmostEqual(data.get('TargetRightAscension', None), right_value)

        await tel.put(method_name, TargetRightAscension=sample_value_str)
        right_value = await tel._get(method_name)
        data = tel._target_rightascension_processor(None, TargetRightAscension=sample_value_str)
        self.assertAlmostEqual(data.get('TargetRightAscension', None), right_value)


if __name__ == '__main__':
    unittest.main()
