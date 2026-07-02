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


def _run(lines):
    path = os.path.join(tempfile.mkdtemp(), "log_X_1.log")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    # collect_capture_events finds the log via _newest_log(station); stub it.
    orig = collect._newest_log
    collect._newest_log = lambda station: path
    try:
        return collect.collect_capture_events(_Station(path))
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


def test_star_overflow_detected_and_flagged():
    from cc_mqtt_monitor import health
    from cc_mqtt_monitor.config import Thresholds
    line = ("2026/07/02 07:15:07-WARNING-ExtractStars-line:134 - "
            "Too many candidate stars to process! 920/800")
    # It's collected by collect_logs (tail scan), not collect_capture_events.
    path = os.path.join(tempfile.mkdtemp(), "log_US005A_1.log")
    with open(path, "w") as fh:
        fh.write(line + "\n")

    class St:
        station_id = "US005A"
        log_path = os.path.dirname(path)
        media_backend = "gst"
    m = collect.collect_logs(St(), 4000)
    assert m["star_overflow"] is True
    assert m["star_candidates"] == 920 and m["star_candidate_limit"] == 800
    # ...and it must NOT also trip the generic log_warning (still on the ignore list)
    assert m["warning_count"] == 0

    status, problems = health.evaluate({"capture_alive": True, **m}, Thresholds())
    assert status == "degraded"
    assert any("Star extraction overflowing" in p for p in problems)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("\nAll %d capture-events tests passed." % len(fns))
