import logging
import time
import unittest
from obsrv.data_colection.base_components.tree_provider import TreeProvider
from obsrv.data_colection.base_components.tree_provider_frontend import TreeProviderFrontend
from obcom.data_colection.value import Value, TreeValueError
from obcom.data_colection.value_call import ValueRequest

logger = logging.getLogger(__name__.rsplit('.')[-1])


class TreeProviderFrontendTest(unittest.IsolatedAsyncioTestCase):
    class SampleTestValueProvider(TreeProvider):
        async def get_value(self, request: ValueRequest, **kwargs) -> Value or None:
            if request.index < len(request.address) and request.address[request.index] == 'some_val':
                return Value(5, time.time())
            else:
                raise TreeValueError

    def setUp(self):
        super().setUp()
        self.sample_provider_name = 'sample_name'
        self.sample_provider_source_name = 'source_sample_name'
        self.sample_provider = self.SampleTestValueProvider(self.sample_provider_name, self.sample_provider_source_name,
                                                            None)

    def test_get_source_name(self):
        provider_name = 'provider_sample_name'
        provider_source_name = 'provider_source_sample_name'
        provider = TreeProvider(provider_name, provider_source_name, None)
        frontend_name = 'frontend_sample_name'
        provider_frontend = TreeProviderFrontend(frontend_name, provider)
        self.assertTrue(provider_frontend.get_source_name() == provider_source_name)

    async def test_get_response(self):

        frontend_name = 'frontend_sample_name'
        provider_frontend = TreeProviderFrontend(frontend_name, self.sample_provider)
        provider_name = 'provider_sample_name'
        provider_source_name = 'provider_source_sample_name'
        provider = TreeProvider(provider_name, provider_source_name, provider_frontend)
        req = ValueRequest('.'.join([provider_source_name, self.sample_provider_source_name, 'some_val']))
        result = await provider.get_response(request=req)
        self.assertIsNotNone(result.value)
        req = ValueRequest('.'.join([provider_source_name, 'some_val']))
        result = await provider.get_response(request=req)
        self.assertIsNone(result.value)


if __name__ == '__main__':
    unittest.main()
