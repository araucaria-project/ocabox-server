import logging
import time
import unittest
from obsrv.data_colection.base_components.tree_provider import TreeProvider
from obsrv.data_colection.specialistic_components.tree_base_request_blocker import ReservationError, \
    TreeBaseRequestBlocker
from obcom.data_colection.tree_user import TreeServiceUser, TreeUser
from obcom.data_colection.value import TreeValueError, Value
from obcom.data_colection.value_call import ValueRequest
from obsrv.ob_config import SingletonConfig

logger = logging.getLogger(__name__.rsplit('.')[-1])


class TreeBaseRequestBlockerTest(unittest.IsolatedAsyncioTestCase):
    class SampleTestValueProvider(TreeProvider):
        async def get_value(self, request: ValueRequest, **kwargs) -> Value or None:
            if request.index < len(request.address) and request.address[request.index] in {'some_val',
                                                                                           'some_other_val'}:
                return Value(5, time.time())
            else:
                raise TreeValueError

    def setUp(self):
        super().setUp()
        self.sample_provider_name = 'sample_name'
        self.sample_provider_source_name = 'source_sample_name'
        self.sample_provider = self.SampleTestValueProvider(self.sample_provider_name, self.sample_provider_source_name,
                                                            None)
        self.blocker_name = 'test_sample_blocker'  # this name is used in config file
        self.blocker = TreeBaseRequestBlocker(component_name=self.blocker_name, subcontractor=self.sample_provider)
        self.provider_name = 'provider_sample_name'
        self.provider_source_name = 'provider_source_sample_name'
        self.provider = TreeProvider(self.provider_name, self.provider_source_name, self.blocker)

    async def test_access_granted_get_request(self):
        """Test access granted for GET request"""
        # GET request always go through
        req = ValueRequest('.'.join([self.provider_source_name, self.sample_provider_source_name, 'some_val']))
        result = await self.provider.get_response(request=req)
        self.assertIsNotNone(result.value.v)

    async def test_access_denied_put_request(self):
        """Test when access denied"""
        # PUT request stop
        user = TreeServiceUser(name='some_user')
        req = ValueRequest('.'.join([self.provider_source_name, self.sample_provider_source_name, 'some_val']),
                           request_type='PUT', user=user)
        result = await self.provider.get_response(request=req)
        self.assertIsNone(result.value)
        self.assertTrue(result.error.code == 1004)

    async def test_access_granted_put_request(self):
        """Test when access granted for user"""
        # PUT request when access granted
        user = TreeServiceUser(name='some_user')
        self.blocker.make_reservation(user=user, timeout_reservation=time.time() + 60)
        req = ValueRequest('.'.join([self.provider_source_name, self.sample_provider_source_name, 'some_val']),
                           request_type='PUT', user=user)
        result = await self.provider.get_response(request=req)
        self.assertIsNotNone(result.value)
        self.assertIsNone(result.error)

    async def test_wrong_type_request(self):
        """Test when comes request witch unrecognised type"""
        # wrong type request
        req = ValueRequest('.'.join([self.provider_source_name, self.sample_provider_source_name, 'some_val']),
                           request_type='EXECUTE')
        result = await self.provider.get_response(request=req)
        self.assertIsNone(result.value)
        self.assertTrue(result.error.code == 4001)

    async def test_access_timeout_put_request(self):
        """Test when user lost access by timeout"""
        # PUT request when access denied by timeout
        user = TreeServiceUser(name='some_user')
        self.blocker.make_reservation(user=user, timeout_reservation=time.time() - 2)
        req = ValueRequest('.'.join([self.provider_source_name, self.sample_provider_source_name, 'some_val']),
                           request_type='PUT', user=user)
        result = await self.provider.get_response(request=req)
        self.assertIsNone(result.value)
        self.assertTrue(result.error.code == 1004)

    def test_double_reservation(self):
        """test whether the reservation will not be overwritten before the end of the previous one. """
        user = TreeServiceUser(name='some_user')
        self.blocker.make_reservation(user=user, timeout_reservation=time.time() + 60)
        user2 = TreeServiceUser(name='some_user2')
        with self.assertRaises(ReservationError):
            self.blocker.make_reservation(user=user2, timeout_reservation=time.time() + 60)
        self.assertEqual(user, self.blocker.get_current_user())
        self.assertNotEqual(user2, self.blocker.get_current_user())

    async def test_white_list(self):
        """Test request from white list"""
        req_address = '.'.join([self.provider_source_name, self.sample_provider_source_name, 'some_other_val'])
        req = ValueRequest(req_address, request_type='PUT')
        result = await self.provider.get_response(request=req)
        # first check if white list is empty
        self.assertIsNone(result.value)
        self.assertTrue(result.error.code == 1004)

        # add address to white list
        self.blocker.add_to_white_list('.'.join([self.sample_provider_source_name, 'some_other_val']), 'PUT')
        req = ValueRequest(req_address, request_type='PUT')
        result = await self.provider.get_response(request=req)
        self.assertIsNotNone(result.value)
        self.assertIsNone(result.error)

    async def test_black_list(self):
        """Test request from black list"""
        req_address = '.'.join([self.provider_source_name, self.sample_provider_source_name, 'some_other_val'])
        req = ValueRequest(req_address, request_type='GET')
        result = await self.provider.get_response(request=req)
        # first check if black list is empty
        self.assertIsNotNone(result.value)
        self.assertIsNone(result.error)

        # add address to black list
        self.blocker.add_to_black_list('.'.join([self.sample_provider_source_name, 'some_other_val']), 'GET')
        req = ValueRequest(req_address, request_type='GET')
        result = await self.provider.get_response(request=req)
        self.assertIsNone(result.value)
        self.assertTrue(result.error.code == 1004)

    async def test_black_list_type_request(self):
        """Test black list can recognise type request"""
        user = TreeServiceUser(name='some_user')
        self.blocker.make_reservation(user=user, timeout_reservation=time.time() + 60)
        self.blocker.add_to_black_list('.'.join([self.sample_provider_source_name, 'some_other_val']), 'GET')

        req_address = '.'.join([self.provider_source_name, self.sample_provider_source_name, 'some_other_val'])
        req = ValueRequest(req_address, request_type='PUT', user=user)
        result = await self.provider.get_response(request=req)
        # first check if black list is empty
        self.assertIsNotNone(result.value)
        self.assertIsNone(result.error)

        req = ValueRequest(req_address, request_type='GET', user=user)
        result = await self.provider.get_response(request=req)
        self.assertIsNone(result.value)
        self.assertTrue(result.error.code == 1004)

    def test__init_lists_of_special_requests(self):
        """Test initial black/white list"""
        black_lists = self.blocker._black_lists
        expected_black_list = SingletonConfig.get_config()['tree'][self.blocker_name]['black_list'].get()
        for key, val in expected_black_list.items():
            self.assertTrue(all(item in black_lists.get(key) for item in val))
        white_lists = self.blocker._white_lists
        expected_white_list = SingletonConfig.get_config()['tree'][self.blocker_name]['white_list'].get()
        self.assertFalse(white_lists.get('GET'))
        self.assertTrue(white_lists.get('PUT'))
        for key, val in expected_white_list.items():
            self.assertTrue(all(item in white_lists.get(key) for item in val))

    async def test_permit_request_with_special_param(self):
        """Test situation when request coming witch special permission param was set"""
        user = TreeServiceUser(name='some_user')
        # without param
        request_data = {}
        req = ValueRequest('.'.join([self.provider_source_name, self.sample_provider_source_name, 'some_val']),
                           request_type='PUT', user=user, request_data=request_data)
        result = await self.provider.get_response(request=req)
        self.assertIsNone(result.value)
        self.assertTrue(result.error.code == 1004)
        # with param and service user
        request_data = {TreeBaseRequestBlocker.SPECIAL_PERMISSION_PARAM: True}
        req = ValueRequest('.'.join([self.provider_source_name, self.sample_provider_source_name, 'some_val']),
                           request_type='PUT', user=user, request_data=request_data)
        result = await self.provider.get_response(request=req)
        self.assertIsNotNone(result.value)
        self.assertIsNone(result.error)

        # with param and normal user
        user = TreeUser(name='some_user')
        user.socket_id = b'qweet234'
        request_data = {TreeBaseRequestBlocker.SPECIAL_PERMISSION_PARAM: True}
        req = ValueRequest('.'.join([self.provider_source_name, self.sample_provider_source_name, 'some_val']),
                           request_type='PUT', user=user, request_data=request_data)
        result = await self.provider.get_response(request=req)
        self.assertIsNone(result.value)
        self.assertTrue(result.error.code == 1004)


if __name__ == '__main__':
    unittest.main()
