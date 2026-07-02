"""Unit checks for the systemd-journal fatal scan (startup/build crashes).

An import/build crash kills StartCapture before RMS file logging is up, so the
traceback goes to stderr -> the systemd journal, never the RMS log. collect.py
resolves the capture's systemd unit from its cgroup (so it's a clean no-op for a
terminal/screen launch, whose output isn't journal-captured) and scans that
unit's journal so the real error reaches last_error / log_fatal.

Runs under pytest, or standalone: `python tests/test_journal_fatal.py`.
"""

import io
import os
import sys
import builtins

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor import collect                        # noqa: E402


def _cgroup(data):
    """Run _proc_unit against a fixture /proc/<pid>/cgroup body."""
    real_open = builtins.open
    builtins.open = lambda *a, **k: io.StringIO(data)
    try:
        return collect._proc_unit(4242)
    finally:
        builtins.open = real_open


def test_cgroup_resolves_system_service():
    assert _cgroup("0::/system.slice/au0004.service\n") == "au0004.service"


def test_cgroup_resolves_user_service():
    assert _cgroup("0::/user.slice/user-1001.slice/user@1001.service/app.slice/"
                   "au0004.service\n") == "au0004.service"


def test_cgroup_ignores_terminal_scope():
    # gnome-terminal vte-spawn scope: NOT a service -> nothing to scan.
    assert _cgroup("0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
                   "app-org.gnome.Terminal.slice/vte-spawn-abc.scope\n") is None


def test_cgroup_ignores_bare_user_manager_and_session():
    assert _cgroup("0::/user.slice/user-1000.slice/user@1000.service\n") is None
    assert _cgroup("0::/user.slice/user-1000.slice/session-3.scope\n") is None


def test_no_pid_no_unit():
    assert collect._proc_unit(None) is None


# The real AU0004 crash (pyximport rebuilding Kht.c after an update, gcc fails).
_CRASH = """[INFO] Nothing new on feature/systemd - exiting.
Kht.c:1135:12: error: expected identifier or '(' before string constant
distutils.compilers.C.errors.CompileError: command '/usr/bin/gcc' failed with exit code 1
Traceback (most recent call last):
  File "/home/au0004/source/RMS/RMS/StartCapture.py", line 46, in <module>
    from RMS.DetectStarsAndMeteors import detectStarsAndMeteors
  File "/home/au0004/source/RMS/RMS/Detection.py", line 46, in <module>
    from RMS.Routines import Kht
ImportError: Building module RMS.Routines.Kht failed
""".splitlines()


class _Station:
    station_id = "AU0004"


def _with_journal(unit, lines, fn):
    """Run fn() with _capture_unit/_journal_tail stubbed."""
    cu, jt = collect._capture_unit, collect._journal_tail
    collect._capture_unit = lambda station, pid: unit
    collect._journal_tail = lambda u, n: lines
    try:
        return fn()
    finally:
        collect._capture_unit, collect._journal_tail = cu, jt


def test_crash_surfaces_even_without_live_pid():
    # main_pid=None (process gone) still resolves via the (stubbed) cached unit.
    res = _with_journal("au0004.service", _CRASH,
                        lambda: collect.collect_journal_fatal(_Station(), None))
    assert res["fatal_error_count"] >= 1
    assert res["fatal_source"] == "journal"
    assert res["last_error"].startswith("ImportError: Building module RMS.Routines.Kht failed")


def test_non_systemd_capture_is_noop():
    # No unit (terminal/screen launch) -> nothing scanned, empty result.
    res = _with_journal(None, _CRASH,
                        lambda: collect.collect_journal_fatal(_Station(), 123))
    assert res == {}


def test_clean_journal_returns_empty():
    res = _with_journal("au0004.service", ["all good", "capturing"],
                        lambda: collect.collect_journal_fatal(_Station(), 1))
    assert res == {}


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("\nAll %d journal-fatal tests passed." % len(fns))
