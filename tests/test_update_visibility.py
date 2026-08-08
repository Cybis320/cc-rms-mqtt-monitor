"""A stuck auto-update must be visible, not silent.

autoupdate.sh refused the fast-forward, printed to the journal and exited 0, so systemd
recorded success and a station could sit on old code indefinitely while looking healthy.
Two live hosts were found frozen this way only by comparing published fields across the
fleet. Now the block reason is written to a marker file, the monitor publishes it (plus
the commit it is actually running), and health flags it.

Runs under pytest, or standalone: `python tests/test_update_visibility.py`.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor import health, oslevel                # noqa: E402
from cc_mqtt_monitor.config import Thresholds              # noqa: E402

T = Thresholds()


def test_blocked_update_is_flagged():
    status, problems = health.evaluate_host(
        {"monitor_update_blocked": "diverged from origin/master; auto-update skipped"}, T)
    assert any("auto-update is blocked" in p for p in problems)
    assert status == health.DEGRADED       # capture is unaffected; the code is just frozen


def test_healthy_update_is_silent():
    assert not any("auto-update" in p
                   for p in health.evaluate_host({"monitor_update_blocked": None}, T)[1])
    assert not any("auto-update" in p for p in health.evaluate_host({}, T)[1])


def test_marker_is_read_when_present():
    with tempfile.NamedTemporaryFile("w", suffix=".marker", delete=False) as fh:
        fh.write("diverged from origin/master (local commits or dirty tree)\n")
        path = fh.name
    old = oslevel.UPDATE_MARKER
    try:
        oslevel.UPDATE_MARKER = path
        got = oslevel.monitor_version()
        assert got["monitor_update_blocked"].startswith("diverged from origin/master")
    finally:
        oslevel.UPDATE_MARKER = old
        os.unlink(path)


def test_absent_marker_reports_none():
    old = oslevel.UPDATE_MARKER
    try:
        oslevel.UPDATE_MARKER = "/nonexistent/never/update_blocked"
        assert oslevel.monitor_version()["monitor_update_blocked"] is None
    finally:
        oslevel.UPDATE_MARKER = old


def test_commit_is_reported_for_a_git_checkout():
    """So the fleet can be compared against origin and stragglers spotted directly."""
    v = oslevel.monitor_version()
    assert "monitor_commit" in v
    if v["monitor_commit"] is not None:                    # this repo is a checkout
        assert len(v["monitor_commit"]) >= 7


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok", name)
