"""Day/night expectation using RMS's own switch logic and programmed delays.

We deliberately do NOT import RMS.CaptureModeSwitcher: it pulls in
Utils.CameraControl -> RMS.ConfigReader -> cv2/numpy, ballooning the monitor
from ~12 MB to ~106 MB, and it would couple the monitor to RMS being importable
(the very failure it watches for). Instead we replicate RMS's logic with the
same astronomy library (ephem) and mirror its constants.

Keep these in sync with RMS.CaptureModeSwitcher (they are stable):
    SWITCH_HORIZON_DEG, CAPTURE_HORIZON_DEG, SHUTDOWN_INERTIA_SECONDS
The other programmed delay, capture_wait_seconds (the multi-camera switch
stagger), is read live from each station's RMS .config, so it is always exact.

The algorithm mirrors RMS.CaptureModeSwitcher.lastNightToDaySwitch(): find the
most recent sun crossing of the per-mode horizon and treat the window of
(capture_wait_seconds + SHUTDOWN_INERTIA_SECONDS) after it as "transition".
"""

from datetime import datetime, timedelta

try:
    import ephem
    HAVE_EPHEM = True
except Exception:
    HAVE_EPHEM = False

# Mirrored from RMS.CaptureModeSwitcher.
SWITCH_HORIZON_DEG = "-9"        # continuous + switch_camera_modes
CAPTURE_HORIZON_DEG = "-5:26"    # standard capture / continuous-no-switch
SHUTDOWN_INERTIA_SECONDS = 300   # pipeline shutdown inertia after a switch

_EPOCH = datetime(1970, 1, 1)


def expected_output(station, epoch):
    """Return 'ff' / 'frames' / 'idle' / 'transition', or None if it can't be
    computed (no ephem, no location, or polar day/night)."""
    if not HAVE_EPHEM or not station.has_location:
        return None

    cont = station.continuous_capture
    horizon = (SWITCH_HORIZON_DEG if (cont and station.switch_camera_modes)
               else CAPTURE_HORIZON_DEG)
    when = _EPOCH + timedelta(seconds=epoch)
    try:
        obs = ephem.Observer()
        obs.lat = str(station.latitude)
        obs.long = str(station.longitude)
        obs.elevation = station.elevation
        obs.horizon = horizon
        obs.date = ephem.Date(when)
        sun = ephem.Sun()
        prev_rise = obs.previous_rising(sun).datetime()    # crossing up -> day
        prev_set = obs.previous_setting(sun).datetime()    # crossing down -> night
    except (ephem.AlwaysUpError, ephem.NeverUpError):
        return None  # polar; caller falls back
    except Exception:
        return None

    # RMS's programmed switch delay (see lastNightToDaySwitch()).
    grace = timedelta(seconds=(station.capture_wait_seconds or 0)
                      + SHUTDOWN_INERTIA_SECONDS)
    last_switch = max(prev_rise, prev_set)
    if when - last_switch < grace:
        return "transition"

    if prev_rise > prev_set:                  # last event was a sunrise -> day
        return "frames" if (cont and station.save_frames) else "idle"
    return "ff"                               # last event was a sunset -> night
