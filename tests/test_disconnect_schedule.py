"""A camera auto-reboot is not a fault: tell scheduled disconnects from unplanned ones.

The camera's firmware reboots it daily at a fixed clock time (CameraControl
setAutoReboot), which RMS's config cannot reveal. Each reboot drops the RTSP stream and
was counted as a disconnect, so every camera wore an amber "fault" badge once a day. The
signature is timing: a single disconnect ~24 h after another.

Runs under pytest, or standalone: `python tests/test_disconnect_schedule.py`.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor import collect                        # noqa: E402

DAY = 86400
T0 = 1_790_000_000                                         # some day, 15:00


def test_same_time_next_day_is_scheduled():
    assert collect.classify_disconnects([T0 + DAY + 40], [T0]) == (1, 0)


def test_two_days_later_still_scheduled():
    """A missed day (box down, log rotated) must not break the pattern."""
    assert collect.classify_disconnects([T0 + 2 * DAY - 300], [T0]) == (1, 0)


def test_off_schedule_is_unplanned():
    assert collect.classify_disconnects([T0 + DAY + 3 * 3600], [T0]) == (0, 1)


def test_first_ever_is_honestly_unplanned():
    """No predecessor -> no pattern yet; never assume it away."""
    assert collect.classify_disconnects([T0], []) == (0, 1)


def test_same_day_repeat_is_not_scheduled():
    """Two drops an hour apart today are instability, not the daily reboot."""
    assert collect.classify_disconnects([T0 + 3600], [T0]) == (0, 1)


def test_reboot_burst_counts_together():
    """One reboot can need two reconnects; both belong to the scheduled event."""
    assert collect.classify_disconnects([T0 + DAY, T0 + DAY + 240], [T0]) == (2, 0)


def test_scheduled_plus_a_real_drop():
    assert collect.classify_disconnects([T0 + DAY, T0 + DAY + 5 * 3600], [T0]) == (1, 1)


# --- end to end through the real log scanner, across a monitor restart ---------------

class _Station:
    def __init__(self, sid):
        self.station_id = sid


def _log_line(epoch):
    ts = time.strftime("%Y/%m/%d %H:%M:%S", time.gmtime(epoch))
    return ts + "-WARNING-BufferedCapture-line:900 - video device is probably disconnected"


def _scan(lines, sid, now):
    path = os.path.join(tempfile.mkdtemp(), "log_%s_1.log" % sid)
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    orig = collect._newest_log
    collect._newest_log = lambda station: path
    try:
        return collect.collect_capture_events(_Station(sid), now=now)
    finally:
        collect._newest_log = orig


def test_end_to_end_second_day_reboot_is_scheduled():
    state = tempfile.mkdtemp()
    os.environ["CC_CLIENT_ID_FILE"] = os.path.join(state, "client_suffix")
    from cc_mqtt_monitor import config
    real = config.state_paths
    config.state_paths = lambda name, env=None: [os.path.join(state, name)]
    try:
        day1 = _scan([_log_line(T0)], "TST001", now=T0 + 60)
        assert (day1["disconnects_scheduled_session"], day1["disconnects_unplanned_session"]) == (0, 1)
        # next day: new log (rotated), new process -- history must come from disk
        day2 = _scan([_log_line(T0 + DAY + 90)], "TST001", now=T0 + DAY + 120)
        assert day2["disconnects_session"] == 1, "historical count unchanged in meaning"
        assert (day2["disconnects_scheduled_session"], day2["disconnects_unplanned_session"]) == (1, 0)
        # rescanning the same log must not let today's drop vouch for itself
        again = _scan([_log_line(T0 + DAY + 90)], "TST001", now=T0 + DAY + 180)
        assert again["disconnects_scheduled_session"] == 1
    finally:
        config.state_paths = real


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok", name)
