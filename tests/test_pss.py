"""Summing RSS across a process tree over-counts; Pss is the figure that means something.

Every process in an RMS capture tree maps the same shared pages, and total_rss_mb adds
them once per process. On a six-camera host that read as ~60 GB of usage on a 31 GB box
(~1.85x over on measurement), which is enough to send someone hunting a leak that is not
there -- it briefly did exactly that during an OOM investigation on AUC0A.

Pss divides each shared page by the number of processes sharing it, so a sum over the
tree is comparable to real RAM. It is published ALONGSIDE total_rss_mb, never instead of
it: `rss` carries months of cc-trends history, and redefining it in place would make old
and new samples look comparable when they are not.

Runs under pytest, or standalone: `python tests/test_pss.py`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor import collect, health                # noqa: E402


def test_pss_of_a_real_process_is_at_most_its_rss():
    """Pss <= Rss always: shared pages are divided, private ones counted whole."""
    pid = os.getpid()
    pss = collect._proc_pss_kb(pid)
    if pss is None:
        return                          # no smaps_rollup here (kernel < 4.14 / no perm)
    rss = 0
    with open("/proc/%d/status" % pid) as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1]); break
    assert 0 < pss <= rss, (pss, rss)


def test_unreadable_process_yields_None_not_zero_and_never_raises():
    """"Cannot measure" must be distinguishable from "measured zero" -- a 0 would look
    like a station using no memory, which is a fault signal, not a gap."""
    assert collect._proc_pss_kb(999999) is None      # no such pid
    # pid 1 is another user's process here: unreadable must give None, never raise and
    # never 0, since a station reporting 0 MB would read as a dead capture tree.
    got = collect._proc_pss_kb(1)
    assert got is None or got > 0, got


def test_collect_publishes_both_fields():
    class _S(object):
        station_id = "TEST01"; config_path = "/nonexistent/.config"
        captured_path = archived_path = data_dir = "/nonexistent"
    res = collect.collect_process(_S())
    assert "total_rss_mb" in res, "the historical field must stay published"
    assert "total_pss_mb" in res, "the accurate field must be published too"


def test_duplicate_alert_quotes_pss_when_available():
    """The number an operator reads should be the one that reflects real RAM."""
    from cc_mqtt_monitor.config import Thresholds
    _s, problems = health.evaluate(
        {"capture_instances": 38, "total_rss_mb": 26600, "total_pss_mb": 9100,
         "capture_alive": True}, Thresholds(), ())
    txt = " ".join(problems)
    assert "9100 MB" in txt, txt
    assert "26600 MB" not in txt, "the inflated sum must not be what is reported"


def test_duplicate_alert_falls_back_to_rss():
    """Old kernels report no Pss; the alert must still carry a figure."""
    from cc_mqtt_monitor.config import Thresholds
    _s, problems = health.evaluate(
        {"capture_instances": 38, "total_rss_mb": 26600, "total_pss_mb": None,
         "capture_alive": True}, Thresholds(), ())
    assert "26600 MB" in " ".join(problems)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn) and not name.startswith("_"):
            fn(); print("ok", name)
