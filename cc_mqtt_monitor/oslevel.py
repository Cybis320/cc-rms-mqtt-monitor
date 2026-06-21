"""Host-level OS signals: memory pressure (PSI) + headroom and OOM-killer events.

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


def read_psi_memory():
    """Memory Pressure Stall Information from /proc/pressure/memory.

    `some avgN` = % of the last N s at least one task was stalled on memory;
    `full avgN` = % of time EVERY task was stalled (the box thrashing in reclaim)
    -- the leading pre-OOM indicator, and a stall RATIO so it needs no per-host
    scaling. Null (PSI disabled / kernel <4.20) leaves the fields None, so the
    memory-pressure check simply doesn't fire rather than erroring."""
    result = {"mem_psi_some_avg10": None, "mem_psi_full_avg10": None,
              "mem_psi_full_avg60": None}
    try:
        with open("/proc/pressure/memory") as fh:
            lines = fh.readlines()
    except (IOError, OSError):
        return result
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        kind = parts[0]   # "some" or "full"
        fields = {}
        for tok in parts[1:]:
            k, _, v = tok.partition("=")
            fields[k] = v
        try:
            if kind == "some":
                result["mem_psi_some_avg10"] = float(fields["avg10"])
            elif kind == "full":
                result["mem_psi_full_avg10"] = float(fields["avg10"])
                result["mem_psi_full_avg60"] = float(fields["avg60"])
        except (KeyError, ValueError):
            continue
    return result


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


# ---------------------------------------------------------------------------
# CPU / I-O pressure (the "back-pressure" class of dropped frames)
# ---------------------------------------------------------------------------

# Previous /proc/stat aggregate-cpu jiffies, so we can derive busy/iowait/steal
# PERCENTAGES over the interval (the raw fields are cumulative since boot).
_CPU_LAST = {}


def read_loadavg():
    """1-minute load average and load-per-core (load normalised by CPU count).

    load-per-core is the portable saturation signal: ~1.0 means the run queue
    matches the cores; well above 1.0 means tasks are waiting for CPU, a cause
    of capture back-pressure that is identical on x86 and a 4-core Pi."""
    ncpu = os.cpu_count() or 1
    try:
        with open("/proc/loadavg") as fh:
            load1 = float(fh.read().split()[0])
    except (IOError, OSError, ValueError, IndexError):
        return {"load1": None, "load_per_core": None, "ncpu": ncpu}
    return {"load1": round(load1, 2),
            "load_per_core": round(load1 / ncpu, 2),
            "ncpu": ncpu}


def collect_cpu_pressure():
    """Busy / iowait / steal percentages over the interval from /proc/stat.

    iowait% is the direct read on disk-I/O back-pressure (the capture consumer
    blocked writing FF/frames), and busy% on CPU saturation. Both are deltas, so
    the first cycle (no previous sample) yields nulls rather than a boot-to-now
    average. Pi-friendly: a single short read of /proc/stat."""
    result = {"cpu_busy_pct": None, "cpu_iowait_pct": None, "cpu_steal_pct": None}
    result.update(read_loadavg())
    try:
        with open("/proc/stat") as fh:
            parts = fh.readline().split()  # "cpu  user nice system idle iowait irq softirq steal ..."
        if parts and parts[0] == "cpu":
            vals = [int(x) for x in parts[1:]]
        else:
            return result
    except (IOError, OSError, ValueError):
        return result
    # Index by the canonical /proc/stat order; missing trailing fields => 0.
    idle = vals[3] if len(vals) > 3 else 0
    iowait = vals[4] if len(vals) > 4 else 0
    steal = vals[7] if len(vals) > 7 else 0
    total = sum(vals)
    last = _CPU_LAST
    if last.get("total") is not None and total > last["total"]:
        d_total = total - last["total"]
        d_idle = (idle + iowait) - (last["idle"] + last["iowait"])
        result["cpu_busy_pct"] = round((1.0 - d_idle / d_total) * 100.0, 1)
        result["cpu_iowait_pct"] = round((iowait - last["iowait"]) / d_total * 100.0, 1)
        result["cpu_steal_pct"] = round((steal - last["steal"]) / d_total * 100.0, 1)
    _CPU_LAST.update(total=total, idle=idle, iowait=iowait, steal=steal)
    return result


# ---------------------------------------------------------------------------
# NIC errors and IP reassembly (the "loss on the wire / before the socket" class)
# ---------------------------------------------------------------------------

_NIC_LAST = {}   # previous summed NIC error counters + monotonic time


