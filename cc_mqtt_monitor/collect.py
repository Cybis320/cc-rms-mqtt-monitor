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
import subprocess
import tempfile
import time
import calendar

from .solar import solar_elevation_deg
from .sanitize import redact
from . import rmsmode

# ---------------------------------------------------------------------------
# Process detection (via /proc; no external pgrep dependency)
# ---------------------------------------------------------------------------

_STARTCAPTURE_MARKER = "RMS.StartCapture"


def _iter_proc():
    """Yield (pid, args, ppid, vmrss_kb) for every readable process; args is the
    argv list."""
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as fh:
                args = [a.decode("utf-8", "replace")
                        for a in fh.read().split(b"\x00") if a]
            if not args:
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
        yield pid, args, ppid, vmrss_kb


_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
# Per-station previous CPU sample {station_id: (jiffies, monotonic_t)}, so the
# capture tree's CPU% can be derived from the cumulative utime+stime counters.
_PROC_CPU_LAST = {}


def _proc_cpu_jiffies(pid):
    """utime+stime (clock ticks) for a pid, or 0 if unreadable. The comm field
    can contain spaces/parens, so fields are parsed AFTER the final ')'."""
    try:
        with open("/proc/%d/stat" % pid) as fh:
            data = fh.read()
    except (IOError, OSError):
        return 0
    rpar = data.rfind(")")
    if rpar < 0:
        return 0
    fields = data[rpar + 2:].split()
    try:
        return int(fields[11]) + int(fields[12])   # utime, stime (post-comm index)
    except (ValueError, IndexError):
        return 0


def _process_config_path(pid, args):
    """The .config a StartCapture process uses: the ``-c/--config`` argument when
    given (multicam), else the default ``.config`` in the process's working
    directory (single-cam runs StartCapture with no ``-c`` from the RMS dir)."""
    cfg = None
    for i, a in enumerate(args):
        if a in ("-c", "--config") and i + 1 < len(args):
            cfg = args[i + 1]
            break
        if a.startswith("--config="):
            cfg = a.split("=", 1)[1]
            break
        if a.startswith("-c="):
            cfg = a.split("=", 1)[1]
            break
    if cfg and os.path.isabs(cfg):
        return os.path.realpath(cfg)
    # No -c, or a relative -c: resolve against the process's working directory.
    try:
        cwd = os.readlink("/proc/%d/cwd" % pid)
    except OSError:
        return None
    return os.path.realpath(os.path.join(cwd, cfg or ".config"))


def collect_process(station):
    """Detect whether the station's capture process tree is alive.

    A StartCapture process is matched to its station by the .config it actually
    uses -- the ``-c`` argument (multicam) or its working-directory default
    ``.config`` (single-cam, launched with no ``-c``). Paths are canonicalized
    with realpath, so a symlinked station .config still matches the process even
    when RMS was launched via the symlink's target (or vice versa). The "main"
    process is the matching PID whose parent is not itself a matching PID.
    """
    target = os.path.realpath(station.config_path)
    matches = []  # (pid, ppid, vmrss_kb)
    for pid, args, ppid, vmrss_kb in _iter_proc():
        if not any(_STARTCAPTURE_MARKER in a for a in args):
            continue
        if _process_config_path(pid, args) == target:
            matches.append((pid, ppid, vmrss_kb))

    match_pids = {pid for pid, _, _ in matches}
    main_pid = None
    for pid, ppid, _ in matches:
        if ppid not in match_pids:
            main_pid = pid
            break

    total_rss_mb = round(sum(rss for _, _, rss in matches) / 1024.0, 1)

    # CPU% of the whole capture tree over the interval (delta of cumulative
    # utime+stime / wall time). A saturated capture process is the on-station
    # signature of CPU back-pressure dropping frames. First sample => null.
    cpu_pct = None
    jiffies = sum(_proc_cpu_jiffies(pid) for pid in match_pids)
    now = time.monotonic()
    last = _PROC_CPU_LAST.get(station.station_id)
    if matches and last is not None and now > last[1] and jiffies >= last[0]:
        secs = (jiffies - last[0]) / float(_CLK_TCK)
        cpu_pct = round(secs / (now - last[1]) * 100.0, 1)
    if matches:
        _PROC_CPU_LAST[station.station_id] = (jiffies, now)
    else:
        _PROC_CPU_LAST.pop(station.station_id, None)

    return {
        "capture_alive": bool(matches),
        "process_count": len(matches),
        "main_pid": main_pid,
        "total_rss_mb": total_rss_mb,
        "capture_cpu_pct": cpu_pct,
    }


