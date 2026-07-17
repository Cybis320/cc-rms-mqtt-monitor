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


def _oom(age_s, victim="python", count=6):
    return {"oom_kill_count": count, "last_oom_victim": victim,
            "oom_last_age_s": age_s, "mem_available_mb": 10000}


def test_recent_oom_flags():
    status, problems = health.evaluate_host(_oom(60), T)      # 1 min ago
    assert any("OOM-killer fired" in p for p in problems)
    assert status == health.ERROR                             # python victim -> error


def test_stale_oom_self_clears():
    # kill happened well past the window -> memory recovered, no new kill -> no flag
    status, problems = health.evaluate_host(_oom(T.oom_recent_s + 3600), T)
    assert not any("OOM" in p for p in problems)


def test_unparseable_age_still_flags():
    # if the kernel-log timestamp couldn't be parsed we must not go silent on a real OOM
    status, problems = health.evaluate_host(_oom(None), T)
    assert any("OOM-killer fired" in p for p in problems)


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
