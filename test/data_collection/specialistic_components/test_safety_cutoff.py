import logging
import time
import unittest

from obsrv.tree_components.base_components.tree_base_broker import TreeBaseBroker
from obsrv.tree_components.base_components.tree_provider import TreeProvider
from obsrv.tree_components.specialized_components import TreeBaseRequestBlocker
from obsrv.tree_components.specialized_components import TreeBlockerAccessGrantor
from obcom.data_colection.tree_user import TreeServiceUser
from obcom.data_colection.value_call import ValueRequest
from test.data_collection.sample_test_value_provider import SampleTestValueProvider

logger = logging.getLogger(__name__.rsplit('.')[-1])


class SafetyCutoffTest(unittest.IsolatedAsyncioTestCase):
    """Tests for the safety cutoff switch in TreeBaseRequestBlocker."""

    def setUp(self):
        super().setUp()
        self.sample_provider_name = 'sample_name'
        self.sample_provider_source_name = 'source_sample_name'
        self.sample_provider = SampleTestValueProvider(self.sample_provider_name, self.sample_provider_source_name, [])
        self.blocker_name = 'test_sample_blocker'  # this name is used in config file
        self.blocker = TreeBaseRequestBlocker(component_name=self.blocker_name, subcontractor=self.sample_provider)
        self.provider_name = 'provider_sample_name'
        self.provider_source_name = 'provider_source_sample_name'
        self.provider = TreeProvider(self.provider_name, self.provider_source_name, self.blocker)

        self.grantor_name = 'sample_grantor'
        self.grantor_source_name = 'sample_grantor_source_name'
        self.grantor = TreeBlockerAccessGrantor(self.grantor_name, self.grantor_source_name, self.blocker)

        self.broker = TreeBaseBroker('sample_broker', [self.provider, self.grantor])

        # Give user a reservation so PUT requests would normally pass
        self.user = TreeServiceUser(name='test_user')
        self.blocker.make_reservation(user=self.user, timeout_reservation=time.time() + 600)

        # Manually add a cutoff-listed command for testing
        self.blocker._safety_cutoff_list = ['slewtocoordinates', 'opencover', 'dome.slewtoazimuth']

    async def test_cutoff_disengaged_allows_all(self):
        """When cutoff is disengaged, listed commands pass normally."""
        self.assertFalse(self.blocker.is_safety_cutoff_engaged())
        req = ValueRequest(
            '.'.join([self.provider_source_name, self.sample_provider_source_name, 'slewtocoordinates']),
            request_type='PUT', user=self.user)
        result = await self.broker.get_response(request=req)
        # Should pass through (SampleTestValueProvider returns None for unknown, but no error)
        self.assertIsNone(result.error)

    async def test_cutoff_engaged_blocks_listed_command(self):
        """When cutoff is engaged, listed commands are blocked with error 1005."""
        self.blocker.engage_safety_cutoff()
        req = ValueRequest(
            '.'.join([self.provider_source_name, self.sample_provider_source_name, 'slewtocoordinates']),
            request_type='PUT', user=self.user)
        result = await self.broker.get_response(request=req)
        self.assertIsNone(result.value)
        self.assertEqual(result.error.code, 1005)

    async def test_cutoff_engaged_allows_unlisted_command(self):
        """When cutoff is engaged, commands not in the list pass through normally."""
        self.blocker.engage_safety_cutoff()
        req = ValueRequest(
            '.'.join([self.provider_source_name, self.sample_provider_source_name, 'abortslew']),
            request_type='PUT', user=self.user)
        result = await self.broker.get_response(request=req)
        self.assertIsNone(result.error)

    async def test_cutoff_does_not_block_get(self):
        """GET requests pass through even when cutoff is engaged for a listed command."""
        self.blocker.engage_safety_cutoff()
        req = ValueRequest(
            '.'.join([self.provider_source_name, self.sample_provider_source_name, 'slewtocoordinates']),
            request_type='GET', user=self.user)
        result = await self.broker.get_response(request=req)
        self.assertIsNone(result.error)

    async def test_special_permission_does_not_bypass_cutoff(self):
        """The regular SPECIAL_PERMISSION_PARAM must NOT bypass the safety cutoff."""
        self.blocker.engage_safety_cutoff()
        request_data = {TreeBaseRequestBlocker.SPECIAL_PERMISSION_PARAM: True}
        req = ValueRequest(
            '.'.join([self.provider_source_name, self.sample_provider_source_name, 'slewtocoordinates']),
            request_type='PUT', user=self.user, request_data=request_data)
        result = await self.broker.get_response(request=req)
        self.assertIsNone(result.value)
        self.assertEqual(result.error.code, 1005)

    async def test_safety_cutoff_bypass_param_passes(self):
        """The dedicated SAFETY_CUTOFF_BYPASS_PARAM bypasses the cutoff."""
        self.blocker.engage_safety_cutoff()
        request_data = {TreeBaseRequestBlocker.SAFETY_CUTOFF_BYPASS_PARAM: True}
        req = ValueRequest(
            '.'.join([self.provider_source_name, self.sample_provider_source_name, 'slewtocoordinates']),
            request_type='PUT', user=self.user, request_data=request_data)
        result = await self.broker.get_response(request=req)
        self.assertIsNone(result.error)

    async def test_cutoff_full_address_matching(self):
        """Dotted entries in cutoff list match by full relative address."""
        self.blocker.engage_safety_cutoff()
        # 'dome.slewtoazimuth' is in the list as a full-address pattern
        req = ValueRequest(
            '.'.join([self.provider_source_name, 'dome', 'slewtoazimuth']),
            request_type='PUT', user=self.user)
        result = await self.broker.get_response(request=req)
        self.assertEqual(result.error.code, 1005)

    async def test_cutoff_command_matching_ignores_component_prefix(self):
        """Command-name entries (no dot) match regardless of component prefix."""
        self.blocker.engage_safety_cutoff()
        # 'opencover' should match whether prefixed by 'covercalibrator' or anything else
        req = ValueRequest(
            '.'.join([self.provider_source_name, 'covercalibrator', 'opencover']),
            request_type='PUT', user=self.user)
        result = await self.broker.get_response(request=req)
        self.assertEqual(result.error.code, 1005)

        req = ValueRequest(
            '.'.join([self.provider_source_name, 'other_component', 'opencover']),
            request_type='PUT', user=self.user)
        result = await self.broker.get_response(request=req)
        self.assertEqual(result.error.code, 1005)

    def test_engage_disengage_state(self):
        """Engage and disengage toggle the state correctly."""
        self.assertFalse(self.blocker.is_safety_cutoff_engaged())
        self.blocker.engage_safety_cutoff()
        self.assertTrue(self.blocker.is_safety_cutoff_engaged())
        self.blocker.disengage_safety_cutoff()
        self.assertFalse(self.blocker.is_safety_cutoff_engaged())

    def test_get_safety_cutoff_list_returns_copy(self):
        """get_safety_cutoff_list returns a copy, not the internal list."""
        result = self.blocker.get_safety_cutoff_list()
        result.append('should_not_appear')
        self.assertNotIn('should_not_appear', self.blocker.get_safety_cutoff_list())


