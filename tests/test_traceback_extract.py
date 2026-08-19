"""Unit checks for traceback summary extraction (_extract_traceback).

RMS logs contain no blank lines, so a traceback block ends where the next
timestamped log line resumes. Scanning for a blank line instead swallowed the
rest of the tail, and last_error reported whatever line happened to be last at
scan time (e.g. a routine "Grabbing a new block of 256 frames...").

Runs under pytest, or standalone: `python tests/test_traceback_extract.py`.
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


def _logs(lines):
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "log_US005A_1.log"), "w") as fh:
        fh.write("\n".join(lines) + "\n")

    class St:
        station_id = "US005A"
        log_path = d
        media_backend = "gst"
    return collect.collect_logs(St(), 4000, now=NOW, window_s=None)


def test_summary_is_exception_line_not_tail_end():
    # The block must stop at the next timestamped line, so the summary is the
    # exception line -- not the newest routine line in the tail.
    lines = [_ts(600) + "-ERROR-Reprocess-line:938 - Transparency demo video failed",
             "Traceback (most recent call last):",
             '  File "x.py", line 241, in generateDemoVideo',
             "FileNotFoundError: [Errno 2] No such file or directory: 'pp.json'",
             _ts(300) + "-INFO-BufferedCapture-line:2082 - Grabbing a new block of 256 frames...",
             _ts(200) + "-INFO-BufferedCapture-line:2356 - Buffer fill: 8.4%."]
    r = _logs(lines)
    assert "FileNotFoundError" in (r["last_error"] or "")
    assert "Grabbing" not in (r["last_error"] or "")


def test_blank_line_still_ends_block():
    # Journal-style input (no RMS timestamps) keeps the blank-line terminator.
    lines = ["Traceback (most recent call last):",
             '  File "y.py", line 1, in <module>',
             "ImportError: nope",
             "",
             "unrelated trailing text"]
    r = _logs(lines)
    assert "ImportError" in (r["last_error"] or "")
    assert "unrelated" not in (r["last_error"] or "")


def test_traceback_at_end_of_tail():
    # No terminator at all: the exception line is the last line of the tail.
    lines = [_ts(60) + "-ERROR-Bar-line:1 - Traceback (most recent call last):",
             "ValueError: boom"]
    r = _logs(lines)
    assert "ValueError" in (r["last_error"] or "")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("\nAll %d traceback-extract tests passed." % len(fns))
