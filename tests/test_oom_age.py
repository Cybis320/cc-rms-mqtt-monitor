"""OOM alert must self-clear once the episode is over.

An OOM-kill is counted from a fixed-size kernel-log window, so a single past kill
keeps being counted long after memory recovered. The host-health OOM flag is gated
on recency (oom_last_age_s) so it clears once no kill has happened for oom_recent_s.

Runs under pytest, or standalone: `python tests/test_oom_age.py`.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor import health, oslevel              # noqa: E402
from cc_mqtt_monitor.config import Thresholds            # noqa: E402

T = Thresholds()


def _oom(age_s, victim="python", count=6, uptime=1_000_000, capture_age=None):
    # uptime defaults large, so by default the OOM is "this boot" (age < uptime).
    # capture_age None = capture has NOT restarted since (older than the OOM is implied
    # by leaving it unset, which is the "still damaged" case).
    m = {"oom_kill_count": count, "last_oom_victim": victim, "oom_last_age_s": age_s,
         "uptime_s": uptime, "mem_available_mb": 10000}
    if capture_age is not None:
        m["capture_restart_age_s"] = capture_age
    return m


def test_fresh_oom_is_error():
    status, problems = health.evaluate_host(_oom(60), T)      # 1 min ago, python victim
    assert any("OOM-killer fired" in p for p in problems)
    assert status == health.ERROR


def test_old_oom_without_a_capture_restart_still_advises():
    # OOM'd this boot, past the fresh window, capture NOT restarted since -> still flagged.
    status, problems = health.evaluate_host(_oom(T.oom_recent_s + 3600, uptime=1_000_000), T)
    assert any("has not restarted since" in p for p in problems)
    assert status == health.DEGRADED


def test_capture_restart_since_the_oom_clears_it():
    """The real case that exposed this: a host 17 d past an OOM cascade, NEVER rebooted
    (35 d uptime), but capture restarted 6 d ago -> recovered, must not keep flagging."""
    day = 86400
    m = _oom(17 * day, uptime=35 * day, capture_age=6 * day)
    status, problems = health.evaluate_host(m, T)
    assert not any("OOM" in p for p in problems)


def test_capture_older_than_the_oom_does_not_count_as_recovery():
    # capture predates the kill -> it was never replaced -> keep advising
    day = 86400
    m = _oom(5 * day, uptime=35 * day, capture_age=20 * day)
    assert any("has not restarted since" in p for p in health.evaluate_host(m, T)[1])


def test_rebooted_since_oom_clears():
    # the kill PREDATES the current boot (age > uptime) -> box was rebooted -> clear.
    status, problems = health.evaluate_host(_oom(50_000, uptime=1_000), T)
    assert not any("OOM" in p for p in problems)


def test_unparseable_age_still_flags():
    # if the kernel-log timestamp couldn't be parsed we must not go silent on a real OOM
    status, problems = health.evaluate_host(_oom(None), T)
    assert any("OOM-killer fired" in p for p in problems)


def test_missing_uptime_still_flags_within_window():
    # no uptime -> can't prove a reboot -> flag (fresh here)
    m = _oom(60); m.pop("uptime_s")
    assert any("OOM-killer fired" in p for p in health.evaluate_host(m, T)[1])


def test_nonpython_victim_is_degraded():
    status, problems = health.evaluate_host(_oom(60, victim="chromium"), T)
    assert status == health.DEGRADED


def test_kloc_age_journalctl_iso():
    # journalctl short-iso, UTC: 120 s before `now`
    now = 1_700_000_000.0
    line = time.strftime("%Y-%m-%dT%H:%M:%S+0000",
                         time.gmtime(now - 120)) + " host kernel: Out of memory: Killed process 1 (python)"
    age = oslevel._kloc_age_s(line, now=now)
    assert age is not None and abs(age - 120) < 2


def test_kloc_age_iso_offset():
    # +0200 wall time must still resolve to the correct UTC age
    now = 1_700_000_000.0
    wall = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 120 + 2 * 3600))  # +2h wall for +0200
    age = oslevel._kloc_age_s(wall + "+0200 host kernel: oom-kill:task=python,pid=1", now=now)
    assert age is not None and abs(age - 120) < 2


def test_kloc_age_unparseable():
    assert oslevel._kloc_age_s("no timestamp here", now=1_700_000_000.0) is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok", name)
