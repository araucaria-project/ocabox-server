import asyncio
import unittest
from obcom.data_colection.address import AddressError
from obsrv.data_colection.base_components.tree_base_provider import TreeBaseProvider
from obcom.data_colection.value import Value, TreeValueError
from obcom.data_colection.value_call import ValueRequest, ValueResponse
from test.data_colection.sample_test_value_provider import SampleTestValueProvider


class TreeBaseProviderTest(unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.v1 = ('val1', Value(1, 1661349399.030824))
        self.v2 = ('val2', Value(2, 1661349399.030824))
        self.vp1 = SampleTestValueProvider('provider1_name', 'provider1_source_name', [self.v1, self.v2])

        self.sample_address_prefix = 'aaa.bbb'
        self.start_index = len(self.sample_address_prefix.split('.'))

    def tearDown(self) -> None:
        super().tearDown()

    def test_get_response_redirect_to_next(self):
        """
        test when get_value() method compels to call next provider
        :return:
        """
        tbp = TreeBaseProvider('component_sample_name', self.vp1)

        request = ValueRequest('.'.join([self.sample_address_prefix, self.vp1.get_source_name(), self.v1[0]]),
                               self.v1[1].ts, 20)
        request.index = self.start_index  # set manually index
        response = asyncio.run(tbp.get_response(request))
        self.assertEqual(response.value, self.v1[1])
        self.assertTrue(response.status)
        self.assertIsNone(response.error)

    def test_get_response_no_next_component(self):
        """
        test when is no next provider and get_value() method compels to call next provider
        :return:
        """
        tbp = TreeBaseProvider('component_sample_name', None)
        request = ValueRequest('.'.join([self.sample_address_prefix, self.vp1.get_source_name(), self.v1[0]]),
                               self.v1[1].ts, 20)
        request.index = self.start_index  # set manually index
        response = asyncio.run(tbp.get_response(request))
        self.assertEqual(response.value, None)
        self.assertFalse(response.status)
        self.assertEqual(response.error.code, 3001)

    def test_get_response_address_error(self):
        """
        test when get_value() method call AddressError
        :return:
        """
        tbp = self.vp1
        # without specific code
        request = ValueRequest('.'.join([self.sample_address_prefix, self.vp1.get_source_name(), 'address_error']),
                               self.v1[1].ts, 20)
        request.index = self.start_index  # set manually index
        response = asyncio.run(tbp.get_response(request))
        self.assertEqual(response.value, None)
        self.assertFalse(response.status)
        self.assertEqual(response.error.code, AddressError.CODE_GROUP)  # code CODE_GROUP means no code was specified

        # with specific code
        request = ValueRequest('.'.join([self.sample_address_prefix, self.vp1.get_source_name(),
                                         'address_error_coded']), self.v1[1].ts, 20)
        request.index = self.start_index  # set manually index
        response = asyncio.run(tbp.get_response(request))
        self.assertEqual(response.value, None)
        self.assertFalse(response.status)
        self.assertEqual(response.error.code, 20)

    def test_get_response_value_error(self):
        """
        test when get_value() method call TreeValueError
        :return:
        """
        tbp = self.vp1
        # without specific code
        request = ValueRequest('.'.join([self.sample_address_prefix, self.vp1.get_source_name(), 'value_error']),
                               self.v1[1].ts, 20)
        request.index = self.start_index  # set manually index
        response = asyncio.run(tbp.get_response(request))
        self.assertEqual(response.value, None)
        self.assertFalse(response.status)
        self.assertEqual(response.error.code, TreeValueError.CODE_GROUP)  # code CODE_GROUP means no code was specified

        # with specific code
        request = ValueRequest('.'.join([self.sample_address_prefix, self.vp1.get_source_name(), 'value_error_coded']),
                               self.v1[1].ts, 20)
        request.index = self.start_index  # set manually index
        response = asyncio.run(tbp.get_response(request))
        self.assertEqual(response.value, None)
        self.assertFalse(response.status)
        self.assertEqual(response.error.code, 10)

    def test_get_response_wrong_type(self):
        """
        test when get_value() method return wrong type
        :return:
        """
        tbp = self.vp1
        request = ValueRequest('.'.join([self.sample_address_prefix, self.vp1.get_source_name(), 'wrong_type']),
                               self.v1[1].ts, 20)
        request.index = self.start_index  # set manually index
        response = asyncio.run(tbp.get_response(request))
        self.assertEqual(response.value, None)
        self.assertFalse(response.status)
        self.assertEqual(response.error.code, 2002)

    def test_get_response_crash(self):
        """
        test when get_value() method raise not supported error
        :return:
        """
        tbp = self.vp1
        request = ValueRequest('.'.join([self.sample_address_prefix, self.vp1.get_source_name(), 'crash']),
                               self.v1[1].ts, 20)
        request.index = self.start_index  # set manually index
        self.assertRaises(ValueError, asyncio.run, tbp.get_response(request))

    def test_get_resource(self):
        """Test method get_resources"""
        tbp = TreeBaseProvider('component_sample_name', self.vp1)
        resources = tbp.get_resources()
        self.assertEqual((self.vp1.get_resource_name(), [self.vp1.get_source_name()]), resources[0])

    def test_get_response_subcontractor_unexpected_exception(self):
        """
        Test when the subcontractor's get_response() raises an unexpected (non-AttributeError) exception.
        The result should be an error response with code 3003 and _on_subcontractor_return must be called.
        """

        class _RaisingSubcontractor:
            """Subcontractor that always raises RuntimeError from get_response()."""
            async def get_response(self, request):
                raise RuntimeError("unexpected subcontractor error")

        class _TrackingProvider(TreeBaseProvider):
            """Provider that tracks whether _on_subcontractor_return was invoked."""
            on_subcontractor_return_called = False

            async def _on_subcontractor_return(self, result: ValueResponse, request: ValueRequest):
                _TrackingProvider.on_subcontractor_return_called = True

        _TrackingProvider.on_subcontractor_return_called = False
        tbp = _TrackingProvider('tracking_provider', _RaisingSubcontractor())

        request = ValueRequest('.'.join([self.sample_address_prefix, 'some_source', 'some_val']),
                               self.v1[1].ts, 20)
        request.index = self.start_index
        response = asyncio.run(tbp.get_response(request))

        self.assertFalse(response.status)
        self.assertIsNone(response.value)
        self.assertEqual(response.error.code, 3003)
        self.assertTrue(_TrackingProvider.on_subcontractor_return_called,
                        "_on_subcontractor_return must be called even when subcontractor raises an exception")

    def test_get_response_subcontractor_no_method_still_calls_on_return(self):
        """
        Test when the subcontractor raises AttributeError (missing get_response method).
        The result should be an error response with code 3002 and _on_subcontractor_return must be called.
        """

        class _BadSubcontractor:
            """Subcontractor without get_response()."""
            pass

        class _TrackingProvider(TreeBaseProvider):
            on_subcontractor_return_called = False

            async def _on_subcontractor_return(self, result: ValueResponse, request: ValueRequest):
                _TrackingProvider.on_subcontractor_return_called = True

        _TrackingProvider.on_subcontractor_return_called = False
        tbp = _TrackingProvider('tracking_provider', _BadSubcontractor())

        request = ValueRequest('.'.join([self.sample_address_prefix, 'some_source', 'some_val']),
                               self.v1[1].ts, 20)
        request.index = self.start_index
        response = asyncio.run(tbp.get_response(request))

        self.assertFalse(response.status)
        self.assertIsNone(response.value)
        self.assertEqual(response.error.code, 3002)
        self.assertTrue(_TrackingProvider.on_subcontractor_return_called,
                        "_on_subcontractor_return must be called even when subcontractor has no get_response()")


if __name__ == '__main__':
    unittest.main()
