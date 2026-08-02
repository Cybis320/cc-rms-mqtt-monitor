"""gather_host must actually POPULATE capture_restart_age_s, not just consult it.

Regression: the OOM "has capture restarted since?" recovery shipped calling
`collect_capture()` -- which returns capture SESSION freshness and carries no process
age -- instead of `collect_process()`. `.get("capture_age_s")` quietly returned None
for every station, so the field was never published and the OOM advisory never cleared.
No exception was raised; the broad try/except hid it.

The unit tests for evaluate_host all passed, because they fed the field in by hand. What
was missing was a check that the PRODUCER supplies the key the CONSUMER reads.

Runs under pytest, or standalone: `python tests/test_host_capture_restart.py`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor import collect, monitor              # noqa: E402


class _Station(object):
    """Minimal stand-in. Both collectors tolerate paths that do not exist -- they simply
    report "nothing found", which is exactly the state we want to inspect the KEYS of."""
    station_id = "TEST01"
    config_path = "/nonexistent/.config"
    captured_path = "/nonexistent/CapturedFiles"
    archived_path = "/nonexistent/ArchivedFiles"
    data_dir = "/nonexistent"


def test_collect_process_supplies_capture_age_s():
    """The key evaluate_host's recovery check depends on. Present even with no capture
    running (None), which is what makes the wrong-collector bug silent -- so assert the
    KEY exists, not merely that .get() returns something."""
    res = collect.collect_process(_Station())
    assert "capture_age_s" in res, "collect_process must supply capture_age_s"


def test_collect_capture_does_not_supply_it():
    """Guards the exact mix-up: collect_capture is session freshness, not process age.
    If this ever starts returning capture_age_s the two collectors have converged and the
    comment in gather_host should be revisited -- but silently swapping them must not
    become harmless-looking again."""
    res = collect.collect_capture(_Station())
    assert "capture_age_s" not in res


def test_gather_host_reads_the_right_collector():
    """gather_host must call the process collector for the age, not the session one."""
    import inspect
    src = inspect.getsource(monitor.gather_host)
    assert "collect_process(" in src, "gather_host must use collect_process for capture age"
    assert "collect_capture(" not in src, "collect_capture carries no capture_age_s"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok", name)
