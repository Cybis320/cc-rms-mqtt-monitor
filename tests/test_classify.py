"""Unit checks for dropped-frame attribution (health.classify_drops).

Runs under pytest, or standalone: `python tests/test_classify.py`.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor import health                       # noqa: E402
from cc_mqtt_monitor.config import Thresholds            # noqa: E402

T = Thresholds()


def test_spike_then_drop_is_backpressure():
    # Buffer fill recovered to baseline at the drop line (4.2%), but it SPIKED to
    # 34% in the lead-up -- the back-pressure signature. CPU is high but that's
    # not what decides it.
    m = {"dropped_frames_10min": 1132, "buffer_fill_pct": 4.2,
         "buffer_fill_max_recent": 34.0}
    v = health.classify_drops(m, {"cpu_busy_pct": 86.0, "cpu_iowait_pct": 0.0}, T)
    assert v["drop_cause"] == health.CAUSE_BACKPRESSURE
    assert "spiked to 34%" in v["drop_detail"]


def test_high_cpu_no_spike_is_not_backpressure():
    # A busy Pi runs hot; without a buffer spike that must NOT read as
    # back-pressure (CPU% is context, not a trigger).
    m = {"dropped_frames_10min": 26, "buffer_fill_max_recent": 8.0,
         "capture_cpu_pct": 304.0}
    v = health.classify_drops(m, {"cpu_busy_pct": 86.0, "cpu_iowait_pct": 0.0}, T)
    assert v["drop_cause"] != health.CAUSE_BACKPRESSURE


def test_spike_with_reconnects_is_not_backpressure():
    # Every fresh (re)connection produces a startup buffer spike; when it rides
    # with reconnect churn it's a connection transient, NOT back-pressure -- it
    # must read as camera/link (the camera dropping the stream).
    m = {"dropped_frames_10min": 1746, "buffer_fill_max_recent": 51.6,
         "pipeline_reconnects": 12, "decoder_errors": 9}
    v = health.classify_drops(m, {"cpu_busy_pct": 30.0}, T)
    assert v["drop_cause"] == health.CAUSE_CAMERA_BW
    assert "reconnect" in v["drop_detail"]


def test_flat_fill_decoder_errors_is_camera():
    # CAWEC4: flat fill (no spike), decoder corruption, host clean -> camera/link.
    m = {"dropped_frames_10min": 2214, "buffer_fill_max_recent": 11.0,
         "decoder_errors": 12, "pipeline_reconnects": 9, "stream_mbps": 8.1}
    v = health.classify_drops(m, {"cpu_busy_pct": 5.0, "nic_rx_errors_per_min": 0.0}, T)
    assert v["drop_cause"] == health.CAUSE_CAMERA_BW


def test_udp_buffer_overflow_wins():
    m = {"dropped_frames_10min": 500, "buffer_fill_max_recent": 8.0}
    v = health.classify_drops(m, {"udp_rcvbuf_errors_per_min": 120.0}, T)
    assert v["drop_cause"] == health.CAUSE_UDP_BUFFER


def test_no_drops_no_attribution():
    assert health.classify_drops({"dropped_frames_10min": 0}, {}, T)["drop_cause"] is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("\nAll %d classify tests passed." % len(fns))
