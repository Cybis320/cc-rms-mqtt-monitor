"""A power cut / hard reset must be visible after the box comes back.

Nothing on a Pi records why the last boot ended (the journal is volatile, uptime looks
the same after an outage as after `sudo reboot`). The monitor keeps a boot marker --
`live` heartbeat while running, `shutdown` when systemd stops it during a reboot/
poweroff, `stopped` when only the monitor was stopped -- and at the next start judges
the previous boot by it: a `live` marker from a different boot id means the box died
without a shutdown. That verdict is published as `last_shutdown` and health raises a
degraded, self-ageing `unclean_shutdown` advisory.

Runs under pytest, or standalone: `python tests/test_unclean_shutdown.py`.
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor import bootmarker, health              # noqa: E402
from cc_mqtt_monitor.config import Thresholds               # noqa: E402

T = Thresholds()
HOUR = 3600


# --- health -----------------------------------------------------------------------

def test_recent_unclean_shutdown_is_degraded():
    status, problems = health.evaluate_host(
        {"last_shutdown": "unclean", "last_shutdown_age_s": 2 * HOUR}, T)
    assert status == health.DEGRADED
    assert any("not shut down cleanly" in p and "power cut" in p for p in problems)


def test_unclean_shutdown_ages_out_but_field_stays():
    m = {"last_shutdown": "unclean", "last_shutdown_age_s": T.unclean_shutdown_recent_s + 1}
    status, problems = health.evaluate_host(m, T)
    assert status == health.OK and not problems
    assert m["last_shutdown"] == "unclean"        # the record still says so


def test_clean_unknown_and_absent_are_silent():
    for val in ("clean", "unknown", None):
        status, problems = health.evaluate_host(
            {"last_shutdown": val, "last_shutdown_age_s": 60}, T)
        assert status == health.OK and not problems, val
    assert health.evaluate_host({}, T) == (health.OK, [])


def test_check_can_be_disabled():
    m = {"last_shutdown": "unclean", "last_shutdown_age_s": 60}
    assert health.evaluate_host(m, T, disabled=("unclean_shutdown",)) == (health.OK, [])


# --- boot marker ------------------------------------------------------------------

class _Marker:
    """Run the marker against a temp file with a fake boot id and fake systemd state."""

    def __init__(self, boot_id="boot-B", stopping=False):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "boot_marker")
        self.boot_id, self.stopping = boot_id, stopping

    def __enter__(self):
        self._env = os.environ.get("CC_BOOT_MARKER")
        os.environ["CC_BOOT_MARKER"] = self.path
        self._saved = (bootmarker.current_boot_id, bootmarker.system_stopping,
                       bootmarker._last_beat)
        bootmarker.current_boot_id = lambda: self.boot_id
        bootmarker.system_stopping = lambda: self.stopping
        bootmarker._last_beat = 0.0
        return self

    def __exit__(self, *exc):
        bootmarker.current_boot_id, bootmarker.system_stopping, bootmarker._last_beat = self._saved
        if self._env is None:
            os.environ.pop("CC_BOOT_MARKER", None)
        else:
            os.environ["CC_BOOT_MARKER"] = self._env

    def seed(self, **rec):
        with open(self.path, "w") as fh:
            json.dump(rec, fh)

    def read(self):
        with open(self.path) as fh:
            return json.load(fh)


def test_first_run_has_no_verdict_and_goes_live():
    with _Marker() as mk:
        bootmarker.start()
        assert bootmarker.metrics() == {"last_shutdown": None, "last_shutdown_age_s": None}
        rec = mk.read()
        assert rec["boot_id"] == "boot-B" and rec["state"] == "live"


def test_live_marker_from_another_boot_means_unclean():
    with _Marker() as mk:
        mk.seed(boot_id="boot-A", state="live", ts=time.time() - 3 * HOUR)
        bootmarker.start()
        m = bootmarker.metrics()
        assert m["last_shutdown"] == "unclean"
        assert abs(m["last_shutdown_age_s"] - 3 * HOUR) < 5   # outage time, from the heartbeat
        assert mk.read()["state"] == "live"                   # and we are now the live one


def test_shutdown_marker_means_clean_and_stopped_means_unknown():
    for state, verdict in (("shutdown", "clean"), ("stopped", "unknown")):
        with _Marker() as mk:
            mk.seed(boot_id="boot-A", state=state, ts=time.time() - 600)
            bootmarker.start()
            assert bootmarker.metrics()["last_shutdown"] == verdict, state


def test_process_restart_within_a_boot_keeps_the_verdict():
    """auto-update restarts the service several times a boot; each restart must keep
    reporting the SAME verdict and the SAME outage time, not re-judge itself."""
    with _Marker() as mk:
        mk.seed(boot_id="boot-A", state="live", ts=time.time() - 3 * HOUR)
        bootmarker.start()
        bootmarker.stop()                       # e.g. autoupdate.sh restarting us
        assert mk.read()["state"] == "stopped"
        bootmarker.start()                      # same boot id
        m = bootmarker.metrics()
        assert m["last_shutdown"] == "unclean"
        assert abs(m["last_shutdown_age_s"] - 3 * HOUR) < 5


def test_system_shutdown_stamps_shutdown():
    with _Marker(stopping=True) as mk:
        bootmarker.start()
        bootmarker.stop()
        assert mk.read()["state"] == "shutdown"


def test_heartbeat_is_rate_limited_and_refreshes_ts():
    with _Marker() as mk:
        bootmarker.start()
        first = mk.read()["ts"]
        bootmarker.heartbeat()                  # too soon: no write
        assert mk.read()["ts"] == first
        bootmarker._last_beat = 0.0             # pretend HEARTBEAT_S elapsed
        time.sleep(0.01)
        bootmarker.heartbeat()
        assert mk.read()["ts"] > first


def test_readers_never_write_and_ignore_a_foreign_boot():
    """--once / --status only READ the marker: no start() means no clobbering, and a
    marker from another boot is not a verdict for this one."""
    with _Marker() as mk:
        mk.seed(boot_id="boot-A", state="live", ts=time.time() - 60)
        assert bootmarker.metrics()["last_shutdown"] is None
        bootmarker.heartbeat(force=True)
        bootmarker.stop()
        assert mk.read()["boot_id"] == "boot-A" and mk.read()["state"] == "live"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok  %s" % name)
