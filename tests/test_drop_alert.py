"""Drop alerting = a UNIVERSAL per-night rate standard + a catastrophic live guard.

The old per-cycle raw-count alert (dropped_frames_10min >= 10) fired on the normal
occasional blip every station has -- ~62 alerts/day of noise. Now:
  - alert once/night when RMS's per-night dropped_frame_rate (%) exceeds the universal
    threshold (the actionable "stream was degraded" signal + weekly-digest input);
  - a separate catastrophic LIVE guard still pages on a mid-night stream failure.

Runs under pytest, or standalone: `python tests/test_drop_alert.py`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor import health                       # noqa: E402
from cc_mqtt_monitor.config import Thresholds            # noqa: E402

T = Thresholds()
# a live, capturing station (passes the capture-liveness early returns so evaluate()
# reaches the drop block); newest FITS recent + no timelapse gap so nothing else trips.
BASE = dict(capture_alive=True, expected_output="frames", newest_fits_age_s=30,
            newest_frame_age_s=30, capture_age_s=999999, capture_wait_seconds=0,
            timelapse_mp4_present=True, newest_timelapse_age_s=3600)


def _eval(**metrics):
    m = dict(BASE); m.update(metrics)
    return health.evaluate(m, T)


def _probs(**metrics):
    return _eval(**metrics)[1]


def test_normal_blip_does_not_alert():
    # a healthy station: small live count, tiny nightly rate -> silence
    probs = _probs(dropped_frames_10min=40, summary={"dropped_frame_rate": 0.4})
    assert not any("rop" in p for p in probs)


def test_nightly_rate_over_threshold_alerts():
    probs = _probs(dropped_frames_10min=0, summary={"dropped_frame_rate": 6.5})
    assert any("6.5% of frames last night" in p for p in probs)


def test_nightly_rate_at_boundary_is_silent():
    # exactly at (not over) the 3% line -> no alert
    probs = _probs(summary={"dropped_frame_rate": T.dropped_frame_rate_warn_pct})
    assert not any("last night" in p for p in probs)


def test_nightly_rate_missing_is_safe():
    # no summary / unparseable rate -> no crash, no nightly alert
    assert not any("last night" in p for p in _probs(dropped_frames_10min=5))
    assert not any("last night" in p for p in _probs(summary={"dropped_frame_rate": None}))
    assert not any("last night" in p for p in _probs(summary={"dropped_frame_rate": "n/a"}))


def test_catastrophic_live_guard_pages():
    status, probs = _eval(dropped_frames_10min=T.dropped_frames_catastrophic,
                          summary={"dropped_frame_rate": 0.0})
    assert any("Severe live frame loss" in p for p in probs)
    assert status == health.ERROR


def test_moderate_live_count_no_longer_pages():
    # the old alert fired here (>=10); now a moderate live count is silent
    probs = _probs(dropped_frames_10min=800, summary={"dropped_frame_rate": 0.0})
    assert not any("rop" in p for p in probs)


def test_both_can_fire():
    status, probs = _eval(dropped_frames_10min=T.dropped_frames_catastrophic,
                          summary={"dropped_frame_rate": 40.0})
    assert any("last night" in p for p in probs)
    assert any("Severe live frame loss" in p for p in probs)
    assert status == health.ERROR


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok", name)
