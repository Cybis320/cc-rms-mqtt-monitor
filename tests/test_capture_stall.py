"""Unit checks for the capture-stall settling grace (health.evaluate).

A staggered GRMSUpdater restart brings the cameras on a host back minutes apart.
A just-restarted station's newest FF/frame on disk is from BEFORE the restart, so
its age spans the whole downtime and would trip the stall check the instant
capture comes back. evaluate() suppresses that for capture_restart_grace_s (plus
the station's capture_wait_seconds), measured from the station's OWN process age.

Runs under pytest, or standalone: `python tests/test_capture_stall.py`.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor import health                       # noqa: E402
from cc_mqtt_monitor.config import Thresholds            # noqa: E402

T = Thresholds()


def _stalled(metrics):
    """True if evaluate() raised the capture_stalled flag for these metrics."""
    _status, problems = health.evaluate(metrics, T)
    return any("stalled" in p for p in problems)


# Night station whose newest FF on disk predates the restart (age >> threshold).
_NIGHT = dict(capture_alive=True, expected_output="ff", newest_fits_age_s=1200,
              newest_frame_age_s=None, capture_session_age_s=1200,
              capture_wait_seconds=0)


def test_fresh_restart_is_suppressed():
    # Process only 60s old: still settling, no alert despite the stale FF.
    assert not _stalled(dict(_NIGHT, capture_age_s=60))


def test_tail_camera_within_grace_with_wait_is_suppressed():
    # capture_wait_seconds extends the grace: 400s old + 120s wait => grace 420s.
    m = dict(_NIGHT, capture_wait_seconds=120, capture_age_s=400)
    assert not _stalled(m)


def test_past_grace_alerts():
    # Process older than the grace: a real stall, must alert.
    assert _stalled(dict(_NIGHT, capture_age_s=600))


def test_long_running_stall_alerts():
    # A camera up for hours that genuinely stops producing output still alerts.
    assert _stalled(dict(_NIGHT, capture_age_s=99999))


def test_unknown_age_fails_toward_alerting():
    # capture_age_s unknown (proc unreadable) must NOT mute a stall.
    assert _stalled(dict(_NIGHT, capture_age_s=None))


def test_daytime_frames_fresh_restart_is_suppressed():
    m = dict(capture_alive=True, expected_output="frames", newest_fits_age_s=None,
             newest_frame_age_s=1200, capture_wait_seconds=0, capture_age_s=30)
    assert not _stalled(m)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("\nAll %d capture-stall tests passed." % len(fns))