# ---------------------------------------------------------------------------
# Capture freshness
# ---------------------------------------------------------------------------

_FITS_GLOB = "FF_*.fits"


def _latest_subdir(path):
    """Return the highest-NAMED immediate subdirectory of path, or None.

    RMS names capture/frame directories with zero-padded, sortable timestamps
    (e.g. US005A_20260618_053104_..., 20260618-169, 20260618-169_05), so the
    lexicographically-greatest name is the most recent. We deliberately do NOT
    use mtime: a directory's mtime changes only when an entry is added/removed
    (not when frames are written into an existing subdir) and is perturbed by
    timelapse/archiving touching older directories -- both made mtime pick a
    stale directory and produce false "capture stalled" alerts (notably in the
    morning at the UTC date rollover)."""
    try:
        names = [name for name in os.listdir(path)
                 if os.path.isdir(os.path.join(path, name))]
    except (IOError, OSError):
        return None
    return os.path.join(path, max(names)) if names else None


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
        newest = max(fits)   # latest by name (FF filename carries a sortable timestamp)
        result["newest_fits_age_s"] = round(now - _safe_mtime(newest), 1)
    return result


# ---------------------------------------------------------------------------
# Platepar (camera pointing)
# ---------------------------------------------------------------------------


def collect_platepar(station):
    """Camera pointing (centre of field) from the station's platepar, rounded to
    whole degrees. Fields omitted if the platepar is missing/unreadable."""
    result = {}
    try:
        with open(station.platepar_path) as fh:
            pp = json.load(fh)
    except (IOError, OSError, ValueError):
        return result
    for key in ("alt_centre", "az_centre"):
        val = pp.get(key)
        if isinstance(val, (int, float)):
            result[key] = round(val)

    # Resolution reconciliation: RMS discards the platepar (no astrometry) if the
    # .config width/height differ from the platepar X_res/Y_res. Publish both and
    # a strict-mismatch flag so health can surface this silent data-killer.
    xr, yr = pp.get("X_res"), pp.get("Y_res")
    if isinstance(xr, (int, float)) and isinstance(yr, (int, float)):
        result["platepar_x_res"] = int(xr)
        result["platepar_y_res"] = int(yr)
        if station.config_width and station.config_height:
            result["config_width"] = station.config_width
            result["config_height"] = station.config_height
            result["platepar_res_mismatch"] = (
                int(xr) != station.config_width or int(yr) != station.config_height)

    # Config FOV sanity vs the fitted horizontal FOV: astrometry.net only searches
    # [0.75x, 1.5x] of config.fov_w, so if the real FOV is outside that window a
    # fresh auto-calibration would fail. Mirror RMS's own range (no arbitrary tol).
    fov_h = pp.get("fov_h")   # platepar fov_h is the horizontal FOV (deg)
    if isinstance(fov_h, (int, float)) and station.config_fov_w > 0:
        result["platepar_fov_h"] = round(fov_h, 1)
        result["config_fov_w"] = station.config_fov_w
        result["config_fov_mismatch"] = not (
            0.75 * station.config_fov_w <= fov_h <= 1.5 * station.config_fov_w)
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

    # Newest by NAME (filename carries the UTC timestamp) -- robust like the dir
    # walk above; age is still measured from the file's mtime (its write time).
    newest = max(images)
    result["newest_frame_age_s"] = round(now - _safe_mtime(newest), 1)
    base = os.path.splitext(os.path.basename(newest))[0]
    if base.endswith("_d"):
        result["frame_mode"] = "day"
    elif base.endswith("_n"):
        result["frame_mode"] = "night"
    return result


