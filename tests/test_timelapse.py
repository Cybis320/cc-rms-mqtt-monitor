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


from cc_mqtt_monitor import health                         # noqa: E402
from cc_mqtt_monitor.config import Thresholds              # noqa: E402

_T = Thresholds()
# Actively saving frames (fresh), long-running capture (not settling).
_SAVING = dict(capture_alive=True, expected_output="frames", newest_fits_age_s=None,
               newest_frame_age_s=30, capture_age_s=999999, capture_wait_seconds=0)


def _overdue(**tl):
    _s, problems = health.evaluate(dict(_SAVING, **tl), _T)
    return any("while saving frames" in p for p in problems)


def test_overdue_present_but_old_mp4_does_not_fire():
    # A present-but-old newest mp4 = no recent completed session, NOT a fault.
    assert not _overdue(newest_timelapse_age_s=40 * 3600, timelapse_mp4_present=True)


def test_overdue_no_mp4_ever_fires():
    assert _overdue(newest_timelapse_age_s=None, frames_data_age_s=40 * 3600)


def test_overdue_last_session_failed_fires():
    # Newest completed session produced no mp4 (ffmpeg failed) and it's old.
    assert _overdue(newest_timelapse_age_s=40 * 3600, timelapse_mp4_present=False)


def test_overdue_recent_mp4_does_not_fire():
    assert not _overdue(newest_timelapse_age_s=10 * 3600, timelapse_mp4_present=True)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("\nAll %d timelapse tests passed." % len(fns))
