"""Host-level OS signals: memory headroom and OOM-killer events.

These are host-wide (not per-station), so they are published under a separate
host health topic. The OOM scan is best-effort: if the kernel log is not
readable (e.g. dmesg_restrict and not in the adm/systemd-journal group) the
fields are simply null and a note is set, rather than failing.
"""

import os
import re
import time
import subprocess

# OOM-killer signatures, e.g.:
#   "Out of memory: Killed process 12345 (python) total-vm:..."
#   "oom-kill:constraint=...,task=python,pid=12345,..."
_OOM_KILLED_RE = re.compile(r"Out of memory: Killed process (\d+) \(([^)]+)\)")
_OOM_KILL_RE = re.compile(r"oom-kill:.*?task=([^,]+).*?pid=(\d+)", re.IGNORECASE)


def read_meminfo():
    """Return host memory headroom in MB from /proc/meminfo."""
    fields = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                fields[key.strip()] = int(rest.split()[0])  # kB
    except (IOError, OSError, ValueError, IndexError):
        return {"mem_available_mb": None, "mem_total_mb": None, "swap_free_mb": None}
    return {
        "mem_available_mb": round(fields.get("MemAvailable", 0) / 1024.0),
        "mem_total_mb": round(fields.get("MemTotal", 0) / 1024.0),
        "swap_free_mb": round(fields.get("SwapFree", 0) / 1024.0),
    }


def _kernel_log_lines(max_lines):
    """Best-effort recent kernel log lines, trying the cheapest readable source."""
    attempts = [
        ["journalctl", "-k", "-n", str(max_lines), "--no-pager", "-o", "short-iso"],
        ["dmesg", "-T"],
    ]
    for cmd in attempts:
        try:
            out = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=10, universal_newlines=True)
            if out.returncode == 0 and out.stdout:
                return out.stdout.splitlines()[-max_lines:], None
        except (OSError, subprocess.SubprocessError):
            continue
    # Fall back to log files (often readable by the adm group).
    for path in ("/var/log/kern.log", "/var/log/syslog"):
        if os.path.isfile(path):
            try:
                with open(path, errors="replace") as fh:
                    return fh.read().splitlines()[-max_lines:], None
            except (IOError, OSError):
                continue
    return [], "kernel log not readable (need adm/systemd-journal group or root)"


def scan_oom(max_lines=1500):
    """Count OOM-kill events in the recent kernel log and return the last victim."""
    result = {
        "oom_kill_count": 0,
        "last_oom_victim": None,
        "last_oom_line": None,
        "oom_note": None,
    }
    lines, note = _kernel_log_lines(max_lines)
    if note:
        result["oom_note"] = note
        return result

    for line in lines:
        m = _OOM_KILLED_RE.search(line) or _OOM_KILL_RE.search(line)
        if m:
            result["oom_kill_count"] += 1
            # group(2) is comm for "Killed process", group(1) is task for oom-kill:
            victim = m.group(2) if _OOM_KILLED_RE.search(line) else m.group(1)
            result["last_oom_victim"] = victim
            result["last_oom_line"] = line.strip()[:300]
    return result


def collect_host(scan_oom_events=True):
    """Host-wide metrics dict."""
    metrics = read_meminfo()
    if scan_oom_events:
        metrics.update(scan_oom())
    metrics["uptime_s"] = _uptime()
    return metrics


def _uptime():
    try:
        with open("/proc/uptime") as fh:
            return round(float(fh.read().split()[0]))
    except (IOError, OSError, ValueError, IndexError):
        return None


def protect_from_oom(score_adj=-900):
    """Make this process one of the last the kernel OOM-killer chooses.

    Lowering oom_score_adj below 0 needs CAP_SYS_RESOURCE, so this is
    best-effort when running unprivileged. The systemd unit sets
    OOMScoreAdjust=-900 to guarantee it; this call covers manual runs where
    the privilege happens to be available. Returns the value actually set,
    or None if it could not be changed.
    """
    try:
        with open("/proc/self/oom_score_adj", "w") as fh:
            fh.write(str(score_adj))
        return score_adj
    except (IOError, OSError):
        return None
