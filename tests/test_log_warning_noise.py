"""The log_warning bucket: mute self-recovering chatter, keep the actionable lines.

Audited from a fortnight of live alerts (859 log-warning mentions, ~61/day). Every string
below is a REAL observed message. The muted ones describe RMS's own recovery working, a
duplicate of a dedicated check, or a science-pipeline choice -- none of which say the
station is unhealthy. The kept ones are operator-actionable.

Runs under pytest, or standalone: `python tests/test_log_warning_noise.py`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor import collect                       # noqa: E402

IGNORE = collect._compile_warning_ignore(None)            # built-in defaults only

MUTED = [
    # capture teardown at day<->night switches / reconnects -- RMS recovers and continues
    "BufferedCapture-line:1658 - releaseResources: cap.release() hung - fd dropped",
    "BufferedCapture-line:1671 - releaseResources: set_state(NULL) did not return within 5s - abandoning teardown",
    "BufferedCapture-line:1402 - RawFrameSaver still busy. Terminating...",
    # duplicate of the dedicated watchdog check
    "StartCapture-line:812 - WATCHDOG: Force terminating hung process...",
    # science-pipeline decisions
    "Flux-line:1220 - No valid recalibrated platepar this night - cannot score frames with the dome model",
    "Flux-line:990 - Dome model rejected for this night: median matched/expected = 0.4 exceeds the limit",
    "Flux-line:770 - Could not update the star calibration: zero-size array to reduction operation",
    "ApplyRecalibrate-line:401 - recalibrateSelectedFF: no FF files after filtering - skipping recalibration",
    # routine nightly alignment bookkeeping
    "FFTalign-line:355 - Rotation: 1.8 deg, limit of 5.0 deg",
    "FFTalign-line:355 - Rotation: -2.4 deg, limit of 5.0 deg",
    # third-party library noise
    "Logger-line:77 - ~/source/RMS/RMS/BufferedCapture.py:191: Warning: g_type_class_unref: assertion failed",
]

KEPT = [
    # a box that cannot reboot itself is a real problem (and pairs with the OOM advisory)
    "StartCapture-line:640 - Reboot failed after 6 hours of attempts, resuming capture",
    # a write failure can mean the disk is going -- never muted with the teardown chatter
    "BufferedCapture-line:900 - GST WARN  from src: gst-resource-error-quark: Could not write to resource. (9)",
    "BufferedCapture-line:900 - GST WARN  from src: gst-resource-error-quark: Could not read from resource. (9)",
    # upload config faults stay actionable
    "UploadManager-line:64 - Agent authentication failed. No valid authorized keys found.",
    "UploadManager-line:70 - SSH error with provided key: encountered RSA key, expected OPENSSH key",
    # the hard FFTalign failure, as opposed to the routine rotation report
    "FFTalign-line:360 - imreg_dft error: The scale correction is too high!",
    # sky-quality / pedestal movement stays visible
    "SkyQuality-line:210 - Sky quality: floor guard tripped - pedestal appears to have moved (floor 12.5 ADU)",
]


def _full(msg):
    """A real log line as the filter sees it: RMS stamps time + level + module."""
    return "2026/07/31 13:35:23-WARNING-" + msg


def test_noise_is_muted():
    for line in MUTED:
        assert IGNORE.search(_full(line)), "should be muted: %s" % line


def test_actionable_still_alerts():
    for line in KEPT:
        assert not IGNORE.search(_full(line)), "should still alert: %s" % line


def test_watchdog_detection_is_unaffected():
    """Muting the WATCHDOG line in the log_warning filter must NOT disarm the dedicated
    watchdog check -- that runs off its own regex, which the ignore filter never touches."""
    assert collect._WATCHDOG_RE.search("WATCHDOG: Capture died, Restarting...")
    assert collect._WATCHDOG_RE.search("WATCHDOG: process stale, Restarting")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok", name)
