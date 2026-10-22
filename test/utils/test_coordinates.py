"""Tests for obsrv/utils/coordinates.py.

Verifies that check_equatorial_coordinates and check_horizontal_coordinates
produce the same results as the former astropy.Angle implementation for the
formats accepted in practice (colon-separated sexagesimal, hour-angle notation,
signed declination).
"""
import unittest

from obsrv.utils.coordinates import check_equatorial_coordinates, check_horizontal_coordinates


class TestCheckEquatorialCoordinates(unittest.TestCase):

    def _assert_close(self, a, b, msg=None):
        self.assertAlmostEqual(a, b, places=5, msg=msg)

    # --- RA string parsing (hour-angle, result in degrees) ---

    def test_ra_colon_separated(self):
        ra, _ = check_equatorial_coordinates('12:30:00', 0.0)
        self._assert_close(ra, 187.5)

    def test_ra_zero(self):
        ra, _ = check_equatorial_coordinates('0:0:0', 0.0)
        self._assert_close(ra, 0.0)

    def test_ra_near_full_circle(self):
        ra, _ = check_equatorial_coordinates('23:59:59', 0.0)
        self._assert_close(ra, 359.995833)

    def test_ra_hourangle_notation(self):
        ra, _ = check_equatorial_coordinates('1h30m0s', 0.0)
        self._assert_close(ra, 22.5)

    # --- Dec string parsing (degrees) ---

    def test_dec_negative(self):
        _, dec = check_equatorial_coordinates(0.0, '-45:30:00')
        self._assert_close(dec, -45.5)

    def test_dec_positive_explicit_sign(self):
        _, dec = check_equatorial_coordinates(0.0, '+89:59:59')
        self._assert_close(dec, 89.999722)

    def test_dec_zero(self):
        _, dec = check_equatorial_coordinates(0.0, '0:0:0')
        self._assert_close(dec, 0.0)

    def test_dec_negative_colon(self):
        _, dec = check_equatorial_coordinates(0.0, '-12:15:00')
        self._assert_close(dec, -12.25)

    # --- Non-string inputs are passed through unchanged ---

    def test_ra_float_passthrough(self):
        ra, _ = check_equatorial_coordinates(45.0, 0.0)
        self.assertEqual(ra, 45.0)

    def test_dec_float_passthrough(self):
        _, dec = check_equatorial_coordinates(0.0, -30.0)
        self.assertEqual(dec, -30.0)

    def test_empty_string_passthrough(self):
        ra, dec = check_equatorial_coordinates('', '')
        self.assertEqual(ra, '')
        self.assertEqual(dec, '')

    def test_none_passthrough(self):
        ra, dec = check_equatorial_coordinates(None, None)
        self.assertIsNone(ra)
        self.assertIsNone(dec)


class TestCheckHorizontalCoordinates(unittest.TestCase):

    def _assert_close(self, a, b, msg=None):
        self.assertAlmostEqual(a, b, places=5, msg=msg)

    def test_az_colon_separated(self):
        az, _ = check_horizontal_coordinates('180:30:00', 0.0)
        self._assert_close(az, 180.5)

    def test_alt_positive(self):
        _, alt = check_horizontal_coordinates(0.0, '45:00:00')
        self._assert_close(alt, 45.0)

    def test_alt_negative(self):
        _, alt = check_horizontal_coordinates(0.0, '-5:30:00')
        self._assert_close(alt, -5.5)

    def test_float_passthrough(self):
        az, alt = check_horizontal_coordinates(90.0, 30.0)
        self.assertEqual(az, 90.0)
        self.assertEqual(alt, 30.0)


if __name__ == '__main__':
    unittest.main()
