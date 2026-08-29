from pyaraucaria.coordinates import ra_to_decimal, dec_to_decimal


def check_equatorial_coordinates(ra, dec):
    if isinstance(ra, str) and ra:
        ra = ra_to_decimal(ra)
    if isinstance(dec, str) and dec:
        dec = dec_to_decimal(dec)
    return ra, dec


def check_horizontal_coordinates(az, alt):
    if isinstance(az, str) and az:
        az = dec_to_decimal(az)
    if isinstance(alt, str) and alt:
        alt = dec_to_decimal(alt)
    return az, alt
