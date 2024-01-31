import unittest
from obsrv.planrunner.dither_data import DitherModes


class TestDitherModes(unittest.TestCase):
    def setUp(self):
        super().setUp()

    def tearDown(self) -> None:
        super().tearDown()

    def test_is_mode_exist(self):
        """Test method is_mode_exist"""
        self.assertTrue(DitherModes.is_mode_exist("basic"))
        self.assertFalse(DitherModes.is_mode_exist("basic_no_exist"))
        self.assertFalse(DitherModes.is_mode_exist("ra_dither"))

    def test_ra_dither(self):
        """Test method ra_dither"""
        a = DitherModes.ra_dither(ra=20, dec=0, dither_frequency_nr=0, dith_val=3)
        self.assertIsInstance(a, float)


if __name__ == '__main__':
    unittest.main()
