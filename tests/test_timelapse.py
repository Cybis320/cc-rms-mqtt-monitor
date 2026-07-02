"""Unit checks for collect_timelapse artifact recognition.

RMS writes <session>_frames_timelapse.mp4, and with frame_cleanup=delete (its
DEFAULT) archives it to <session>_frames_timelapse.tar and removes the mp4. Either
form counts as "a timelapse was produced" -- an mp4-only view false-fires the
timelapse_overdue / timelapse_missing checks.

Runs under pytest, or standalone: `python tests/test_timelapse.py`.
"""

import os
import sys
import time
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor import collect                        # noqa: E402


class _St:
    save_frames = True
    timelapse_generate_from_frames = True
    def __init__(self, fp):
        self._fp = fp
    @property
    def frames_path(self):
        return self._fp


def _fp(*files):
    d = tempfile.mkdtemp()
    for name in files:
        with open(os.path.join(d, name), "w") as fh:
            fh.write("x" * 2_000_000)   # > _MIN_TIMELAPSE_BYTES
    return d


def test_loose_mp4_counts():
    d = _fp("AU0004_20260701_100619_to_200000_frames_timelapse.mp4")
    res = collect.collect_timelapse(_St(d), time.time())
    assert res["newest_timelapse_age_s"] is not None


def test_archived_tar_counts():
    # frame_cleanup=delete leaves only the tar -- must still count as produced.
    d = _fp("AU0004_20260701_100619_to_200000_frames_timelapse.tar")
    res = collect.collect_timelapse(_St(d), time.time())
    assert res["newest_timelapse_age_s"] is not None


def test_tar_marks_session_present():
    # json (session marker) + tar (archived success), no loose mp4.
    d = _fp("AU0004_20260701_100619_to_200000_frametimes.json",
            "AU0004_20260701_100619_to_200000_frames_timelapse.tar")
    res = collect.collect_timelapse(_St(d), time.time())
    assert res["timelapse_mp4_present"] is True


def test_no_timelapse_at_all_is_none():
    d = _fp("AU0004_20260701_100619_to_200000_frametimes.json")   # json only, no output
    res = collect.collect_timelapse(_St(d), time.time())
    assert res["newest_timelapse_age_s"] is None
    assert res["timelapse_mp4_present"] is False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("\nAll %d timelapse tests passed." % len(fns))
