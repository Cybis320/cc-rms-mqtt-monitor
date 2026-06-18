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


def read_udp_stats():
    """Return (RcvbufErrors, InDatagrams) from /proc/net/snmp.

    These are host-wide cumulative UDP counters (one kernel counter shared by
    every UDP socket), so they belong to the host, not any single camera.
    """
    try:
        with open("/proc/net/snmp") as fh:
            lines = fh.readlines()
    except (IOError, OSError):
        return None, None
    header = None
    for line in lines:
        if not line.startswith("Udp:"):
            continue
        cols = line.split()[1:]
        if header is None:            # first "Udp:" line is the field-name row
            header = cols
            continue
        row = dict(zip(header, cols))  # second "Udp:" line is the values row
        try:
            return int(row["RcvbufErrors"]), int(row["InDatagrams"])
        except (KeyError, ValueError):
            return None, None
    return None, None


def _rmem_max():
    try:
        with open("/proc/sys/net/core/rmem_max") as fh:
            return int(fh.read().strip())
    except (IOError, OSError, ValueError):
        return None


# Previous RcvbufErrors sample, so the long-running loop can derive a growth
# rate (the raw counter only climbs / resets at boot). {"t": monotonic, "err"}.
_UDP_LAST = {}


def collect_udp_errors():
    """Host-wide UDP receive-buffer overflow stats, with a per-minute rate.

    RcvbufErrors counts datagrams the kernel dropped because a socket receive
    buffer was full -- the host-level analogue of dropped frames for UDP RTSP.
    It is cumulative and resets at boot, so the alertable signal is its growth
    RATE: the delta since the previous cycle. The first sample (and any sample
    after a counter reset) yields a null rate rather than a false spike.
    """
    err, dgrams = read_udp_stats()
    result = {
        "udp_rcvbuf_errors": err,              # cumulative since boot
        "udp_in_datagrams": dgrams,            # cumulative since boot
        "udp_rcvbuf_errors_per_min": None,     # growth rate (alert signal)
        "udp_rcvbuf_error_pct": None,          # cumulative ratio (overview)
        "udp_rmem_max": _rmem_max(),
    }
    if err is None:
        return result
    if dgrams:
        result["udp_rcvbuf_error_pct"] = round(err / dgrams * 100.0, 4)
    now = time.monotonic()
    last_err, last_t = _UDP_LAST.get("err"), _UDP_LAST.get("t")
    if last_err is not None and last_t is not None and now > last_t:
        d_err = err - last_err
        if d_err >= 0:   # negative => counter reset (reboot); skip this delta
            result["udp_rcvbuf_errors_per_min"] = round(d_err / (now - last_t) * 60.0, 1)
    _UDP_LAST["err"], _UDP_LAST["t"] = err, now
    return result


def collect_host(scan_oom_events=True, udp=False):
    """Host-wide metrics dict. `udp=True` adds UDP RcvbufErrors stats (only
    worth collecting when a station uses protocol: udp)."""
    metrics = read_meminfo()
    if scan_oom_events:
        metrics.update(scan_oom())
    metrics["uptime_s"] = _uptime()
    if udp:
        metrics.update(collect_udp_errors())
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
