"""Unit checks for collect_capture_events (session counters + stars_recent).

meteors accumulate (summed, reset at each day/night transition); stars do NOT --
`stars_recent` is the most recent per-FF "Detected stars: N", a live transparency
reading (last value wins, across the whole log).

Runs under pytest, or standalone: `python tests/test_capture_events.py`.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor import collect                        # noqa: E402


class _Station:
    def __init__(self, log_path):
        self._log = log_path


def _run(lines, now=None):
    path = os.path.join(tempfile.mkdtemp(), "log_X_1.log")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    # collect_capture_events finds the log via _newest_log(station); stub it.
    orig = collect._newest_log
    collect._newest_log = lambda station: path
    try:
        return collect.collect_capture_events(_Station(path), now=now)
    finally:
        collect._newest_log = orig


def test_stars_recent_is_last_value():
    res = _run([
        "...-DetectStarsAndMeteors-... - Detected stars: 40",
        "...-DetectStarsAndMeteors-... - Detected stars: 55",
        "...-DetectStarsAndMeteors-... - Detected stars: 12",
    ])
    assert res["stars_recent"] == 12


def test_stars_recent_survives_transition_reset():
    # Session counters reset at a transition; stars_recent is the latest overall.
    res = _run([
        "...Detected stars: 50",
        "...detected meteors: 3",
        "transition detected",
        "...Detected stars: 8",
    ])
    assert res["stars_recent"] == 8      # most recent, not reset
    assert res["meteors_session"] == 0   # meteors reset at the transition


def test_meteors_still_sum():
    res = _run(["...detected meteors: 2", "...detected meteors: 5",
                "...Detected stars: 30"])
    assert res["meteors_session"] == 7
    assert res["stars_recent"] == 30


def test_no_stars_line_is_none():
    res = _run(["...detected meteors: 1"])
    assert res["stars_recent"] is None


def test_capture_backend_found_far_from_tail():
    # RMS logs "GStreamer pipeline created!" ONCE at start; the full-log stream
    # must still find it after thousands of later lines (a tail scan would miss it).
    lines = ["2026/07/02 17:13:21-INFO-BufferedCapture-line:1421 - GStreamer pipeline created!"]
    lines += ["...WATCHDOG: Status check - capture_alive=True"] * 8000
    res = _run(lines)
    assert res["capture_backend"] == "gst"


def test_capture_backend_cv2_fallback_wins():
    # gst attempt then OpenCV init at startup => cv2 is the actual backend.
    res = _run([
        "...GStreamer pipeline created!",
        "...Using OpenCV.",
    ])
    assert res["capture_backend"] == "cv2"


def test_overflow_frame_reports_over_cap_not_zero():
    # "Too many candidate stars! 920/800" then "Detected stars: 0" => the frame was
    # too rich to count, so stars_recent is ">800", not a misleading 0.
    res = _run([
        "...-WARNING-ExtractStars-line:134 - Too many candidate stars to process! 920/800",
        "...-DetectStarsAndMeteors-line:231 - Detected stars: 0",
    ])
    assert res["stars_recent"] == ">800"


def test_genuine_zero_stays_zero():
    # A plain "Detected stars: 0" with no preceding overflow is a real 0 (washout).
    res = _run(["...Detected stars: 0"])
    assert res["stars_recent"] == 0


def test_overflow_does_not_leak_to_next_frame():
    # The ">cap" applies only to the overflow frame; a later normal frame is an int.
    res = _run([
        "...Too many candidate stars to process! 920/800",
        "...Detected stars: 0",
        "...Detected stars: 137",
    ])
    assert res["stars_recent"] == 137


DAWN_DIR = ("2026/07/18 12:09:00-INFO-StartCapture-line:346 - Data directory: "
            "/home/ops/RMS_data/CapturedFiles/US005A_20260718_031234_567890")
DAWN_START = ("2026/07/18 12:10:00-INFO-StartCapture-line:752 - "
              "Finishing up the detection, 42 files to process...")


def test_dawn_processing_active():
    import calendar, time as _time
    now = calendar.timegm(_time.strptime("2026/07/18 12:15:00", "%Y/%m/%d %H:%M:%S"))
    res = _run([
        DAWN_DIR,
        DAWN_START,
        "2026/07/18 12:10:02-INFO-StartCapture-line:783 - Waiting for the detection to finish...",
    ], now=now)
    assert res["processing_active"] is True
    assert res["processing_kind"] == "dawn"
    assert res["processing_dir"] == "US005A_20260718_031234_567890"
    # Age counts from the FIRST dawn marker (12:10:00), not the later one.
    assert res["processing_age_s"] == 300.0


def test_dawn_processing_ends_when_next_session_starts():
    res = _run([
        DAWN_DIR,
        DAWN_START,
        "2026/07/18 12:40:00-INFO-StartCapture-line:479 - Capturing in daytime mode...",
    ])
    assert res["processing_active"] is False
    assert res["processing_kind"] is None
    assert res["processing_dir"] is None
    assert res["processing_age_s"] is None


def test_dawn_processing_ends_at_standard_mode_wait():
    # Standard (non-continuous) mode: processing done -> back to waiting.
    res = _run([
        DAWN_START,
        "2026/07/18 12:40:00-INFO-StartCapture-line:1232 - Next start time: 2026-07-19 03:10:00 UTC",
    ])
    assert res["processing_active"] is False


def test_startup_reprocess_active_then_done():
    found = ("2026/07/18 12:00:00-INFO-StartCapture-line:986 - Found partially-processed "
             "data in /home/ops/RMS_data/CapturedFiles/US005A_20260717_031234_567890")
    res = _run([found])
    assert res["processing_active"] is True
    assert res["processing_kind"] == "reprocess"
    assert res["processing_dir"] == "US005A_20260717_031234_567890"

    res = _run([
        found,
        "2026/07/18 12:30:00-INFO-StartCapture-line:1011 - Folder /home/ops/RMS_data/"
        "CapturedFiles/US005A_20260717_031234_567890 reprocessed with success!",
    ])
    assert res["processing_active"] is False
    assert res["processing_kind"] is None


def test_reprocess_error_line_ends_episode():
    res = _run([
        "...Found partially-processed data in /d/CapturedFiles/US005A_x",
        "...-ERROR-StartCapture-line:1015 - An error occurred when trying to reprocess "
        "partially processed data!",
    ])
    assert res["processing_active"] is False


def test_no_processing_lines_reports_inactive():
    res = _run(["...Detected stars: 30"])
    assert res["processing_active"] is False
    assert res["processing_kind"] is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("\nAll %d capture-events tests passed." % len(fns))
