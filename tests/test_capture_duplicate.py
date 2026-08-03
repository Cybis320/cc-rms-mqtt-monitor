"""Duplicate StartCapture instances for ONE camera must be detected and must not hide.

Several different failure chains end the same way: an external supervisor sees the station
as unhealthy, respawns StartCapture with no already-running guard, and the clones pile up
-- each a full capture tree (~700 MB) -- until the OOM-killer takes the box down. Observed
in the field at ~38-40 clones and 17 OOM kills.

Detecting the STATE (more than one instance) rather than any one trigger catches every
chain that produces it. Two things are asserted here:

  * the alert fires on the state, and fires even while the station still looks "up";
  * memory is summed across ALL instances. The old collector kept only the FIRST tree root,
    so 38 clones reported one clone's RSS and looked perfectly healthy -- the reason this
    failure was invisible.

Runs under pytest, or standalone: `python tests/test_capture_duplicate.py`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor import health                       # noqa: E402
from cc_mqtt_monitor.config import Thresholds            # noqa: E402

T = Thresholds()
LIVE = dict(capture_alive=True, expected_output="frames", newest_fits_age_s=30,
            newest_frame_age_s=30, capture_age_s=999999, capture_wait_seconds=0,
            timelapse_mp4_present=True, newest_timelapse_age_s=3600)


def _eval(**kw):
    m = dict(LIVE); m.update(kw)
    return health.evaluate(m, T)


def test_single_instance_is_silent():
    assert not any("StartCapture instances" in p for p in _eval(capture_instances=1)[1])


def test_duplicates_are_an_error():
    status, problems = _eval(capture_instances=38, total_rss_mb=26600)
    assert any("38 StartCapture instances" in p for p in problems)
    assert status == health.ERROR


def test_duplicate_reports_total_memory():
    """The operator needs the scale: 38 clones is abstract, 26 GB is not."""
    _s, problems = _eval(capture_instances=38, total_rss_mb=26600)
    assert any("26600 MB total" in p for p in problems)


def test_duplicates_flagged_even_while_capture_looks_healthy():
    """The clones do not stop output at first -- the station still produces FF/frames while
    memory climbs. The alert must not wait for capture to visibly fail."""
    status, problems = _eval(capture_instances=5, newest_frame_age_s=5)
    assert any("StartCapture instances" in p for p in problems)


def test_missing_field_does_not_alert():
    """A monitor that predates the field publishes nothing -- must not false-alarm."""
    assert not any("StartCapture instances" in p for p in _eval()[1])
    assert not any("StartCapture instances" in p for p in _eval(capture_instances=None)[1])


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok", name)