def read_nic_stats():
    """Summed genuine NIC error counters across real interfaces (/proc/net/dev).

    Counts only HARDWARE/link errors -- RX errs+fifo+frame, TX errs+carrier --
    which point at the physical link (bad cable, duplex mismatch, dying port,
    NIC overrun). It deliberately EXCLUDES rx_dropped / tx_dropped: those are
    dominated by benign unwanted multicast/broadcast the host discards (mDNS,
    SSDP/ONVIF discovery, IGMP) and by qdisc drops, which climb steadily on a
    healthy host and would false-alarm. rx_dropped is returned separately as
    information (not alerted). 'lo' and virtual interfaces are skipped. Returns
    (rx_err, rx_dropped, tx_err), or (None, None, None) if unreadable."""
    try:
        with open("/proc/net/dev") as fh:
            lines = fh.readlines()[2:]   # skip the two header rows
    except (IOError, OSError):
        return None, None, None
    rx_err = rx_drop = tx_err = 0
    for line in lines:
        name, _, rest = line.partition(":")
        name = name.strip()
        if not rest or name == "lo" or name.startswith(("veth", "docker", "br-")):
            continue
        f = rest.split()
        if len(f) < 12:
            continue
        try:
            # RX: bytes packets errs drop fifo frame ... | TX: bytes packets errs
            # drop fifo colls carrier compressed
            rx_err += int(f[2]) + int(f[4]) + int(f[5])   # errs + fifo + frame
            rx_drop += int(f[3])                          # dropped (benign here)
            tx_err += int(f[10]) + (int(f[14]) if len(f) > 14 else 0)  # errs + carrier
        except (ValueError, IndexError):
            continue
    return rx_err, rx_drop, tx_err


def collect_nic_errors():
    """Host NIC RX/TX error totals plus an RX-error growth RATE (per min).

    Like the UDP counter, the alertable signal is the rate: a climbing RX error
    count during dropped frames implicates the wire/NIC, whereas a flat count
    (with drops still happening) clears the NIC and points downstream."""
    rx_err, rx_drop, tx_err = read_nic_stats()
    result = {"nic_rx_errors": rx_err, "nic_tx_errors": tx_err,
              "nic_rx_dropped": rx_drop,      # info only (benign multicast) -- not alerted
              "nic_rx_errors_per_min": None}
    if rx_err is None:
        return result
    now = time.monotonic()
    last_err, last_t = _NIC_LAST.get("rx"), _NIC_LAST.get("t")
    if last_err is not None and last_t is not None and now > last_t:
        d = rx_err - last_err
        if d >= 0:   # negative => counters reset (NIC reset / reboot)
            result["nic_rx_errors_per_min"] = round(d / (now - last_t) * 60.0, 1)
    _NIC_LAST["rx"], _NIC_LAST["t"] = rx_err, now
    return result


_IP_REASM_LAST = {}


def read_ip_reasm_fails():
    """Ip.ReasmFails from /proc/net/snmp (fragment-reassembly drops).

    Large UDP datagrams (some cameras emit them) that get IP-fragmented and lose
    a fragment are dropped here -- counted in NEITHER RcvbufErrors nor NIC errs,
    so without this a fragmentation problem looks like an unexplained drop."""
    try:
        with open("/proc/net/snmp") as fh:
            lines = fh.readlines()
    except (IOError, OSError):
        return None
    header = None
    for line in lines:
        if not line.startswith("Ip:"):
            continue
        cols = line.split()[1:]
        if header is None:
            header = cols
            continue
        try:
            return int(dict(zip(header, cols))["ReasmFails"])
        except (KeyError, ValueError):
            return None
    return None


def collect_ip_reasm():
    """IP reassembly-failure total and growth rate (per min)."""
    fails = read_ip_reasm_fails()
    result = {"ip_reasm_fails": fails, "ip_reasm_fails_per_min": None}
    if fails is None:
        return result
    now = time.monotonic()
    last, last_t = _IP_REASM_LAST.get("n"), _IP_REASM_LAST.get("t")
    if last is not None and last_t is not None and now > last_t:
        d = fails - last
        if d >= 0:
            result["ip_reasm_fails_per_min"] = round(d / (now - last_t) * 60.0, 1)
    _IP_REASM_LAST["n"], _IP_REASM_LAST["t"] = fails, now
    return result


def collect_host(scan_oom_events=True, udp=False):
    """Host-wide metrics dict. `udp=True` adds UDP RcvbufErrors + IP reassembly
    stats (only worth collecting when a station uses protocol: udp). CPU/I-O
    pressure and NIC errors are always collected -- they're cheap and apply to
    every dropped-frame attribution regardless of transport."""
    metrics = read_meminfo()
    metrics.update(read_psi_memory())
    if scan_oom_events:
        metrics.update(scan_oom())
    metrics["uptime_s"] = _uptime()
    metrics.update(collect_cpu_pressure())
    metrics.update(collect_nic_errors())
    if udp:
        metrics.update(collect_udp_errors())
        metrics.update(collect_ip_reasm())
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