# ---------------------------------------------------------------------------
# Timelapse mp4 (silent-failure class: frame session done but no mp4)
# ---------------------------------------------------------------------------

# A failed ffmpeg can leave a 0-byte / stub file, so require a real size.
_MIN_TIMELAPSE_BYTES = 1024


def collect_timelapse(station, now=None):
    """Did the most recent completed frame session produce a timelapse mp4?

    RMS writes <id>_<start>_to_<end>_frametimes.json as it processes a frame
    session (before ffmpeg finalizes), and the matching ..._frames_timelapse.mp4
    on success. An ffmpeg failure leaves the json but no (or a stub) mp4, and is
    only logged as a WARNING -- so this outcome check is the way to catch it.
    """
    result = {
        "timelapse_mp4_present": None,    # newest session's mp4 present (ran-but-failed)
        "timelapse_session_age_s": None,  # age of newest session marker (json)
        "newest_timelapse_age_s": None,   # age of newest timelapse mp4 anywhere
        "frames_data_age_s": None,        # age of oldest frame data on disk
    }
    if not (station.save_frames and station.timelapse_generate_from_frames):
        return result
    now = now or time.time()
    fp = station.frames_path

    # Newest completed session (json written even when ffmpeg fails) + its mp4.
    suffix = "_frametimes.json"
    jsons = glob.glob(os.path.join(fp, "*" + suffix))
    if jsons:
        newest = max(jsons)   # latest session by name (sortable start/end timestamps)
        result["timelapse_session_age_s"] = round(now - _safe_mtime(newest), 1)
        prefix = os.path.basename(newest)[:-len(suffix)]
        mp4 = os.path.join(fp, prefix + "_frames_timelapse.mp4")
        try:
            result["timelapse_mp4_present"] = (
                os.path.isfile(mp4) and os.path.getsize(mp4) > _MIN_TIMELAPSE_BYTES)
        except OSError:
            result["timelapse_mp4_present"] = False

    # Newest timelapse mp4 of any session (for the "none being generated" check).
    mp4s = glob.glob(os.path.join(fp, "*_frames_timelapse.mp4"))
    if mp4s:
        result["newest_timelapse_age_s"] = round(now - _safe_mtime(max(mp4s)), 1)

    # Oldest frame data on disk (FramesFiles/<year>/<date>/...), so a station
    # that has accumulated frames for ages but produced no mp4 is still caught.
    date_dirs = [d for d in glob.glob(os.path.join(fp, "[0-9][0-9][0-9][0-9]", "*"))
                 if os.path.isdir(d)]
    if date_dirs:
        result["frames_data_age_s"] = round(now - _safe_mtime(min(date_dirs)), 1)  # oldest by name
    return result


# ---------------------------------------------------------------------------
# Delivered camera bandwidth (the "camera/link bitrate" class)
# ---------------------------------------------------------------------------


def newest_segment(station, now=None):
    """Path of the newest FINALIZED raw video segment, or None.

    Walks VideoFiles/<year>/<date>/<hour>/ and returns the newest segment whose
    mtime is at least one segment-duration old (the in-progress one is still
    growing). Shared by the cheap bandwidth read and the on-demand keyframe probe.
    """
    if not station.raw_video_save:
        return None
    now = now or time.time()
    hour_dir = station.video_path
    for _ in range(3):  # year -> date -> hour
        nxt = _latest_subdir(hour_dir)
        if not nxt:
            return None
        hour_dir = nxt

    dur = station.raw_video_duration or 30.0
    try:
        segs = [os.path.join(hour_dir, n) for n in os.listdir(hour_dir)
                if n.lower().endswith((".mkv", ".mp4"))]
    except (IOError, OSError):
        return None
    segs.sort(reverse=True)   # newest first by name (sortable timestamp)
    for s in segs:
        if now - _safe_mtime(s) >= dur:
            return s
    return segs[0] if segs else None


