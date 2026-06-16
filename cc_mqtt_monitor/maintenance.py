"""Detect when disruption is EXPECTED, using local host knowledge.

The monitor (unlike the bridge) can see why capture is briefly down: the host
just rebooted, RMS is mid-update, or an operator flagged maintenance. It stamps
`maintenance` + `maintenance_reason` on every published record so the bridge can
suppress notifications for *expected* churn while still alerting instantly on
genuine, unexpected failures (no blind time-delay needed).

Reasons, in priority order:
  flagged       -- an explicit maintenance sentinel file is present and fresh
  booting       -- host uptime is below boot_grace_s (capture still starting)
  rms-updating  -- a GRMSUpdater / RMS_Update process is running
"""

import os
import time

# Process command-line markers that mean "RMS is being updated right now".
_UPDATER_MARKERS = ("GRMSUpdater", "RMS_Update")


def _uptime_s():
    try:
        with open("/proc/uptime") as fh:
            return float(fh.read().split()[0])
    except (IOError, OSError, ValueError, IndexError):
        return None


def _updater_running():
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % entry, "rb") as fh:
                cmd = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except (IOError, OSError):
            continue
        if cmd and any(m in cmd for m in _UPDATER_MARKERS):
            return True
    return False


def _flag_fresh(path, max_age_s):
    try:
        path = os.path.expanduser(path)
        return os.path.isfile(path) and (time.time() - os.path.getmtime(path)) < max_age_s
    except (IOError, OSError):
        return False


def detect(config):
    """Return (maintenance: bool, reason: str|None)."""
    if config.maintenance_file and _flag_fresh(
            config.maintenance_file, config.maintenance_file_max_age_s):
        return True, "flagged"

    uptime = _uptime_s()
    if uptime is not None and uptime < config.boot_grace_s:
        return True, "booting"

    if _updater_running():
        return True, "rms-updating"

    return False, None
