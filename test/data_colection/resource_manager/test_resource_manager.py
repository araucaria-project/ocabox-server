import logging
import unittest

from obsrv.data_colection.resource_manager.resource import CameraAlpaca
from obsrv.data_colection.specialistic_components.tree_alpaca import TreeAlpacaObservatory

logger = logging.getLogger(__name__.rsplit('.')[-1])


class ResourceManagerTest(unittest.IsolatedAsyncioTestCase):
    OBSERVATORY_NAME = 'test_observatory'

    def setUp(self):
        super().setUp()
        self.tao = TreeAlpacaObservatory(component_name='sample_component', observatory_name=self.OBSERVATORY_NAME)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.rm = await self.tao.get_res_manager()

    def test_(self):
        self.assertTrue(True)


class ResourceManagerAlpacaTest(unittest.IsolatedAsyncioTestCase):
    OBSERVATORY_NAME = 'test_observatory'

    def setUp(self):
        super().setUp()
        self.tao = TreeAlpacaObservatory(component_name='sample_component', observatory_name=self.OBSERVATORY_NAME)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.rm = await self.tao.get_res_manager()

    async def test_get_resource(self):
        res = self.rm.get_resource(self.rm.CAMERA, 1)
        self.assertIsNotNone(res)
        self.assertIsInstance(res, CameraAlpaca)

    async def test_get_resource_by_source_name(self):
        origin_res = self.rm.get_resource(self.rm.CAMERA, 1)
        self.assertIsNotNone(origin_res)
        res = self.rm.get_resource_by_source_name(origin_res.source_name, self.rm.CAMERA)
        self.assertIsNotNone(res)
        self.assertIsInstance(res, CameraAlpaca)
        self.assertEqual(res, origin_res)
        res = self.rm.get_resource_by_source_name(origin_res.source_name, self.rm.DOME)
        self.assertIsNone(res)


if __name__ == '__main__':
    unittest.main()
