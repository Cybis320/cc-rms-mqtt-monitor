"""Unit checks for the lead-up buffer-fill peak (collect.collect_logs).

Back-pressure means the appsink buffer was ALREADY backing up when frames started
dropping, so the elevated fill must show on the lines PRECEDING the drop. The fill
AT the drop line is concurrent with the event: a pipeline reconnect tears the
stream down (frames lost) and the rebuilt pipeline reports a big startup fill on
that very line. buffer_fill_max_leadup must therefore ignore the drop line and
everything after it.

Runs under pytest, or standalone: `python tests/test_buffer_leadup.py`.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor import collect                      # noqa: E402


class _Station(object):
    station_id = "XX0001"
    media_backend = "gst"

    def __init__(self, log_path):
        self.log_path = log_path


def _scan(lines):
    """Write lines to a real RMS-named log and run collect_logs over it."""
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "log_XX0001_20260713_020000_001.log")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return collect.collect_logs(_Station(tmp), max_lines=500)


def _buf(ts, fill, recent, session):
    return ("2026/07/13 %s-INFO-BufferedCapture-line:2323 - Buffer fill: %.1f%%. "
            "Dropped frames: %d (last 10 min), %d this session" % (ts, fill, recent, session))


# The real incident (2026-07-13, 02:05-02:07). Flat baseline, then a 47s gap in
# the 10s-cadence log (the pipeline rebuild) and a 52.5% fill ON the drop line.
_INCIDENT = [
    _buf("02:05:40", 5.0, 0, 2405),
    _buf("02:05:50", 5.4, 0, 2405),
    _buf("02:06:00", 4.6, 0, 2405),
    _buf("02:06:47", 52.5, 850, 3255),   # <- drop line: fill is the reconnect transient
    _buf("02:06:57", 4.4, 850, 3255),
    _buf("02:07:07", 4.8, 850, 3255),
]


def test_leadup_excludes_the_drop_line():
    r = _scan(_INCIDENT)
    # The lead-up was flat baseline -- the 52.5% must NOT leak in.
    assert r["buffer_fill_max_leadup"] == 5.4
    # The recent peak still reports it (informational context).
    assert r["buffer_fill_max_recent"] == 52.5
    assert r["dropped_frames_10min"] == 850


def test_leadup_catches_a_genuine_preceding_spike():
    # True back-pressure: the buffer is already climbing BEFORE frames drop.
    lines = [
        _buf("02:05:40", 6.0, 0, 100),
        _buf("02:05:50", 22.0, 0, 100),
        _buf("02:06:00", 34.0, 0, 100),    # backing up ahead of the drop
        _buf("02:06:10", 4.2, 700, 800),   # drop line: fill already recovered
    ]
    assert _scan(lines)["buffer_fill_max_leadup"] == 34.0


def test_no_drop_in_tail_leaves_leadup_unset():
    lines = [_buf("02:05:40", 5.0, 0, 100), _buf("02:05:50", 5.4, 0, 100)]
    r = _scan(lines)
    assert r["buffer_fill_max_leadup"] is None
    assert r["buffer_fill_max_recent"] == 5.4


def test_ongoing_backpressure_still_detected():
    # Drops on every line: the lead-up lines themselves carry the elevated fill,
    # so picking the LAST drop still sees a hot lead-up.
    lines = [
        _buf("02:05:40", 40.0, 100, 100),
        _buf("02:05:50", 45.0, 200, 200),
        _buf("02:06:00", 42.0, 300, 300),
    ]
    assert _scan(lines)["buffer_fill_max_leadup"] == 45.0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("\nAll %d lead-up tests passed." % len(fns))
