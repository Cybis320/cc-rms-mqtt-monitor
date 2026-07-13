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
         "buffer_fill_max_leadup": 34.0}
    v = health.classify_drops(m, {"cpu_busy_pct": 86.0, "cpu_iowait_pct": 0.0}, T)
    assert v["drop_cause"] == health.CAUSE_BACKPRESSURE
    assert "spiked to 34%" in v["drop_detail"]


def test_high_cpu_no_spike_is_not_backpressure():
    # A busy Pi runs hot; without a buffer spike that must NOT read as
    # back-pressure (CPU% is context, not a trigger).
    m = {"dropped_frames_10min": 26, "buffer_fill_max_leadup": 8.0,
         "capture_cpu_pct": 304.0}
    v = health.classify_drops(m, {"cpu_busy_pct": 86.0, "cpu_iowait_pct": 0.0}, T)
    assert v["drop_cause"] != health.CAUSE_BACKPRESSURE


def test_single_reconnect_spike_is_not_backpressure():
    # Regression (2026-07-13): ONE reconnect, not "churn". The lead-up was flat
    # baseline (5.0/5.4/4.6%) and the 52.5% fill appears only ON the drop line,
    # after a 47s gap in the 10s-cadence log -- i.e. the pipeline was rebuilt and
    # reported its startup fill. That is a consequence of the drop, not its cause.
    # Old behaviour: 1 < pipeline_reconnects_warn(3) => "stable" => back-pressure.
    m = {"dropped_frames_10min": 850, "buffer_fill_pct": 4.8,
         "buffer_fill_max_recent": 52.5,   # includes the drop line -- informational
         "buffer_fill_max_leadup": 5.4,    # flat baseline BEFORE the drop
         "pipeline_reconnects": 1}
    v = health.classify_drops(m, {"cpu_busy_pct": 30.0, "cpu_iowait_pct": 0.0}, T)
    assert v["drop_cause"] != health.CAUSE_BACKPRESSURE
    assert v["drop_cause"] == health.CAUSE_CAMERA_BW
    assert "1 reconnect (stream dropped and rebuilt)" in v["drop_detail"]


def test_concurrent_spike_alone_is_not_backpressure():
    # Even with zero reconnects, a spike that shows up ONLY at the drop line (so
    # the lead-up is flat) must not read as back-pressure -- the lead-up is what
    # decides. buffer_fill_max_recent being hot is irrelevant to the verdict.
    m = {"dropped_frames_10min": 900, "buffer_fill_max_recent": 52.5,
         "buffer_fill_max_leadup": 5.0, "pipeline_reconnects": 0}
    v = health.classify_drops(m, {"cpu_busy_pct": 30.0}, T)
    assert v["drop_cause"] != health.CAUSE_BACKPRESSURE


def test_leadup_unknown_never_claims_backpressure():
    # Drop line not in the scanned tail => lead-up can't be assessed => must not
    # claim back-pressure off the (drop-line-inclusive) recent peak.
    m = {"dropped_frames_10min": 900, "buffer_fill_max_recent": 60.0,
         "buffer_fill_max_leadup": None, "pipeline_reconnects": 0}
    v = health.classify_drops(m, {"cpu_busy_pct": 30.0}, T)
    assert v["drop_cause"] != health.CAUSE_BACKPRESSURE


def test_spike_with_reconnects_is_not_backpressure():
    # Every fresh (re)connection produces a startup buffer spike; when it rides
    # with reconnect churn it's a connection transient, NOT back-pressure -- it
    # must read as camera/link (the camera dropping the stream).
    m = {"dropped_frames_10min": 1746, "buffer_fill_max_leadup": 51.6,
         "pipeline_reconnects": 12, "decoder_errors": 9}
    v = health.classify_drops(m, {"cpu_busy_pct": 30.0}, T)
    assert v["drop_cause"] == health.CAUSE_CAMERA_BW
    assert "reconnect" in v["drop_detail"]


def test_flat_fill_decoder_errors_is_camera():
    # CAWEC4: flat fill (no spike), decoder corruption, host clean -> camera/link.
    m = {"dropped_frames_10min": 2214, "buffer_fill_max_leadup": 11.0,
         "decoder_errors": 12, "pipeline_reconnects": 9, "stream_mbps": 8.1}
    v = health.classify_drops(m, {"cpu_busy_pct": 5.0, "nic_rx_errors_per_min": 0.0}, T)
    assert v["drop_cause"] == health.CAUSE_CAMERA_BW


def test_udp_buffer_overflow_wins():
    m = {"dropped_frames_10min": 500, "buffer_fill_max_leadup": 8.0}
    v = health.classify_drops(m, {"udp_rcvbuf_errors_per_min": 120.0}, T)
    assert v["drop_cause"] == health.CAUSE_UDP_BUFFER


def test_no_drops_no_attribution():
    assert health.classify_drops({"dropped_frames_10min": 0}, {}, T)["drop_cause"] is None


def test_uncertain_message_depends_on_whether_probe_ran():
    # Drops with a clean host and no decoder/reconnect symptom.
    base = {"dropped_frames_10min": 300, "buffer_fill_max_leadup": 11.0,
            "decoder_errors": 0, "pipeline_reconnects": 0, "stream_mbps": 8.1}
    # Pre-probe: asks for a probe.
    pre = health.classify_drops(base, {}, T)
    assert pre["drop_cause"] == health.CAUSE_UNCERTAIN
    assert "probe to confirm" in pre["drop_detail"]
    # Post-probe (probe attached, all clean): points at the camera, no "confirm".
    probed = dict(base, probe_ping_note=None, probe_ping_loss_pct=0.0,
                  probe_keyframe_peak_kb=256.3, probe_stream_mbps=8.1)
    post = health.classify_drops(probed, {}, T)
    assert post["drop_cause"] == health.CAUSE_UNCERTAIN
    assert "probe to confirm" not in post["drop_detail"]
    assert "check the camera" in post["drop_detail"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("\nAll %d classify tests passed." % len(fns))
