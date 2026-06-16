"""Per-station health collectors.

Every collector takes a Station and returns a plain dict of metrics. They are
intentionally defensive: a station whose data directory does not yet exist, or
whose process is not running, must never raise -- it just yields empty/false
metrics. The combined dict is consumed by health.py to compute a status.
"""

import os
import re
import glob
import json
import shutil
import time

from .solar import solar_elevation_deg

# ---------------------------------------------------------------------------
# Process detection (via /proc; no external pgrep dependency)
# ---------------------------------------------------------------------------

_STARTCAPTURE_MARKER = "RMS.StartCapture"


def _iter_proc():
    """Yield (pid, cmdline_str, ppid, vmrss_kb) for every readable process."""
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as fh:
                cmdline = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
            if not cmdline:
                continue
            ppid = 0
            vmrss_kb = 0
            with open("/proc/%d/status" % pid) as fh:
                for line in fh:
                    if line.startswith("PPid:"):
                        ppid = int(line.split()[1])
                    elif line.startswith("VmRSS:"):
                        vmrss_kb = int(line.split()[1])
        except (IOError, OSError, ValueError):
            continue
        yield pid, cmdline, ppid, vmrss_kb


def collect_process(station):
    """Detect whether the station's capture process tree is alive.

    A station is launched as ``python -u -m RMS.StartCapture -c <config>``; the
    multiprocessing children inherit the same command line, so we match on the
    config path. The "main" process is the matching PID whose parent is not
    itself a matching PID.
    """
    matches = []  # (pid, ppid, vmrss_kb)
    for pid, cmdline, ppid, vmrss_kb in _iter_proc():
        if _STARTCAPTURE_MARKER in cmdline and station.config_path in cmdline:
            matches.append((pid, ppid, vmrss_kb))

    match_pids = {pid for pid, _, _ in matches}
    main_pid = None
    for pid, ppid, _ in matches:
        if ppid not in match_pids:
            main_pid = pid
            break

    total_rss_mb = round(sum(rss for _, _, rss in matches) / 1024.0, 1)

    return {
        "capture_alive": bool(matches),
        "process_count": len(matches),
        "main_pid": main_pid,
        "total_rss_mb": total_rss_mb,
    }


# ---------------------------------------------------------------------------
# Capture freshness
# ---------------------------------------------------------------------------

_FITS_GLOB = "FF_*.fits"


def _latest_subdir(path):
    """Return the most-recently-modified immediate subdirectory of path, or None."""
    try:
        subdirs = [
            os.path.join(path, name)
            for name in os.listdir(path)
            if os.path.isdir(os.path.join(path, name))
        ]
    except (IOError, OSError):
        return None
    if not subdirs:
        return None
    return max(subdirs, key=lambda d: _safe_mtime(d))


def _safe_mtime(path):
    try:
        return os.path.getmtime(path)
    except (IOError, OSError):
        return 0.0


def collect_capture(station, now=None):
    """Freshness of the current capture session."""
    now = now or time.time()
    latest = _latest_subdir(station.captured_path)
    result = {
        "captured_dir": os.path.basename(latest) if latest else None,
        "fits_count": 0,
        "newest_fits_age_s": None,
        "capture_session_age_s": None,
    }
    if not latest:
        return result

    result["capture_session_age_s"] = round(now - _safe_mtime(latest), 1)

    fits = glob.glob(os.path.join(latest, _FITS_GLOB))
    result["fits_count"] = len(fits)
    if fits:
        newest = max(_safe_mtime(f) for f in fits)
        result["newest_fits_age_s"] = round(now - newest, 1)
    return result


# ---------------------------------------------------------------------------
# Frame images (daytime / continuous output, written to FramesFiles)
# ---------------------------------------------------------------------------


