"""Tell a power cut / hard reset apart from a deliberate reboot.

Nothing on the box records WHY the last boot ended: a low uptime looks the same after
a power outage as after `sudo reboot`, and a Pi's journal is volatile by default, so the
previous boot's last words are gone. The monitor is the one thing that runs the whole
time capture does, so it keeps a marker file that says "I was alive on boot <id> at
<time>", and rewrites it as it goes:

    state = live       -- heartbeat while running (refreshed every HEARTBEAT_S)
    state = shutdown   -- the monitor was stopped BY A SYSTEM SHUTDOWN (systemd was
                          stopping everything when it sent SIGTERM)
    state = stopped    -- the monitor alone was stopped (systemctl stop, auto-update
                          restart); the system carried on without it

At the next START, the marker's boot id is compared with the running kernel's:

  * different boot, marker `live`      -> the previous boot ended with the monitor still
                                          running and never shut down: a power cut, a
                                          hard reset, or a kernel panic -> "unclean"
  * different boot, marker `shutdown`  -> a normal reboot/poweroff -> "clean"
  * different boot, marker `stopped`   -> the monitor wasn't watching when the boot
                                          ended (stopped by hand, then rebooted) ->
                                          "unknown", deliberately NOT "unclean"
  * same boot                          -> a process restart (auto-update); the verdict
                                          already reached for this boot is carried over
  * no marker                          -> first run on this install -> nothing to say

The verdict is stored in the marker for the current boot, so every restart within the
boot publishes the same `last_shutdown` and the same `last_shutdown_age_s` (seconds
since the previous boot was last seen alive -- for a power cut, that is the outage
time to within HEARTBEAT_S).

Only the long-running loop (run_loop) owns the marker: start(), heartbeat(), stop().
One-shot runs (--once, --status) only READ it via metrics(), so they never clobber
the service's live state.
"""

import json
import logging
import os
import subprocess
import time

from .config import state_paths

log = logging.getLogger("cc_mqtt_monitor")

BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"
# How often the live heartbeat is rewritten. Bounds both the SD-card write rate (one
# ~150-byte file) and the precision of "when did the power go".
HEARTBEAT_S = 120

CLEAN, UNCLEAN, UNKNOWN = "clean", "unclean", "unknown"
_VERDICT_FOR_STATE = {"live": UNCLEAN, "shutdown": CLEAN, "stopped": UNKNOWN}

_last_beat = 0.0


def current_boot_id():
    """The running kernel's boot id, or None when unreadable (then the marker is inert)."""
    try:
        with open(BOOT_ID_FILE) as fh:
            return fh.read().strip() or None
    except (IOError, OSError):
        return None


def _paths():
    return state_paths("boot_marker", "CC_BOOT_MARKER")


def _read():
    """The marker record, from the first candidate path that holds one; else None."""
    for path in _paths():
        try:
            with open(path) as fh:
                rec = json.load(fh)
        except (IOError, OSError, ValueError):
            continue
        if isinstance(rec, dict) and rec.get("boot_id"):
            return rec
    return None


def _write(rec):
    """Atomically write the marker to the first writable candidate path."""
    data = json.dumps(rec, sort_keys=True)
    for path in _paths():
        try:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                fh.write(data + "\n")
            os.replace(tmp, path)
            return True
        except (IOError, OSError):
            continue
    log.warning("could not write the boot marker at any of %s", _paths())
    return False


def system_stopping():
    """True when systemd is in the middle of a shutdown/reboot (so a SIGTERM we just got
    is part of it, not a lone `systemctl stop`). Best-effort: no systemd -> False."""
    try:
        out = subprocess.run(["systemctl", "is-system-running"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return out == "stopping"


def start():
    """Reach the verdict for this boot and mark the monitor live. Call once at loop start."""
    global _last_beat
    boot_id = current_boot_id()
    if not boot_id:
        return
    prev = _read()
    now = time.time()
    rec = {"boot_id": boot_id, "state": "live", "ts": now,
           "last_shutdown": None, "last_shutdown_ts": None}
    if prev and prev.get("boot_id") == boot_id:
        # Same boot: a process restart. Keep the verdict already reached.
        rec["last_shutdown"] = prev.get("last_shutdown")
        rec["last_shutdown_ts"] = prev.get("last_shutdown_ts")
    elif prev:
        rec["last_shutdown"] = _VERDICT_FOR_STATE.get(prev.get("state"), UNKNOWN)
        rec["last_shutdown_ts"] = prev.get("ts")
        if rec["last_shutdown"] == UNCLEAN:
            log.warning("Previous boot ended WITHOUT a clean shutdown (power cut / hard "
                        "reset?); last seen alive %s ago",
                        _fmt_age(now - (prev.get("ts") or now)))
        else:
            log.info("Previous boot ended with a %s shutdown", rec["last_shutdown"])
    _write(rec)
    _last_beat = now


def heartbeat(force=False):
    """Refresh the live timestamp (rate-limited to HEARTBEAT_S). Call every cycle."""
    global _last_beat
    now = time.time()
    if not force and now - _last_beat < HEARTBEAT_S:
        return
    rec = _read()
    boot_id = current_boot_id()
    if not boot_id or not rec or rec.get("boot_id") != boot_id:
        return               # start() has not run for this boot; nothing to refresh
    rec.update(state="live", ts=now)
    if _write(rec):
        _last_beat = now


def stop():
    """Record how this process is ending: with the whole system, or on its own."""
    rec = _read()
    boot_id = current_boot_id()
    if not boot_id or not rec or rec.get("boot_id") != boot_id:
        return
    rec.update(state="shutdown" if system_stopping() else "stopped", ts=time.time())
    _write(rec)


def metrics():
    """Published fields for the host record (read-only; safe from any code path).

    last_shutdown        "clean" | "unclean" | "unknown" | None (no verdict this boot)
    last_shutdown_age_s  seconds since the previous boot was last seen alive / shut down
    """
    out = {"last_shutdown": None, "last_shutdown_age_s": None}
    rec = _read()
    boot_id = current_boot_id()
    if not rec or not boot_id or rec.get("boot_id") != boot_id:
        return out
    out["last_shutdown"] = rec.get("last_shutdown")
    ts = rec.get("last_shutdown_ts")
    if out["last_shutdown"] and isinstance(ts, (int, float)):
        out["last_shutdown_age_s"] = max(0, round(time.time() - ts))
    return out


def _fmt_age(secs):
    secs = int(secs)
    if secs < 3600:
        return "%dm" % (secs // 60)
    return "%dh %dm" % (secs // 3600, (secs % 3600) // 60)