def collect_stream_bandwidth(station, now=None):
    """Delivered bitrate of the camera stream, from raw video segment SIZE.

    RMS (raw_video_save) writes fixed-duration .mkv segments, so bytes /
    segment-seconds is the delivered bitrate with no decode -- cheap enough for
    every cycle on a Pi. Rising bitrate that tracks dropped frames while the host
    signals stay clean is the camera/link-bandwidth signature.
    """
    now = now or time.time()
    result = {"stream_mbps": None, "stream_segment_age_s": None}
    seg = newest_segment(station, now)
    if seg is None:
        return result
    try:
        size = os.path.getsize(seg)
    except OSError:
        return result
    dur = station.raw_video_duration or 30.0
    result["stream_mbps"] = round(size * 8 / dur / 1e6, 1)
    result["stream_segment_age_s"] = round(now - _safe_mtime(seg), 1)
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
# RMS log level field, e.g. "2026/06/20 03:08:33-WARNING-BufferedCapture-line:..".
_WARNING_RE = re.compile(r"-WARNING-")
# RMS's actual day/night mode: the in-process `daytime_mode` flag, logged every
# ~60s by the capture watchdog ("daytime_mode_prev=True/False"). This is the only
# mode signal independent of save_frames/raw saving, so it's the ground truth.
_DAYTIME_MODE_RE = re.compile(r"daytime_mode_prev=(True|False)")

# Benign, high-volume RMS warnings ignored by default: computational artifacts
# or self-recovering races, not operational problems. Operators add more via
# config `log_warning_ignore` (these defaults always apply).
_DEFAULT_WARNING_IGNORE = [
    r"Too many candidate stars",                               # ExtractStars caps the list
    r"Could not record media_backend in observation summary",  # summary-lock race; capture continues
    r"(?:Runtime|Optimize|User|Deprecation|Future|Pending)Warning:",  # numpy/scipy/py warnings
    r"alignPlatepar: Fit did not converge",                    # self-recovers to original platepar
    r"Dropped frames timestamp queue exceeded safety limit",   # RMS memory-cap housekeeping;
    # the actual dropping is already covered by the dropped_frames check + drop_cause
    r"Fewer than \d+ images found, cannot create timelapse",   # tiny day/night-transition
    # session: RMS returns before writing any json/mp4, so timelapse_missing won't fire either
    r"too many sporadics per hour",                            # Flux QC: skips a too-noisy
    # directory for the meteor-rate calc -- a science-pipeline decision, not station health
]


def _compile_warning_ignore(extra):
    pats = _DEFAULT_WARNING_IGNORE + list(extra or [])
    return re.compile("|".join("(?:%s)" % p for p in pats))


# "Buffer fill: 12.3%, Dropped frames: 4 (last 10 min), 9 this session"
_BUFFER_RE = re.compile(
    r"Buffer fill:\s*([\d.]+)%.*Dropped frames:\s*(\d+).*?(\d+)\s+this session",
    re.IGNORECASE,
)

# Window over which we take the MAX buffer fill (the spike). The fill at the drop
# line itself has usually recovered, so a drop's cause is in the lead-up: a spike
# up to a few minutes before, which must stay visible as long as its drops are in
# the 10-min count -- hence a touch wider than 10 min.
_SPIKE_WINDOW_S = 900           # 15 min (timestamped path)
_SPIKE_WINDOW_POINTS = 90       # ~15 min at the ~10s log cadence (fallback path)

# RMS log-line timestamp prefix, e.g. "2026/03/14 07:19:34-INFO-...".
_LOG_TS_RE = re.compile(r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})")


