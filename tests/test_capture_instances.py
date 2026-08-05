"""capture_instances must count real StartCapture instances, not their workers.

Built from a real tree on a live host (usv001), which false-alarmed "5 StartCapture
instances (822 MB total)" while genuinely running ONE:

    4986  ppid 1139   bash .../RMS_StartCapture.sh     <- launcher (no "RMS.StartCapture")
    5167  ppid 4986   python -m RMS.StartCapture       <- the one real instance
    5340  ppid 5167   python -m RMS.StartCapture   ┐
    5409  ppid 5167   python -m RMS.StartCapture   │ workers: classic fork, so they
    5417  ppid 5167   python -m RMS.StartCapture   │ INHERIT the parent's cmdline
    5451  ppid 5167   python -m RMS.StartCapture   │
    5592  ppid 5167   python -m RMS.StartCapture   ┘
   30445  ppid 5592   python -m RMS.StartCapture       <- grandchild

The bug: _process_config_path() reads /proc/<pid>/cwd and returns None if that read
fails. When it failed on the MAIN process, main dropped out of cmd_pids, and every child
-- whose "is my parent an instance?" test consulted cmd_pids -- was promoted to a root.
Five workers became "five instances"; their summed RSS was the 822 MB in the alert.

Runs under pytest, or standalone: `python tests/test_capture_instances.py`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor import collect                       # noqa: E402

TARGET = "/home/pi/source/RMS/.config"
SC = ["python", "-m", "RMS.StartCapture"]

# (pid, args, ppid, rss_kb) -- the real tree above
TREE = [
    (4986, ["bash", "/home/pi/Desktop/RMS_StartCapture.sh"], 1139, 1572),
    (5167, SC, 4986, 687744),
    (5340, SC, 5167, 34068),
    (5409, SC, 5167, 36120),
    (5417, SC, 5167, 43504),
    (5451, SC, 5167, 44624),
    (5592, SC, 5167, 711140),
    (30445, SC, 5592, 111156),
]


class _Station(object):
    station_id = "USV001"
    config_path = TARGET
    captured_path = "/nonexistent/CapturedFiles"
    archived_path = "/nonexistent/ArchivedFiles"
    data_dir = "/nonexistent"


def _run(procs, cfg_for):
    """collect_process with /proc faked out. cfg_for(pid) -> config path or None."""
    real_iter, real_cfg, real_age = collect._iter_proc, collect._process_config_path, collect._proc_age_s
    real_jif = collect._proc_cpu_jiffies
    collect._iter_proc = lambda: iter(procs)
    collect._process_config_path = lambda pid, args: cfg_for(pid)
    collect._proc_age_s = lambda pid: 1000.0
    collect._proc_cpu_jiffies = lambda pid: 0
    try:
        return collect.collect_process(_Station())
    finally:
        collect._iter_proc, collect._process_config_path = real_iter, real_cfg
        collect._proc_age_s, collect._proc_cpu_jiffies = real_age, real_jif


def _all_resolve(pid):
    return TARGET if pid != 4986 else None          # the bash launcher isn't a StartCapture


def _main_unreadable(pid):
    """The bug's trigger: /proc/<main>/cwd unreadable, so main resolves to None."""
    if pid in (4986, 5167):
        return None
    return TARGET


def test_one_instance_when_everything_resolves():
    r = _run(TREE, _all_resolve)
    assert r["capture_instances"] == 1, r
    assert r["main_pid"] == 5167


def test_workers_are_not_counted_when_main_is_unreadable():
    """The regression: main dropping out of cmd_pids must NOT promote its workers."""
    r = _run(TREE, _main_unreadable)
    assert r["capture_instances"] <= 1, "workers were counted as instances: %r" % r


def test_a_genuine_second_instance_is_still_counted():
    """A real duplicate is started by the launcher (or a supervisor), so its parent does
    NOT carry the RMS.StartCapture marker -- it must still register."""
    procs = list(TREE) + [
        (9001, SC, 4986, 690000),            # second instance from the same launcher
        (9002, SC, 9001, 40000),             # ...with a worker of its own
    ]
    r = _run(procs, lambda pid: None if pid == 4986 else TARGET)
    assert r["capture_instances"] == 2, r


def test_memory_sums_across_all_instances():
    """The measurement bug that hid the failure: RSS must cover every instance."""
    procs = list(TREE) + [(9001, SC, 4986, 690000)]
    r = _run(procs, lambda pid: None if pid == 4986 else TARGET)
    total_kb = sum(p[3] for p in procs if p[0] != 4986)
    assert abs(r["total_rss_mb"] - total_kb / 1024.0) < 1.0, r["total_rss_mb"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok", name)
