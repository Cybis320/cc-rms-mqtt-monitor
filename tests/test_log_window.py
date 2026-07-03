"""Unit checks for the time-window on log-scan alerts (collect_logs window_s).

The fatal/warning/watchdog/decoder/reconnect matches count only if their log
timestamp is within `now - window_s`, so those alerts clear a fixed time after the
last occurrence rather than when the line scrolls out of the fixed-size tail.

Runs under pytest, or standalone: `python tests/test_log_window.py`.
"""

import os
import sys
import time
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor import collect                        # noqa: E402

NOW = 1782000000.0


def _ts(sec_ago):
    return time.strftime("%Y/%m/%d %H:%M:%S", time.gmtime(NOW - sec_ago))


def _logs(lines, window_s):
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "log_US005A_1.log"), "w") as fh:
        fh.write("\n".join(lines) + "\n")

    class St:
        station_id = "US005A"
        log_path = d
        media_backend = "gst"
    return collect.collect_logs(St(), 4000, now=NOW, window_s=window_s)


def test_recent_warning_counts_old_does_not():
    lines = [_ts(3 * 3600) + "-WARNING-Foo-line:1 - old",
             _ts(600) + "-WARNING-Foo-line:2 - recent"]
    assert _logs(lines, 7200)["warning_count"] == 1        # only the 10-min-old one
    assert _logs(lines, None)["warning_count"] == 2        # no window -> both


def test_old_traceback_body_inherits_timestamp():
    # A multi-line traceback: only the header is timestamped; the body must inherit
    # it so an OLD traceback is dropped whole (not kept because its body is undated).
    lines = [_ts(3 * 3600) + "-ERROR-Bar-line:1 - Traceback (most recent call last):",
             '  File "x.py", line 5, in <module>',
             "ValueError: boom"]
    assert _logs(lines, 7200)["fatal_error_count"] == 0
    assert _logs(lines, None)["fatal_error_count"] == 1


def test_recent_fatal_still_counts():
    lines = [_ts(300) + "-ERROR-Bar-line:1 - Traceback (most recent call last):",
             "ImportError: nope"]
    r = _logs(lines, 7200)
    assert r["fatal_error_count"] >= 1        # recent -> within the window
    assert "ImportError" in (r["last_error"] or "")


def test_watchdog_windowed():
    lines = [_ts(3 * 3600) + "-INFO-X - WATCHDOG: Restarting BufferedCapture died"]
    assert _logs(lines, 7200)["last_watchdog_event"] is None
    assert _logs(lines, None)["last_watchdog_event"] is not None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("\nAll %d log-window tests passed." % len(fns))