class SafetyCutoffGrantorTest(unittest.IsolatedAsyncioTestCase):
    """Tests for safety cutoff commands via TreeBlockerAccessGrantor."""

    def setUp(self):
        super().setUp()
        self.sample_provider = SampleTestValueProvider('sample', 'source', [])
        self.blocker = TreeBaseRequestBlocker(component_name='test_sample_blocker', subcontractor=self.sample_provider)
        self.provider = TreeProvider('provider', 'provider_source', self.blocker)

        self.grantor = TreeBlockerAccessGrantor('grantor', 'grantor_source', self.blocker)
        self.broker = TreeBaseBroker('broker', [self.provider, self.grantor])
        self.user = TreeServiceUser(name='test_user')

    async def test_engage_via_grantor(self):
        """PUT engage_safety_cutoff engages the cutoff."""
        req = ValueRequest('grantor_source.engage_safety_cutoff', request_type='PUT', user=self.user)
        result = await self.broker.get_response(request=req)
        self.assertTrue(result.value.v)
        self.assertTrue(self.blocker.is_safety_cutoff_engaged())

    async def test_disengage_via_grantor(self):
        """PUT disengage_safety_cutoff disengages the cutoff."""
        self.blocker.engage_safety_cutoff()
        req = ValueRequest('grantor_source.disengage_safety_cutoff', request_type='PUT', user=self.user)
        result = await self.broker.get_response(request=req)
        self.assertTrue(result.value.v)
        self.assertFalse(self.blocker.is_safety_cutoff_engaged())

    async def test_state_query_disengaged(self):
        """GET safety_cutoff_state returns correct structure when disengaged."""
        req = ValueRequest('grantor_source.safety_cutoff_state', request_type='GET', user=self.user)
        result = await self.broker.get_response(request=req)
        self.assertIsNotNone(result.value)
        state = result.value.v
        self.assertFalse(state['engaged'])
        self.assertIsInstance(state['blocked_commands'], list)

    async def test_state_query_engaged(self):
        """GET safety_cutoff_state returns engaged=True after engagement."""
        self.blocker.engage_safety_cutoff()
        req = ValueRequest('grantor_source.safety_cutoff_state', request_type='GET', user=self.user)
        result = await self.broker.get_response(request=req)
        state = result.value.v
        self.assertTrue(state['engaged'])

    async def test_full_cycle_engage_block_disengage_pass(self):
        """Full cycle: engage cutoff, verify block, disengage, verify pass."""
        self.blocker._safety_cutoff_list = ['slewtocoordinates']
        self.blocker.make_reservation(user=self.user, timeout_reservation=time.time() + 600)

        # Engage
        req = ValueRequest('grantor_source.engage_safety_cutoff', request_type='PUT', user=self.user)
        await self.broker.get_response(request=req)

        # Command blocked
        req = ValueRequest('provider_source.source.slewtocoordinates', request_type='PUT', user=self.user)
        result = await self.broker.get_response(request=req)
        self.assertEqual(result.error.code, 1005)

        # Disengage
        req = ValueRequest('grantor_source.disengage_safety_cutoff', request_type='PUT', user=self.user)
        await self.broker.get_response(request=req)

        # Command passes
        req = ValueRequest('provider_source.source.slewtocoordinates', request_type='PUT', user=self.user)
        result = await self.broker.get_response(request=req)
        self.assertIsNone(result.error)


if __name__ == '__main__':
    unittest.main()
