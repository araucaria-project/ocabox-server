from astropy.coordinates import Angle
from astropy import units as u


def check_equatorial_coordinates(ra, dec):
    if isinstance(ra, str) and ra:
        ra = Angle(ra, unit=u.hourangle).deg
    if isinstance(dec, str) and dec:
        dec = Angle(dec, unit=u.deg).deg
    return ra, dec


def check_horizontal_coordinates(az, alt):
    if isinstance(az, str) and az:
        az = Angle(az, unit=u.deg).deg
    if isinstance(alt, str) and alt:
        alt = Angle(alt, unit=u.deg).deg
    return az, alt
