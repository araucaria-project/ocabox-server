import logging
import unittest
from obsrv.comunication.base_router_with_config import BaseRouterWithConfig

logger = logging.getLogger(__name__.rsplit('.')[-1])


class BaseRouterWithConfigTest(unittest.TestCase):

    class SampleBaseRouterWithConfig(BaseRouterWithConfig):
        DEFAULT_NAME = 'DefaultRouterTest'
        TYPE = 'router'

    def test_get_cfg(self):
        """Test method get_cfg()"""
        bco = self.SampleBaseRouterWithConfig(name='SampleTestRouter')
        self.assertEqual(bco.get_cfg("protocol"), "tcp")
        self.assertIsNone(bco.get_cfg("not_exist"))
        # test get default config
        self.assertEqual(bco.get_cfg("timeout"), 1)

    def test_get_cfg_deep(self):
        """Test method get_cfg_deep()"""
        bco = self.SampleBaseRouterWithConfig(name='SampleTestRouter')
        # one lvl deep
        self.assertEqual(bco.get_cfg_deep(["protocol"]), "tcp")
        self.assertIsNone(bco.get_cfg_deep(["not_exist"]))
        # many lvl deep
        self.assertEqual(bco.get_cfg_deep(["test_deep", "test_property"]), "dir")
        self.assertIsNone(bco.get_cfg_deep(["test_deep", "not_exist"]))
        self.assertIsNone(bco.get_cfg_deep(["not_exist_lvl_1", "not_exist"]))
        # test get default config
        self.assertEqual(bco.get_cfg_deep(["timeout"]), 1)


if __name__ == '__main__':
    unittest.main()
