import unittest

from obsrv.data_colection.base_components.tree_base_broker_default_target import TreeBaseBrokerDefaultTarget
from obsrv.data_colection.base_components.tree_provider import TreeProvider
from obcom.data_colection.value_call import ValueRequest


class TreeBaseBrokerDefaultTargetTest(unittest.TestCase):
    def setUp(self):
        super().setUp()
        p_name1 = 'p1'
        self.provider1 = TreeProvider(component_name=p_name1, source_name=p_name1)
        p_name2 = 'p2'
        self.provider2 = TreeProvider(component_name=p_name2, source_name=p_name2)
        default_p_name = 'default'
        self.de_provider = TreeProvider(component_name=default_p_name, source_name=default_p_name)
        self.vb = TreeBaseBrokerDefaultTarget('sample_name',
                                              list_providers=[self.provider1, self.provider2],
                                              default_provider=self.de_provider)

    def tearDown(self) -> None:
        super().tearDown()

    def test_get_provider(self):
        # get default provider
        request = ValueRequest('.'.join(['non_existing_address', 'some_val']), 1661349399.030824, 20)
        provider = self.vb._get_provider(request)
        self.assertEqual(provider.get_source_name(), self.de_provider.get_source_name())
        self.assertIs(provider, self.de_provider)
        # get normal provider
        request = ValueRequest('.'.join([self.provider2.get_source_name(), 'some_val']), 1661349399.030824, 20)
        provider = self.vb._get_provider(request)
        self.assertEqual(provider.get_source_name(), self.provider2.get_source_name())
        self.assertIs(provider, self.provider2)

    def test_get_provider_no_address(self):
        """
        The test checks if the address does not point to any next block, in which case it should be taken as the
        default.
        """
        request = ValueRequest('some_val', 1661349399.030824, 20)
        request.index = 1
        provider = self.vb._get_provider(request)
        self.assertEqual(provider.get_source_name(), self.de_provider.get_source_name())

    def test_get_configuration(self):
        """Test method get_configuration()"""
        provider = TreeProvider("test_sample_provider", "source_name")
        provider2 = TreeProvider("test_default_provider", "source_name")
        broker = TreeBaseBrokerDefaultTarget("test_sample_broker", [provider], default_provider=provider2)
        cfg = broker.get_configuration()

        self.assertListEqual(list(cfg.keys()), ["test_sample_broker"])
        self.assertListEqual(list(cfg.get("test_sample_broker").keys()), ["child", "type", "config"])
        self.assertListEqual(list(cfg.get("test_sample_broker").get("child").keys()), ["test_sample_provider",
                                                                                       "test_default_provider"])
        self.assertTrue(isinstance(cfg.get("test_sample_broker").get("child").get("test_default_provider").
                                   get("config"), dict))


if __name__ == '__main__':
    unittest.main()
