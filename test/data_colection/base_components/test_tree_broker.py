import asyncio
import unittest

from obsrv.data_colection.base_components.tree_broker import TreeBroker
from obcom.data_colection.value import Value
from obcom.data_colection.value_call import ValueRequest
from test.data_colection.base_components.test_tree_base_broker import TreeBaseBrokerTest


class TreeBrokerTest(unittest.TestCase):
    SampleTestValueProvider = TreeBaseBrokerTest.SampleTestValueProvider

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

    def test_get_response(self):
        """
        Test get response from broker.
        """
        broker_source_name = 'DefaultBroker'
        vb = TreeBroker('DefaultBroker_name', broker_source_name, [self.vp1, self.vp2])
        # good request
        request = ValueRequest('.'.join([broker_source_name, self.vp1.get_source_name(), self.v1[0]]),
                               self.v1[1].ts, 20)
        response = asyncio.run(vb.get_response(request))
        self.assertTrue(response.status)
        self.assertEqual(response.value, self.v1[1])
        # wrong request - provider not exist
        request = ValueRequest('.'.join([broker_source_name, 'nonexistent_provider', self.v1[0]]),
                               self.v1[1].ts, 20)
        response = asyncio.run(vb.get_response(request))
        self.assertEqual(response.address, request.address)
        self.assertIsNone(response.value)
        self.assertFalse(response.status)
        self.assertEqual(response.error.code, 1002)

    def test_get_resource(self):
        """Test method get_resources"""
        broker_source_name = 'DefaultBroker'
        vb = TreeBroker('DefaultBroker_name', broker_source_name, [self.vp1, self.vp2])
        resources = vb.get_resources()
        self.assertEqual(("DefaultBroker_name_RESOURCE", [broker_source_name]), resources[0])


if __name__ == '__main__':
    unittest.main()