def collect_frames(station, now=None):
    """Freshness of saved frame images.

    Layout: FramesFiles/YYYY/YYYYMMDD-jjj/YYYYMMDD-jjj_HH/<id>_<time>_<ms>_<d|n>.ext
    We walk the newest year -> date -> hour dir (cheap, no full recursion) and
    read the newest image. The trailing _d / _n encodes the camera mode RMS
    believed it was in when the frame was written.
    """
    now = now or time.time()
    result = {"newest_frame_age_s": None, "frame_mode": None}
    if not station.save_frames:
        return result

    hour_dir = station.frames_path
    for _ in range(3):  # year -> date -> hour
        nxt = _latest_subdir(hour_dir)
        if not nxt:
            return result
        hour_dir = nxt

    images = [
        os.path.join(hour_dir, name)
        for name in os.listdir(hour_dir)
        if name.lower().endswith((".jpg", ".png"))
    ]
    if not images:
        return result

    newest = max(images, key=_safe_mtime)
    result["newest_frame_age_s"] = round(now - _safe_mtime(newest), 1)
    base = os.path.splitext(os.path.basename(newest))[0]
    if base.endswith("_d"):
        result["frame_mode"] = "day"
    elif base.endswith("_n"):
        result["frame_mode"] = "night"
    return result


# ---------------------------------------------------------------------------
# Detection output (silent-failure class: alive + capturing but no output)
# ---------------------------------------------------------------------------


def collect_detection(station, now=None):
    """Whether detection output is being produced in the current capture dir."""
    now = now or time.time()
    latest = _latest_subdir(station.captured_path)
    result = {
        "ftpdetect_present": False,
        "calstars_present": False,
        "detection_output_age_s": None,
    }
    if not latest:
        return result

    ftp = glob.glob(os.path.join(latest, "FTPdetectinfo_*.txt"))
    cal = glob.glob(os.path.join(latest, "CALSTARS_*.txt"))
    result["ftpdetect_present"] = bool(ftp)
    result["calstars_present"] = bool(cal)

    ages = [now - _safe_mtime(f) for f in (ftp + cal)]
    if ages:
        result["detection_output_age_s"] = round(min(ages), 1)
    return result


# ---------------------------------------------------------------------------
# Log scanning: tracebacks and fatal patterns (the ".so missing" class)
# ---------------------------------------------------------------------------

# Patterns that indicate a fatal/structural failure independent of any specific
# error message -- this is the general solution for "a stage silently died".
_FATAL_PATTERNS = [
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"\bModuleNotFoundError\b"),
    re.compile(r"\bImportError\b"),
    re.compile(r"cannot open shared object file"),
    re.compile(r"undefined symbol"),
    re.compile(r"Segmentation fault|core dumped"),
    re.compile(r"\bMemoryError\b|Cannot allocate memory"),
    re.compile(r"No module named"),
]

_WATCHDOG_RE = re.compile(r"WATCHDOG:.*(died|stale|Restarting)", re.IGNORECASE)
# "Buffer fill: 12.3%, Dropped frames: 4 (last 10 min), 9 this session"
_BUFFER_RE = re.compile(
    r"Buffer fill:\s*([\d.]+)%.*Dropped frames:\s*(\d+).*?(\d+)\s+this session",
    re.IGNORECASE,
)


def _newest_log(station):
    pattern = os.path.join(station.log_path, "*log_%s_*.log" % station.station_id)
    logs = glob.glob(pattern) or glob.glob(os.path.join(station.log_path, "*.log"))
    if not logs:
        return None
    return max(logs, key=_safe_mtime)


