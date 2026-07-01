"""Unit checks for camera-unreachable standby (health + the monitor clock).

When a camera won't answer a ping for a sustained window AND output is stalled,
the fault is the camera itself. The monitor collapses the record to one
"camera not pingable" root cause -- outranking capture_down -- and suppresses the
downstream cascade (stall, detection, drops, watchdog, log lines). The collapse
lives in the monitor (not the bridge) so the dashboard sees the clean record too.

Runs under pytest, or standalone: `python tests/test_camera_unreachable.py`.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor import health, monitor                # noqa: E402
from cc_mqtt_monitor.config import Thresholds              # noqa: E402

T = Thresholds()

# A stalled night station: newest FF long past the freshness threshold, process
# old enough to be past the settling grace.
_STALLED = dict(expected_output="ff", newest_fits_age_s=1200,
                capture_age_s=99999, capture_wait_seconds=0)


def test_output_stalled_predicate():
    assert health.output_stalled(_STALLED, T)
    assert not health.output_stalled(dict(_STALLED, newest_fits_age_s=10), T)   # fresh
    assert not health.output_stalled(dict(_STALLED, capture_age_s=30), T)       # settling
    assert not health.output_stalled({"expected_output": "idle"}, T)           # idle


def test_standby_collapses_and_outranks_capture_down():
    # Camera in standby AND StartCapture also gone: report ONLY camera-unreachable.
    m = dict(capture_alive=False, camera_standby=True, camera_host="10.0.0.9",
             camera_unreachable_s=1320)
    status, problems = health.evaluate(m, T)
    assert status == "error"
    assert len(problems) == 1
    assert "not pingable" in problems[0]
    assert "10.0.0.9" in problems[0]


def test_transient_still_alerts_normally():
    # Stalled + unreachable but NOT yet past grace (no camera_standby): the normal
    # capture_stalled alert must still fire -- transient outages page as before.
    m = dict(capture_alive=True, **_STALLED)
    status, problems = health.evaluate(m, T)
    assert any("stalled" in p for p in problems)
    assert not any("pingable" in p for p in problems)


def test_camera_up_but_stalled_does_not_stand_by():
    # A camera that answers ping but isn't producing (dead RTSP daemon) is a real
    # stall, not a standby case -- capture_stalled, not camera_unreachable.
    monitor._UNREACHABLE.clear()
    standby, secs = monitor._track_unreachable("S", True, True, 500.0, T.camera_unreachable_grace_s)
    assert standby is False and secs is None


def test_clock_trips_only_after_grace():
    monitor._UNREACHABLE.clear()
    g = T.camera_unreachable_grace_s
    assert monitor._track_unreachable("S", True, False, 0.0, g) == (False, 0.0)
    assert monitor._track_unreachable("S", True, False, g - 1, g)[0] is False
    assert monitor._track_unreachable("S", True, False, g + 1, g)[0] is True


def test_indeterminate_ping_never_stands_by():
    # ICMP-blocked-but-working camera (ping returns None): never enter standby.
    monitor._UNREACHABLE.clear()
    for t in (0.0, 1000.0, 100000.0):
        assert monitor._track_unreachable("S", True, None, t, T.camera_unreachable_grace_s) == (False, None)


def test_recovery_clears_clock():
    monitor._UNREACHABLE.clear()
    g = T.camera_unreachable_grace_s
    monitor._track_unreachable("S", True, False, 0.0, g)
    assert "S" in monitor._UNREACHABLE
    monitor._track_unreachable("S", True, True, 10.0, g)     # camera answers
    assert "S" not in monitor._UNREACHABLE


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("\nAll %d camera-unreachable tests passed." % len(fns))
