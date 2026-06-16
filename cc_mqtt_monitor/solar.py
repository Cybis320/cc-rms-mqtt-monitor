"""Self-contained solar elevation (no third-party deps).

Used to decide whether a station should currently be producing night output
(FF compression) or daytime output (frame images). Accuracy is ~0.1 deg, which
is far more than enough to classify day vs night around the horizon.

NOAA general solar position equations.
"""

import math
import time


def solar_elevation_deg(latitude, longitude, epoch=None):
    """Solar elevation in degrees for a lat/lon (deg, east +) at a UTC epoch."""
    epoch = epoch if epoch is not None else time.time()
    tm = time.gmtime(epoch)

    # Fractional year (radians).
    day_of_year = tm.tm_yday
    hour = tm.tm_hour + tm.tm_min / 60.0 + tm.tm_sec / 3600.0
    gamma = 2.0 * math.pi / 365.0 * (day_of_year - 1 + (hour - 12) / 24.0)

    # Equation of time (minutes) and solar declination (radians).
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.001480 * math.sin(3 * gamma)
    )

    # True solar time (minutes); timezone offset is 0 because we use UTC.
    time_offset = eqtime + 4.0 * longitude
    tst = (tm.tm_hour * 60 + tm.tm_min + tm.tm_sec / 60.0) + time_offset
    hour_angle = math.radians(tst / 4.0 - 180.0)

    lat_rad = math.radians(latitude)
    cos_zenith = (
        math.sin(lat_rad) * math.sin(decl)
        + math.cos(lat_rad) * math.cos(decl) * math.cos(hour_angle)
    )
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith = math.acos(cos_zenith)
    return 90.0 - math.degrees(zenith)