def _tail(path, max_lines):
    """Return the last max_lines lines of a file as a list (memory-bounded)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = 65536
            data = b""
            while size > 0 and data.count(b"\n") <= max_lines:
                step = min(block, size)
                size -= step
                fh.seek(size)
                data = fh.read(step) + data
        return data.decode("utf-8", "replace").splitlines()[-max_lines:]
    except (IOError, OSError):
        return []


def _extract_traceback(lines, idx):
    """Given the index of a 'Traceback' line, return the final exception line."""
    # The last non-blank line of a traceback block is the exception summary.
    block = []
    for line in lines[idx:]:
        if line.strip() == "" and block:
            break
        block.append(line)
    for line in reversed(block):
        if line.strip():
            return line.strip()
    return lines[idx].strip()


def collect_logs(station, max_lines):
    """Scan the newest log for fatal errors, watchdog events, and buffer stats."""
    result = {
        "log_file": None,
        "log_age_s": None,
        "fatal_error_count": 0,
        "last_error": None,
        "last_watchdog_event": None,
        "buffer_fill_pct": None,
        "dropped_frames_10min": None,
        "dropped_frames_session": None,
    }
    log_path = _newest_log(station)
    if not log_path:
        return result

    result["log_file"] = os.path.basename(log_path)
    result["log_age_s"] = round(time.time() - _safe_mtime(log_path), 1)

    lines = _tail(log_path, max_lines)
    for idx, line in enumerate(lines):
        for pattern in _FATAL_PATTERNS:
            if pattern.search(line):
                result["fatal_error_count"] += 1
                if "Traceback" in line:
                    result["last_error"] = _extract_traceback(lines, idx)
                else:
                    result["last_error"] = line.strip()[:300]
                break

        if _WATCHDOG_RE.search(line):
            result["last_watchdog_event"] = line.strip()[:300]

        buf = _BUFFER_RE.search(line)
        if buf:
            result["buffer_fill_pct"] = float(buf.group(1))
            result["dropped_frames_10min"] = int(buf.group(2))
            result["dropped_frames_session"] = int(buf.group(3))

    return result


# ---------------------------------------------------------------------------
# Observation summary (rich end-of-night snapshot)
# ---------------------------------------------------------------------------

# Fields worth surfacing from the per-night observation_summary.json.
_SUMMARY_FIELDS = [
    "start_time",
    "total_fits",
    "total_expected_fits",
    "fits_file_shortfall",
    "dropped_frame_rate",
    "detections_after_ml",
    "clock_synchronized",
    "clock_error_uncertainty_ms",
    "jitter_quality",
    "photometry_good",
    "storage_free_gb",
    "commit_hash",
    "repository_lag_remote_days",
]


def collect_summary(station):
    """Parse the most recent observation_summary.json under ArchivedFiles."""
    pattern = os.path.join(station.archived_path, "*", "*_observation_summary.json")
    files = glob.glob(pattern)
    result = {"summary_age_s": None, "summary": None}
    if not files:
        return result

    newest = max(files, key=_safe_mtime)
    result["summary_age_s"] = round(time.time() - _safe_mtime(newest), 1)
    try:
        with open(newest) as fh:
            data = json.load(fh)
    except (IOError, OSError, ValueError):
        return result

    result["summary"] = {k: data.get(k) for k in _SUMMARY_FIELDS if k in data}
    return result


# ---------------------------------------------------------------------------
# Upload backlog and disk
# ---------------------------------------------------------------------------


def collect_upload(station, now=None):
    now = now or time.time()
    path = station.upload_queue_path
    result = {"upload_queue_len": 0, "upload_queue_age_s": None}
    if not os.path.isfile(path):
        return result
    try:
        with open(path) as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        result["upload_queue_len"] = len(lines)
        result["upload_queue_age_s"] = round(now - _safe_mtime(path), 1)
    except (IOError, OSError):
        pass
    return result


def collect_disk(station):
    target = station.data_dir if os.path.isdir(station.data_dir) else "/"
    try:
        usage = shutil.disk_usage(target)
        return {
            "disk_free_gb": round(usage.free / 1e9, 1),
            "disk_total_gb": round(usage.total / 1e9, 1),
        }
    except (IOError, OSError):
        return {"disk_free_gb": None, "disk_total_gb": None}


def collect_mode(station, now=None):
    """Capture-mode context. solar_elevation_deg is informational only (the
    health check observes actual output rather than predicting day/night)."""
    now = now or time.time()
    result = {
        "continuous_capture": station.continuous_capture,
        "save_frames": station.save_frames,
        "solar_elevation_deg": None,
    }
    if station.has_location:
        result["solar_elevation_deg"] = round(
            solar_elevation_deg(station.latitude, station.longitude, now), 2)
    return result


def collect_station(station, max_log_lines, now=None):
    """Run every collector and merge into one flat metrics dict."""
    now = now or time.time()
    metrics = {"station_id": station.station_id}
    metrics.update(collect_process(station))
    metrics.update(collect_capture(station, now))
    metrics.update(collect_frames(station, now))
    metrics.update(collect_detection(station, now))
    metrics.update(collect_logs(station, max_log_lines))
    metrics.update(collect_summary(station))
    metrics.update(collect_upload(station, now))
    metrics.update(collect_disk(station))
    metrics.update(collect_mode(station, now))
    return metrics
