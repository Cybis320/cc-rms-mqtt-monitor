"""Detect when disruption is EXPECTED, using local host knowledge.

The monitor (unlike the bridge) can see why capture is briefly down: the host
just rebooted, or RMS is mid-update. It stamps `maintenance` + `maintenance_
reason` on every published record so the bridge can suppress notifications for
*expected* churn while still alerting instantly on genuine failures.

Reasons:
  booting       -- host uptime is below boot_grace_s (capture still starting)
  rms-updating  -- a GRMSUpdater / RMS_Update process is actually running

Self-healing (so a stuck marker can't mute a host forever):
  * A lock/flag file is NEVER trusted on its own -- "updating" requires a live
    updater process. GRMSUpdater's flock file (e.g. /tmp/rms_grms_updater.lock)
    lingers after the process exits, and a kernel-update run reboots mid-way, so
    a left-behind file must not imply maintenance.
  * The updater process is time-bounded -- a hung updater older than the update
    window doesn't count.
  * A stale maintenance_file is deleted (on boot, when no updater is alive, or
    when older than the window).
"""

import os
import time

# Updater script basenames. We match the *executable being run* (an argv element
# whose basename is one of these), NOT a substring anywhere in the command line --
# otherwise an unrelated process that merely mentions the name (a grep, an editor,
# a commit message) would look like an update in progress.
_UPDATER_SCRIPTS = ("GRMSUpdater.sh", "RMS_Update.sh")

try:
    _CLK_TCK = os.sysconf("SC_CLK_TCK")
except (AttributeError, ValueError, OSError):
    _CLK_TCK = 100


def _uptime_s():
    try:
        with open("/proc/uptime") as fh:
            return float(fh.read().split()[0])
    except (IOError, OSError, ValueError, IndexError):
        return None


def _proc_age_s(pid):
    """Seconds since process <pid> started, or None if it can't be determined."""
    try:
        with open("/proc/%s/stat" % pid) as fh:
            line = fh.read()
        # Fields after the "(comm)" are space-separated; starttime is field 22.
        rest = line[line.rfind(")") + 2:].split()
        starttime_ticks = float(rest[19])
        uptime = _uptime_s()
        if uptime is None:
            return None
        return uptime - starttime_ticks / _CLK_TCK
    except (IOError, OSError, ValueError, IndexError):
        return None


def _updater_running(max_age_s):
    """True if a GRMSUpdater/RMS_Update script started within max_age_s is alive."""
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % entry, "rb") as fh:
                args = fh.read().split(b"\x00")
        except (IOError, OSError):
            continue
        for arg in args:
            if not arg:
                continue
            if os.path.basename(arg.decode("utf-8", "replace")) in _UPDATER_SCRIPTS:
                age = _proc_age_s(entry)
                if age is None or age <= max_age_s:
                    return True   # updater started within the sane window
                break
    return False


def detect(config):
    """Return (maintenance: bool, reason: str|None). Self-healing."""
    window = config.maintenance_file_max_age_s     # sane update window (s)
    uptime = _uptime_s()
    booting = uptime is not None and uptime < config.boot_grace_s
    updating = _updater_running(window)

    # Self-heal a lingering lock/flag file: it is never a maintenance signal on
    # its own, so just delete it when clearly stale -- on boot (an update that
    # rebooted is done), when no updater is alive, or when older than the window.
    mf = config.maintenance_file
    if mf:
        mf = os.path.expanduser(mf)
        try:
            if os.path.isfile(mf):
                age = time.time() - os.path.getmtime(mf)
                if booting or (not updating) or age > window:
                    os.remove(mf)
        except OSError:
            pass

    if booting:
        return True, "booting"
    if updating:
        return True, "rms-updating"
    return False, None
