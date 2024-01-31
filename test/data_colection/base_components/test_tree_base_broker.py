import asyncio
import unittest
from typing import List, Tuple

from obsrv.data_colection.base_components.tree_base_broker import TreeBaseBroker
from obsrv.data_colection.base_components.tree_component import ProvidesResponseProtocol
from obsrv.data_colection.base_components.tree_provider import TreeProvider
from obcom.data_colection.address import AddressError
from obcom.data_colection.value import Value
from obcom.data_colection.value_call import ValueRequest


class TreeBaseBrokerTest(unittest.TestCase):
    class SampleTestValueProvider(TreeProvider):

        async def get_value(self, request: ValueRequest, **kwargs) -> Value or None:
            address = request.address
            value_name = address[address.get_last_index()]
            for n, v in self.test_values:
                if value_name == n:
                    return v
            return None

        def __init__(self, component_name: str, source_name: str, test_values: List[Tuple[str, Value]],
                     subcontractor: ProvidesResponseProtocol = None, **kwargs):
            self.test_values: List[Tuple[str, Value]] = test_values
            super().__init__(component_name=component_name, subcontractor=subcontractor, source_name=source_name,
                             **kwargs)

    def setUp(self):
        super().setUp()
        self.v1 = ('val1', Value(1, 1661349399.030824))
        self.v2 = ('val2', Value(2, 1661349399.030824))
        self.v3 = ('val3', Value(3, 1661349399.030824))
        self.v4 = ('val4', Value(4, 1661349399.030824))

        self.vp1 = self.SampleTestValueProvider('provider1_name', 'provider1', [self.v1, self.v2])
        self.vp2 = self.SampleTestValueProvider('provider2_name', 'provider2', [self.v3, self.v4])

    def tearDown(self) -> None:
        super().tearDown()

    def test_init(self):
        """
        Test correct initialize object
        """
        vb = TreeBaseBroker('DefaultBroker')
        self.assertIsInstance(vb.get_list_providers(), list)
        self.assertTrue(vb.get_list_providers() == [])

    def test_get_provider(self):
        """
        Test provider from broker.
        """
        vb = TreeBaseBroker('DefaultBroker', [self.vp1, self.vp2])
        # good provider
        request = ValueRequest('.'.join([self.vp1.get_source_name(), self.v1[0]]), self.v1[1].ts, 20)
        provider = vb._get_provider(request)
        self.assertEqual(provider.get_source_name(), self.vp1.get_source_name())
        self.assertIs(provider, self.vp1)
        # nonexistent provider
        request = ValueRequest('.'.join(['nonexistent_provider', self.v1[0]]), self.v1[1].ts, 20)
        provider = vb._get_provider(request)
        self.assertIsNone(provider)
        # wrong address
        request = ValueRequest('.'.join(['']), self.v1[1].ts, 20)
        self.assertRaises(AddressError, vb._get_provider, request)

    def test_get_provider_multi_named(self):
        """Test get provider when provider has many names"""
        name = 'multi_named_provider'
        special_provider = TreeProvider('does_not_matter', name)
        other_name1 = 'other_name1'
        other_name2 = 'other_name2'
        special_provider._source_names.append(other_name1)
        special_provider._source_names.append(other_name2)
        vb = TreeBaseBroker('DefaultBroker', [self.vp1, self.vp2, special_provider])
        # main name
        request = ValueRequest('.'.join([special_provider.get_source_name(), 'xxx']), self.v1[1].ts, 20)
        provider = vb._get_provider(request)
        self.assertEqual(provider.get_source_name(), special_provider.get_source_name())
        self.assertIs(provider, special_provider)
        # auxiliary source names
        request = ValueRequest('.'.join([other_name1, 'xxx']), self.v1[1].ts, 20)
        provider = vb._get_provider(request)
        self.assertEqual(provider.get_source_names(), special_provider.get_source_names())
        self.assertIs(provider, special_provider)

        request = ValueRequest('.'.join([other_name2, 'xxx']), self.v1[1].ts, 20)
        provider = vb._get_provider(request)
        self.assertEqual(provider.get_source_names(), special_provider.get_source_names())
        self.assertIs(provider, special_provider)

    def test_add_provider(self):
        """
        Test add provider.
        """
        vb = TreeBaseBroker('DefaultBroker', [self.vp1])
        # correct added new provider
        effect = vb.add_provider(self.vp2)
        self.assertTrue(effect)
        self.assertEqual(vb.get_list_providers(), [self.vp1, self.vp2])
        # try to add again the same provider
        effect = vb.add_provider(self.vp2)
        self.assertFalse(effect)
        self.assertEqual(vb.get_list_providers(), [self.vp1, self.vp2])
        # try force add again the same provider - broker should remove old provider and add new
        effect = vb.add_provider(self.vp1, True)
        self.assertTrue(effect)
        self.assertEqual(vb.get_list_providers(), [self.vp2, self.vp1])

    def test_get_response(self):
        """
        Test get response from broker.
        """
        vb = TreeBaseBroker('DefaultBroker', [self.vp1, self.vp2])
        # good request
        request = ValueRequest('.'.join([self.vp1.get_source_name(), self.v1[0]]), self.v1[1].ts, 20)
        response = asyncio.run(vb.get_response(request))
        self.assertTrue(response.status)
        self.assertEqual(response.value, self.v1[1])
        # wrong request - provider not exist
        request = ValueRequest('.'.join(['nonexistent_provider', self.v1[0]]), self.v1[1].ts, 20)
        response = asyncio.run(vb.get_response(request))
        self.assertEqual(response.address, request.address)
        self.assertIsNone(response.value)
        self.assertFalse(response.status)
        self.assertEqual(response.error.code, 1002)

    def test_get_configuration(self):
        """Test method get_configuration()"""
        provider = TreeProvider("test_sample_provider", "source_name")
        broker = TreeBaseBroker("test_sample_broker", [provider])
        cfg = broker.get_configuration()
        self.assertListEqual(list(cfg.keys()), ["test_sample_broker"])
        self.assertListEqual(list(cfg.get("test_sample_broker").keys()), ["child", "type", "config"])
        self.assertListEqual(list(cfg.get("test_sample_broker").get("child").keys()), ["test_sample_provider"])
        self.assertTrue(isinstance(cfg.get("test_sample_broker").get("child").get("test_sample_provider").
                                   get("config"), dict))
        self.assertTrue(cfg.get("test_sample_broker").get("child").get("test_sample_provider").get("config"))


if __name__ == '__main__':
    unittest.main()
