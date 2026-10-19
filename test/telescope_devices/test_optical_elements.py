"""Building the device tree from a configuration with an optical path (D-F1, ocabox-server#52).

Components of the optical path without a device in TIC (``sky``, ``flatscreen``, lamps, fibres,
splitters, BESO's mirrors) become inert :class:`OpticalElement` nodes: they keep the configuration
one tree, never reach a connector, and answer every request with 3002 CRITICAL. A TIC device class
takes precedence over the optical-path kind. A component that cannot be built is skipped with an
ERROR; its siblings survive (before D-F1 one unknown kind emptied the whole telescope).
"""
import unittest
from unittest.mock import AsyncMock, MagicMock

from obcom.data_colection.coded_error import TreeStructureError
from obcom.data_colection.response_error import ResponseError

from obsrv.telescope_devices.device_tree import Camera, Component, Dome, OpticalElement, TertiaryOCA

JK15_LIKE = {
    'components': {
        'sky': {'kind': 'sky'},
        'flatscreen': {'kind': 'flatscreen'},
        'dome': {'kind': 'dome', 'optics': {'inputs': {'open': 'sky', 'flat': 'flatscreen'}}},
        'tertiary': {
            'kind': 'tertiaryOCA',
            'positions': {'beso': {'port': 1}, 'andor': {'port': 2}},
            'optics': {'from': 'dome'},
        },
        'pickoff': {'kind': 'splitter', 'optics': {'from': {'tertiary': 'andor'}}},
        'camera': {'kind': 'camera', 'optics': {'from': {'pickoff': 'main'}}, 'paths': {'object': 'sky.science'}},
        'thar_lamp': {'kind': 'lamp', 'light': 'lamp.thar'},
        'm4': {'kind': 'mirror', 'positions': {'sky': {}, 'calib': {}}},
    }
}


def _build(options: dict) -> Component:
    root = Component('obs', None)
    root._connector = MagicMock(get=AsyncMock(), put=AsyncMock())
    root._setup(options)
    return root


class OpticalElementTreeTest(unittest.IsolatedAsyncioTestCase):

    def test_device_kinds_stay_devices_and_optical_kinds_become_elements(self):
        root = _build(JK15_LIKE)
        self.assertIsInstance(root.children['dome'], Dome)
        self.assertIsInstance(root.children['tertiary'], TertiaryOCA)
        self.assertIsInstance(root.children['camera'], Camera)
        for name in ('sky', 'flatscreen', 'pickoff', 'thar_lamp', 'm4'):
            with self.subTest(component=name):
                element = root.children[name]
                self.assertIsInstance(element, OpticalElement)
                self.assertEqual(element.kind, JK15_LIKE['components'][name]['kind'])

    def test_optical_keys_stay_in_component_options(self):
        root = _build(JK15_LIKE)
        self.assertEqual(root.children['tertiary'].component_options['positions']['andor'], {'port': 2})
        self.assertEqual(root.children['thar_lamp'].component_options['light'], 'lamp.thar')

    async def test_optical_element_answers_3002_critical_and_never_reaches_a_connector(self):
        root = _build(JK15_LIKE)
        sky = root.children['sky']
        for call in (sky.get('connected'), sky.put('connected', Connected=True)):
            with self.assertRaises(TreeStructureError) as ctx:
                await call
            self.assertEqual(ctx.exception.code, 3002)
            self.assertEqual(ctx.exception.severity, ResponseError.SEVERITY_CRITICAL)
        root._connector.get.assert_not_called()
        root._connector.put.assert_not_called()

    def test_broken_components_are_skipped_siblings_survive(self):
        options = {
            'components': {
                'sky': {'kind': 'sky'},
                'typo': {'kind': 'telescpoe'},
                'nokind': {'device_number': 0},
                'camera': {'kind': 'camera'},
            }
        }
        with self.assertLogs('device_tree', level='ERROR') as logs:
            root = _build(options)
        self.assertEqual(set(root.children), {'sky', 'camera'})
        self.assertFalse(hasattr(root, 'typo'))
        joined = '\n'.join(logs.output)
        self.assertIn("obs.typo", joined)
        self.assertIn("unknown component kind 'telescpoe'", joined)
        self.assertIn("obs.nokind", joined)


if __name__ == '__main__':
    unittest.main()