def _parse_log_ts(line):
    """Epoch seconds for an RMS log line's leading timestamp, or None. Parsed as
    UTC -- only deltas are used, so the actual zone is irrelevant."""
    m = _LOG_TS_RE.match(line)
    if not m:
        return None
    try:
        return calendar.timegm(time.strptime(m.group(1), "%Y/%m/%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return None

# GStreamer pipeline (re)build: RMS logs the full pipeline string each time it
# (re)connects rtspsrc, so counting these in the tail measures reconnect churn.
_PIPELINE_BUILD_RE = re.compile(r"GStreamer pipeline string")
# Decoder-side corruption/timing faults: the in-pipeline symptom of packets lost
# upstream (a damaged keyframe), distinct from a clean back-pressure drop. Covers
# both the gst decoder and any libav fallback wording seen in RMS logs.
_DECODER_ERR_RE = re.compile(
    r"concealing\s+\d+|decreasing timestamp|error while decoding|corrupt(?:ed)? "
    r"(?:decoded )?frame|RTP: missed|lost.*packet",
    re.IGNORECASE,
)


def _newest_log(station):
    # RMS capture logs are named "log_<stationID>_<timestamp>_NNN.log". Anchor
    # the pattern to that "log_" prefix: a leading "*" would also match
    # "reprocess_log_<id>_*.log", which sorts AFTER "log_..." by name (so max()
    # would pick a stale reprocess log) and carries no capture/buffer lines.
    logs = glob.glob(os.path.join(station.log_path, "log_%s_*.log" % station.station_id))
    if not logs:
        # Fallbacks keep the "log_" anchor (still excluding reprocess_/launcher
        # prefixes); only as a last resort fall back to any .log.
        logs = (glob.glob(os.path.join(station.log_path, "log_*.log"))
                or glob.glob(os.path.join(station.log_path, "*.log")))
    if not logs:
        return None
    return max(logs)   # latest log by name (filename carries a sortable timestamp)


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


def collect_logs(station, max_lines, warning_ignore=None):
    """Scan the newest log for fatal errors, watchdog events, and buffer stats.
    `warning_ignore` adds patterns to the built-in benign-warning filter."""
    result = {
        "log_file": None,
        "log_age_s": None,
        "fatal_error_count": 0,
        "last_error": None,
        "warning_count": 0,
        "last_warning": None,
        "last_watchdog_event": None,
        "buffer_fill_pct": None,
        "buffer_fill_max_recent": None,
        "dropped_frames_10min": None,
        "dropped_frames_session": None,
        "pipeline_reconnects": 0,
        "decoder_errors": 0,
        "rms_mode": None,   # RMS's actual day/night mode (ground truth, see below)
    }
    log_path = _newest_log(station)
    if not log_path:
        return result
    buffer_points = []   # (ts_epoch_or_None, fill_pct) for each Buffer-fill line

    result["log_file"] = os.path.basename(log_path)
    result["log_age_s"] = round(time.time() - _safe_mtime(log_path), 1)

    ignore_re = _compile_warning_ignore(warning_ignore)
    lines = _tail(log_path, max_lines)
    for idx, line in enumerate(lines):
        is_fatal = False
        for pattern in _FATAL_PATTERNS:
            if pattern.search(line):
                is_fatal = True
                result["fatal_error_count"] += 1
                # Redact before storing: these fields are published to a public
                # feed and raw RMS lines can carry IPs / device-URL credentials.
                if "Traceback" in line:
                    result["last_error"] = redact(_extract_traceback(lines, idx))
                else:
                    result["last_error"] = redact(line.strip())[:300]
                break

        # WARNING-level lines that are neither fatal nor a known-benign pattern.
        if not is_fatal and _WARNING_RE.search(line) and not ignore_re.search(line):
            result["warning_count"] += 1
            result["last_warning"] = redact(line.strip())[:300]

        if _WATCHDOG_RE.search(line):
            result["last_watchdog_event"] = redact(line.strip())[:300]

        buf = _BUFFER_RE.search(line)
        if buf:
            fill = float(buf.group(1))
            result["buffer_fill_pct"] = fill
            result["dropped_frames_10min"] = int(buf.group(2))
            result["dropped_frames_session"] = int(buf.group(3))
            buffer_points.append((_parse_log_ts(line), fill))

        if _PIPELINE_BUILD_RE.search(line):
            result["pipeline_reconnects"] += 1
        elif _DECODER_ERR_RE.search(line):
            result["decoder_errors"] += 1

        # RMS's actual day/night mode (ground truth from the in-process flag).
        # Lines are chronological, so the last match in the tail is the newest.
        mode = _DAYTIME_MODE_RE.search(line)
        if mode:
            result["rms_mode"] = "day" if mode.group(1) == "True" else "night"

    # Peak buffer fill in the recent window -- the back-pressure signal, since the
    # fill at the drop line has usually recovered to baseline. Prefer the
    # timestamped window; fall back to the last N points if timestamps don't parse.
    if buffer_points:
        timed = [(t, f) for t, f in buffer_points if t is not None]
        if timed:
            newest = timed[-1][0]
            recent = [f for t, f in timed if 0 <= newest - t <= _SPIKE_WINDOW_S]
        else:
            recent = [f for _, f in buffer_points[-_SPIKE_WINDOW_POINTS:]]
        if recent:
            result["buffer_fill_max_recent"] = round(max(recent), 1)

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

    newest = max(files)   # latest night by name (the archived-dir path sorts chronologically)
    result["summary_age_s"] = round(time.time() - _safe_mtime(newest), 1)
    try:
        with open(newest) as fh:
            data = json.load(fh)
    except (IOError, OSError, ValueError):
        return result

    result["summary"] = {k: data.get(k) for k in _SUMMARY_FIELDS if k in data}
    return result


def rms_branch(rms_dir):
    """Current git branch of the RMS code checkout (e.g. 'master', 'prerelease').

    Read-only and offline -- it does not fetch, so it can't perturb RMS. Returns
    None when rms_dir isn't a git checkout or git is unavailable; returns the
    literal 'HEAD' that git reports for a detached checkout. Host-wide: every
    camera on a box shares one RMS checkout, so the value is the same for all.
    (Whether that branch is up to date is RMS's own
    `summary.repository_lag_remote_days`, which already does the remote check.)
    """
    rms_dir = os.path.expanduser(rms_dir or "")
    if not rms_dir or not os.path.isdir(os.path.join(rms_dir, ".git")):
        return None
    try:
        out = subprocess.run(
            ["git", "-C", rms_dir, "rev-parse", "--abbrev-ref", "HEAD"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=10, universal_newlines=True)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return (out.stdout or "").strip() or None


def _git(rms_dir, *args):
    """Run a git command in rms_dir, returning stripped stdout or None."""
    try:
        out = subprocess.run(["git", "-C", rms_dir] + list(args),
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=20, universal_newlines=True)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return (out.stdout or "").strip() or None


def _resolve_upstream(rms_dir, branch):
    """(remote, upstream_branch) for `branch`, mirroring RMS_Update.sh's
    resolve_branch_remote(): the configured @{upstream} if set (handles
    origin/upstream/rms naming), else a remote that actually has the branch.
    Returns (None, None) if it can't be resolved."""
    full = _git(rms_dir, "rev-parse", "--abbrev-ref", "--symbolic-full-name",
                "%s@{upstream}" % branch)
    if full and "/" in full:
        remote, _, up = full.partition("/")
        if remote and up:
            return remote, up
    for r in (_git(rms_dir, "remote") or "").split():
        if _git(rms_dir, "ls-remote", "--exit-code", "--heads", r,
                "refs/heads/%s" % branch) is not None:
            return r, branch
    return None, None


def _git_ok(cwd, *args, **kw):
    """Run a side-effecting git command (init/fetch); return True on rc 0."""
    try:
        return subprocess.run(["git", "-C", cwd] + list(args),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=kw.get("timeout", 30)).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _commit_epoch(repo, ref):
    """Committer-date (unix seconds) of a ref, or None."""
    try:
        return int(_git(repo, "log", "-1", "--format=%ct", ref) or "")
    except (TypeError, ValueError):
        return None


def _remote_tip_via_temp(url, branch):
    """(tip_sha, tip_committer_epoch) for url's branch, fetched into a THROWAWAY
    temp repo -- never touches the live RMS checkout (mirrors RMS's daysBehind(),
    which temp-clones rather than fetch into the live repo). Shallow, single
    branch. (None, None) on any failure."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            if not _git_ok(tmp, "init", "-q"):
                return None, None
            # blob:none + tree:0: fetch only the commit object (we just need its
            # date), not the file tree -- keeps it to a fast metadata-only pull.
            if not _git_ok(tmp, "fetch", "--depth=1", "--filter=tree:0", "--quiet",
                           url, "refs/heads/%s" % branch, timeout=30):
                return None, None
            return _git(tmp, "rev-parse", "FETCH_HEAD"), _commit_epoch(tmp, "FETCH_HEAD")
    except (OSError, subprocess.SubprocessError):
        return None, None


# The remote is consulted at most once per TTL per checkout (a shallow fetch into
# a temp dir -- the live RMS repo is never written to, only its HEAD + remote URL
# are read).
_REPO_STATUS_CACHE = {}     # rms_dir -> (monotonic_ts, status_dict)
_REPO_STATUS_TTL = 1800     # 30 min


def rms_repo_status(rms_dir):
    """How current the RMS checkout is vs its actual remote.

    Returns a dict (empty when undeterminable -- offline, detached HEAD, no
    upstream, not a git repo):
      rms_up_to_date  -- HEAD equals the live remote tip (quick yes/no)
      rms_behind_days -- days HEAD lags the remote tip, matching RMS's own
                         repository_lag_remote_days (remote-tip commit date minus
                         local HEAD commit date); 0.0 when current.

    A local `HEAD..@{u}` count is fooled by a stale tracking ref (the bug this
    fixes). Instead: the up-to-date GATE is `git ls-remote` vs HEAD -- no fetch,
    exactly as RMS_Update.sh decides it. Only when actually behind do we spend a
    shallow fetch into a THROWAWAY temp repo to read the remote commit date for
    the day-lag (ls-remote gives no date), as RMS's daysBehind() does. Either way
    the live RMS checkout is never written to. Resolution mirrors RMS_Update.sh.
    """
    rms_dir = os.path.expanduser(rms_dir or "")
    if not rms_dir or not os.path.isdir(os.path.join(rms_dir, ".git")):
        return {}

    now = time.monotonic()
    cached = _REPO_STATUS_CACHE.get(rms_dir)
    if cached is not None and now - cached[0] < _REPO_STATUS_TTL:
        return cached[1]

    status = {}
    branch = _git(rms_dir, "rev-parse", "--abbrev-ref", "HEAD")
    head = _git(rms_dir, "rev-parse", "HEAD")
    if branch and branch != "HEAD" and head:
        remote, up = _resolve_upstream(rms_dir, branch)
        if remote and up:
            # The up-to-date GATE: ls-remote SHA vs HEAD, exactly as RMS_Update.sh
            # does it (no fetch, zero-touch).
            line = _git(rms_dir, "ls-remote", remote, "refs/heads/%s" % up)
            tip_sha = line.split()[0] if line else None
            if tip_sha:
                status["rms_up_to_date"] = (head == tip_sha)
                if head == tip_sha:
                    status["rms_behind_days"] = 0.0
                else:
                    # Behind/diverged: only NOW spend a temp fetch to get the
                    # remote commit date for the day-lag (ls-remote gives no date).
                    url = _git(rms_dir, "remote", "get-url", remote)
                    head_epoch = _commit_epoch(rms_dir, "HEAD")
                    _, tip_epoch = _remote_tip_via_temp(url, up) if url else (None, None)
                    if head_epoch is not None and tip_epoch is not None:
                        status["rms_behind_days"] = round(
                            max(0.0, (tip_epoch - head_epoch) / 86400.0), 1)
    _REPO_STATUS_CACHE[rms_dir] = (now, status)
    return status


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


# RMS day/night switch horizons (degrees below the horizon), matched to RMS so
# our expectation flips exactly when the camera does:
#   continuous + switch_camera_modes -> CaptureModeSwitcher SWITCH_HORIZON (-9)
#   otherwise                        -> CaptureDuration CAPTURE_HORIZON (-5:26)
_SWITCH_HORIZON_CONTINUOUS = -9.0
_SWITCH_HORIZON_STANDARD = -5.4
# Hysteresis band (deg) around the switch where the camera is reconfiguring and
# we expect nothing in particular -- so the switch-over never false-alarms.
_TRANSITION_BUFFER_DEG = 3.0


def _expected_output(station, elev):
    """What disk output should currently be fresh, from the sun + capture mode.

    Returns "ff" (night FF compression), "frames" (daytime continuous frame
    images), "idle" (nothing expected), or "transition" (mid-switch, no alarm).
    Independent of whether frames are actually being written.
    """
    cont = station.continuous_capture
    switch = station.switch_camera_modes
    if cont and not switch:
        return "ff"  # one fixed (night) mode, compressing FF 24/7

    horizon = _SWITCH_HORIZON_CONTINUOUS if (cont and switch) else _SWITCH_HORIZON_STANDARD
    if elev < horizon - _TRANSITION_BUFFER_DEG:
        return "ff"  # night
    if elev > horizon + _TRANSITION_BUFFER_DEG:
        # day: continuous keeps saving frames; standard capture is idle
        return "frames" if (cont and station.save_frames) else "idle"
    return "transition"


def collect_mode(station, now=None):
    """Capture-mode context: sun elevation and what output to expect now.

    Prefer RMS's own switch logic (ephem + RMS horizons + programmed delays);
    fall back to the self-contained NOAA approximation if ephem isn't available
    or the computation can't be done (e.g. polar day/night)."""
    now = now or time.time()
    result = {
        "continuous_capture": station.continuous_capture,
        "switch_camera_modes": station.switch_camera_modes,
        "save_frames": station.save_frames,
        "solar_elevation_deg": None,
        "expected_output": None,
        "mode_source": None,
    }
    if station.has_location:
        result["solar_elevation_deg"] = round(
            solar_elevation_deg(station.latitude, station.longitude, now), 2)
        rms_expected = rmsmode.expected_output(station, now)
        if rms_expected is not None:
            result["expected_output"] = rms_expected
            result["mode_source"] = "rms-ephem"
        else:
            elev = solar_elevation_deg(station.latitude, station.longitude, now)
            result["expected_output"] = _expected_output(station, elev)
            result["mode_source"] = "approx"
    return result


def collect_station(station, max_log_lines, now=None, warning_ignore=None):
    """Run every collector and merge into one flat metrics dict."""
    now = now or time.time()
    metrics = {"station_id": station.station_id}
    metrics.update(collect_process(station))
    metrics.update(collect_capture(station, now))
    metrics.update(collect_platepar(station))
    metrics.update(collect_frames(station, now))
    metrics.update(collect_timelapse(station, now))
    metrics.update(collect_stream_bandwidth(station, now))
    metrics.update(collect_detection(station, now))
    metrics.update(collect_logs(station, max_log_lines, warning_ignore))
    metrics.update(collect_summary(station))
    metrics.update(collect_upload(station, now))
    metrics.update(collect_disk(station))
    metrics.update(collect_mode(station, now))
    return metrics
