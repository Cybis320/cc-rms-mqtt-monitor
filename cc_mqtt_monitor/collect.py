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


def _proc_age_s(pid):
    """Seconds since process <pid> started, or None if it can't be determined.

    Derived from the kernel's starttime (jiffies since boot, /proc/<pid>/stat
    field 22) versus /proc/uptime, so it is the process's TRUE age -- independent
    of when the monitor started watching. Used to grant a just-(re)started
    capture a settling grace before its stale-output age can count as a stall."""
    try:
        with open("/proc/%d/stat" % pid) as fh:
            data = fh.read()
        with open("/proc/uptime") as fh:
            uptime = float(fh.read().split()[0])
    except (IOError, OSError, ValueError, IndexError):
        return None
    rpar = data.rfind(")")
    if rpar < 0:
        return None
    fields = data[rpar + 2:].split()      # fields from "state" on (post-comm)
    try:
        starttime_ticks = float(fields[19])   # field 22 (post-comm index 19)
    except (ValueError, IndexError):
        return None
    return round(uptime - starttime_ticks / _CLK_TCK, 1)


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
    procs = list(_iter_proc())          # (pid, args, ppid, vmrss_kb)

    # The StartCapture process(es) carrying THIS station's config on their cmdline.
    cmd_pids = {pid for pid, args, ppid, _ in procs
                if any(_STARTCAPTURE_MARKER in a for a in args)
                and _process_config_path(pid, args) == target}
    # Main = a cmdline match whose parent isn't also a match (the tree root).
    main_pid = None
    for pid, args, ppid, _ in procs:
        if pid in cmd_pids and ppid not in cmd_pids:
            main_pid = pid
            break

    # Count the WHOLE tree under main, not just cmdline matches: with the
    # multiprocessing 'forkserver' start method (py3.14 support) the workers are
    # spawned by a forkserver helper and DON'T inherit the StartCapture cmdline --
    # only the main process does. Walking the PPID tree from main captures them
    # (and the classic 'fork' children too).
    children, rss = {}, {}
    for pid, args, ppid, vmrss_kb in procs:
        children.setdefault(ppid, []).append(pid)
        rss[pid] = vmrss_kb
    tree, stack = set(), ([main_pid] if main_pid is not None else [])
    while stack:
        p = stack.pop()
        if p in tree:
            continue
        tree.add(p)
        stack.extend(children.get(p, []))

    total_rss_mb = round(sum(rss.get(p, 0) for p in tree) / 1024.0, 1)

    # CPU% of the whole capture tree over the interval (delta of cumulative
    # utime+stime / wall time). A saturated capture process is the on-station
    # signature of CPU back-pressure dropping frames. First sample => null.
    cpu_pct = None
    jiffies = sum(_proc_cpu_jiffies(pid) for pid in tree)
    now = time.monotonic()
    last = _PROC_CPU_LAST.get(station.station_id)
    if tree and last is not None and now > last[1] and jiffies >= last[0]:
        secs = (jiffies - last[0]) / float(_CLK_TCK)
        cpu_pct = round(secs / (now - last[1]) * 100.0, 1)
    if tree:
        _PROC_CPU_LAST[station.station_id] = (jiffies, now)
    else:
        _PROC_CPU_LAST.pop(station.station_id, None)

    return {
        "capture_alive": bool(tree),
        "process_count": len(tree),
        "main_pid": main_pid,
        # Age of the capture tree's main process. A staggered GRMSUpdater restart
        # (and RMS's own capture_wait_seconds pre-capture sleep) means the tail
        # cameras come back minutes apart; the stall check uses this to give each
        # station a settling grace measured from ITS OWN restart, not host-wide.
        "capture_age_s": _proc_age_s(main_pid) if main_pid is not None else None,
        "total_rss_mb": total_rss_mb,
        "capture_cpu_pct": cpu_pct,
    }


def collect_data_access(station):
    """Whether the monitor can actually READ the station's (post-fallback)
    data_dir. False = it exists but permission is denied -- the case where an
    RMS instance runs as a different user with private, non-group-readable data
    and no readable copy is exposed, so logs/FF/detections all come back empty
    and the station would otherwise look dead. None when undeterminable."""
    result = {"data_dir_readable": None}
    try:
        os.listdir(station.data_dir)
        result["data_dir_readable"] = True
    except PermissionError:
        result["data_dir_readable"] = False
    except OSError:
        pass        # missing/other -> leave None (handled by the freshness checks)
    return result


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
    # Cython/C build failures at first import (pyximport compiles .pyx on demand):
    # a broken build crashes StartCapture before RMS logging is up, so this only
    # ever surfaces via the systemd journal (see collect_journal_fatal).
    re.compile(r"\bCompileError\b|\bDistutilsExecError\b"),
    re.compile(r"Building module .* failed"),
]

_WATCHDOG_RE = re.compile(r"WATCHDOG:.*(died|stale|Restarting)", re.IGNORECASE)
# RMS log level field, e.g. "2026/06/20 03:08:33-WARNING-BufferedCapture-line:..".
_WARNING_RE = re.compile(r"-WARNING-")
# ExtractStars overflow: "Too many candidate stars to process! 920/800". When the
# candidate list exceeds the cap (2nd number), RMS SKIPS extraction for that frame
# and logs "Detected stars: 0" -- so a rich (good!) field looks like zero stars.
# We use this only to DISAMBIGUATE that 0: stars_recent reports ">800" (the cap)
# for such a frame instead of a misleading 0 (see collect_capture_events). The
# warning itself stays benign/ignored (_DEFAULT_WARNING_IGNORE) -- no alert.
_STAR_OVERFLOW_RE = re.compile(r"Too many candidate stars to process!\s*\d+\s*/\s*(\d+)")
# RMS's actual day/night mode: the in-process `daytime_mode` flag, logged every
# ~60s by the capture watchdog ("daytime_mode_prev=True/False"). This is the only
# mode signal independent of save_frames/raw saving, so it's the ground truth.
_DAYTIME_MODE_RE = re.compile(r"daytime_mode_prev=(True|False)")
# Actual capture backend, from the BufferedCapture init log line. RMS silently
# falls back from GStreamer to OpenCV (cv2) if gst can't start, so the live log
# is the only truth. The last init marker in the tail is the current backend.
_BACKEND_GST_RE = re.compile(r"GStreamer pipeline created!")
_BACKEND_CV2_RE = re.compile(r"Initialize OpenCV Device|Using OpenCV\.")

# Per-session capture-stability counters (reset at each RMS day<->night
# transition, where RMS resets its own counters too):
#   - unplanned disconnect: the stream dropped and forced a reconnect (distinct
#     from the planned mode-switch resource release).
#   - watchdog restart: RMS's capture watchdog restarted the BufferedCapture
#     process; the log carries a per-mode-session "restart #N" running count.
_TRANSITION_RE = re.compile(r"transition detected")           # day<->night boundary
_DISCONNECT_RE = re.compile(r"video device is probably disconnected")
_WD_RESTART_RE = re.compile(r"WATCHDOG: Restarting BufferedCapture.*restart #(\d+)")
# Real-time per-FF meteor count, logged as each FF is processed ("...detected
# meteors: N"); summed over the session = live meteor total. Matches the
# end-of-night "TOTAL: N" line. (Note the colon: this does NOT match the summary
# line "TOTAL: N detected meteors.", which has no "detected meteors:<num>".)
_METEOR_RE = re.compile(r"detected meteors:\s*(\d+)")
# Per-FF star count ("...-DetectStarsAndMeteors-... - Detected stars: N"). An
# instantaneous sky-transparency reading (NOT accumulated like meteors): we keep
# the most recent value as a live limiting-magnitude / cloud proxy. 0 by day.
_STARS_RE = re.compile(r"Detected stars:\s*(\d+)")

# Benign, high-volume RMS warnings ignored by default: computational artifacts
# or self-recovering races, not operational problems. Operators add more via
# config `log_warning_ignore` (these defaults always apply).
_DEFAULT_WARNING_IGNORE = [
    r"Too many candidate stars",  # benign/noisy per-frame; not alerted -- only
                                  # used to encode stars_recent as ">cap"
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
        "media_backend": station.media_backend,   # configured (gst/cv2/v4l2)
        "capture_backend": None,                   # actual, from the log (gst/cv2)
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

        # Actual capture backend: last init marker in the tail wins (a gst->cv2
        # fallback logs the gst attempt then the OpenCV init, so cv2 ends last).
        if _BACKEND_GST_RE.search(line):
            result["capture_backend"] = "gst"
        elif _BACKEND_CV2_RE.search(line):
            result["capture_backend"] = "cv2"

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
# Journal fatal scan: crashes that never reach the RMS log file
# ---------------------------------------------------------------------------
# An import/build crash (e.g. pyximport rebuilding a .pyx after a bad update)
# kills StartCapture BEFORE RMS's file logging is initialised, so the traceback
# goes to stderr -> the systemd journal, never the RMS log. A log-file scanner
# structurally can't see it; the station only shows the downstream symptom (a
# stall) with no root cause. We read the capture unit's journal tail and match
# the same fatal patterns, so the real error reaches last_error / log_fatal.
#
# This only applies to systemd-managed captures (a service/scope journald
# captures). A capture launched in a terminal (gnome-terminal vte-spawn scope)
# or screen/tmux writes to that terminal, not the journal, so _capture_unit
# returns None and this is a clean no-op -- there is nothing to find there.

_JOURNAL_LINES = 400          # journal tail depth to scan per station
_UNIT_CACHE_FILE = os.path.expanduser("~/.cache/cc-rms-monitor/units.json")
_UNIT_CACHE = None            # station_id -> systemd unit (e.g. "au0004.service")


def _unit_cache():
    global _UNIT_CACHE
    if _UNIT_CACHE is None:
        try:
            with open(_UNIT_CACHE_FILE) as fh:
                _UNIT_CACHE = json.load(fh)
            if not isinstance(_UNIT_CACHE, dict):
                _UNIT_CACHE = {}
        except (IOError, OSError, ValueError):
            _UNIT_CACHE = {}
    return _UNIT_CACHE


def _save_unit_cache():
    try:
        os.makedirs(os.path.dirname(_UNIT_CACHE_FILE), exist_ok=True)
        tmp = _UNIT_CACHE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(_UNIT_CACHE, fh)
        os.replace(tmp, _UNIT_CACHE_FILE)
    except (IOError, OSError):
        pass


def _proc_unit(pid):
    """The systemd unit owning `pid`, parsed from /proc/<pid>/cgroup -- e.g.
    'au0004.service'. None if the pid isn't under a real service unit: a
    terminal/screen scope (vte-spawn-*.scope) or the bare user manager
    (user@N.service) is NOT a capture service, so its output isn't unit-captured
    and there's nothing to scan."""
    if not pid:
        return None
    try:
        with open("/proc/%d/cgroup" % pid) as fh:
            data = fh.read()
    except (IOError, OSError):
        return None
    # cgroup v2: "0::/system.slice/au0004.service" (system) or
    # ".../user@1000.service/app.slice/au0004.service" (user service). Take the
    # innermost '*.service', but never the user manager itself (user@N.service).
    for part in reversed(data.strip().replace("\0", "").split("/")):
        part = part.strip()
        if part.endswith(".service") and not part.startswith("user@"):
            return part
    return None


def _capture_unit(station, main_pid):
    """Resolve the station's capture systemd unit. From a live pid's cgroup when
    available (cached persistently, keyed by station), else the cached value --
    so a crash that leaves NO live process (the very case we want to explain) is
    still attributable to the unit we saw it run under. None => not systemd
    managed / never observed => nothing to scan."""
    cache = _unit_cache()
    unit = _proc_unit(main_pid)
    if unit:
        if cache.get(station.station_id) != unit:
            cache[station.station_id] = unit
            _save_unit_cache()
        return unit
    return cache.get(station.station_id)


def _journal_tail(unit, lines):
    """Last `lines` journal messages (text only) for a system OR user unit.
    Matches both `_SYSTEMD_UNIT` and `_SYSTEMD_USER_UNIT` (OR via '+') so it works
    regardless of how the service is scoped. Best-effort: no journalctl, no read
    permission, or any error -> []."""
    if not shutil.which("journalctl"):
        return []
    try:
        out = subprocess.run(
            ["journalctl", "_SYSTEMD_UNIT=%s" % unit, "+",
             "_SYSTEMD_USER_UNIT=%s" % unit, "-n", str(lines),
             "-o", "cat", "--no-pager"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=15, universal_newlines=True)
    except (OSError, subprocess.SubprocessError):
        return []
    return (out.stdout or "").splitlines()


def collect_journal_fatal(station, main_pid):
    """Scan the capture unit's journal tail for fatal errors that never made it
    to the RMS log (import/build crashes on startup). Returns {} for non-systemd
    captures or when nothing matches, else {fatal_error_count, last_error,
    fatal_source:'journal'} mirroring collect_logs so log_fatal fires."""
    unit = _capture_unit(station, main_pid)
    if not unit:
        return {}
    lines = _journal_tail(unit, _JOURNAL_LINES)
    if not lines:
        return {}
    count, last = 0, None
    for idx, line in enumerate(lines):
        for pattern in _FATAL_PATTERNS:
            if pattern.search(line):
                count += 1
                if "Traceback" in line:
                    last = redact(_extract_traceback(lines, idx))
                else:
                    last = redact(line.strip())[:300]
                break
    if not count:
        return {}
    return {"fatal_error_count": count, "last_error": last,
            "fatal_source": "journal"}


def collect_capture_events(station):
    """Capture-stability counts for the CURRENT day/night session.

    Streams the whole current log (the buffer/dropped tail in collect_logs is too
    short to span a multi-hour session) and resets the counters at each RMS
    day<->night transition -- so the result is "since this session began", the
    same boundary RMS uses to reset its own counters:
      disconnects_session       -- unplanned stream drops that forced a reconnect
                                   (not the planned mode-switch release)
      watchdog_restarts_session -- RMS capture-watchdog restarts (from its own
                                   per-session "restart #N", so it's exact even if
                                   earlier restart lines have scrolled away)
      meteors_session           -- meteors detected this session (sum of the
                                   real-time per-FF "detected meteors: N"); a live
                                   running total for flux on the dashboard
      stars_recent              -- the most recent per-FF star count (NOT summed):
                                   a live sky-transparency reading. An int normally
                                   (0 by day / clouded), or the STRING ">N" when
                                   ExtractStars overflowed its N-candidate cap and
                                   skipped that frame (it logs "Detected stars: 0",
                                   but the field was actually too rich to count, so
                                   0 would mislead). None until the first FF.
    All null if the log can't be read. O(1) memory (line-streamed).
    """
    result = {"disconnects_session": None, "watchdog_restarts_session": None,
              "meteors_session": None, "stars_recent": None}
    log_path = _newest_log(station)
    if not log_path:
        return result
    disc, wd, met, stars = 0, 0, 0, None
    overflow_cap = None   # set by an overflow line; consumed by the next star line
    try:
        with open(log_path, errors="replace") as fh:
            for line in fh:
                if _TRANSITION_RE.search(line):
                    disc, wd, met = 0, 0, 0      # new session -> reset (as RMS does)
                    continue
                if _DISCONNECT_RE.search(line):
                    disc += 1
                    continue
                m = _WD_RESTART_RE.search(line)
                if m:
                    wd = max(wd, int(m.group(1)))   # RMS's running restart #N
                    continue
                m = _METEOR_RE.search(line)
                if m:
                    met += int(m.group(1))
                    continue
                so = _STAR_OVERFLOW_RE.search(line)
                if so:
                    overflow_cap = int(so.group(1))   # "N/M" -> the cap M; frame skipped
                    continue
                m = _STARS_RE.search(line)
                if m:
                    n = int(m.group(1))
                    # An overflow frame logs "Detected stars: 0"; report ">cap"
                    # instead so a too-rich field isn't shown as zero stars.
                    stars = ">%d" % overflow_cap if (overflow_cap and n == 0) else n
                    overflow_cap = None   # consumed
    except (IOError, OSError):
        return result
    result["disconnects_session"] = disc
    result["watchdog_restarts_session"] = wd
    result["meteors_session"] = met
    result["stars_recent"] = stars
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


# Strip any embedded userinfo (https://user:token@host -> https://host) before
# publishing a remote URL to the open feed -- public RMS clones have none, but
# this guards a checkout configured with credentials in the URL.
_URL_CRED_RE = re.compile(r"(://)[^/@]+@")


def rms_remote(rms_dir):
    """URL of the RMS checkout's remote -- the repo it pulls from (usually
    'origin'; the current branch's configured remote if set, else origin, else
    the first remote). Read-only and offline. None if not a git repo / no remote.
    Any URL-embedded credentials are stripped before it's returned."""
    rms_dir = os.path.expanduser(rms_dir or "")
    if not rms_dir or not os.path.isdir(os.path.join(rms_dir, ".git")):
        return None
    branch = _git(rms_dir, "rev-parse", "--abbrev-ref", "HEAD")
    tracked = _git(rms_dir, "config", "branch.%s.remote" % branch) if branch and branch != "HEAD" else None
    candidates = [tracked, "origin"] + (_git(rms_dir, "remote") or "").split()
    for r in candidates:
        if not r:
            continue
        url = _git(rms_dir, "remote", "get-url", r)
        if url:
            return _URL_CRED_RE.sub(r"\1", url)
    return None


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


def _head_signal(rms_dir):
    """Cheap 'has HEAD moved?' marker: the reflog mtime (appended on every HEAD
    change -- commit/pull/reset, so it ticks the moment RMS_Update lands an
    update), falling back to .git/HEAD. A single stat (microseconds). None if
    unreadable -> caching then relies on the TTL alone."""
    for rel in ("logs/HEAD", "HEAD"):
        try:
            return os.stat(os.path.join(rms_dir, ".git", rel)).st_mtime
        except OSError:
            continue
    return None


# up_to_date (the ls-remote check) is cached per TTL AND invalidated the instant
# HEAD moves (a stat-cheap reflog check), so the dashboard reflects an RMS_Update
# within a cycle. The update-age is recomputed fresh every call (just a stat).
_UPTODATE_CACHE = {}     # rms_dir -> (monotonic_ts, up_to_date_or_None, head_signal)
_UPTODATE_TTL = 1800     # 30 min

# Persisted "behind since" clock: the wall-clock time we FIRST observed the
# current HEAD to be behind its remote tip. Kept in the user's home so it
# survives monitor restarts (the nightly auto-update) and reboots -- otherwise a
# multi-day "out of date" would reset to ~0 every restart.
_STATE_FILE = os.path.expanduser("~/.cache/cc-rms-monitor/repo_state.json")
_REPO_STATE = None       # {realpath(rms_dir): {"head": sha, "since": epoch|None}}


def _repo_state():
    global _REPO_STATE
    if _REPO_STATE is None:
        try:
            with open(_STATE_FILE) as fh:
                _REPO_STATE = json.load(fh)
            if not isinstance(_REPO_STATE, dict):
                _REPO_STATE = {}
        except (IOError, OSError, ValueError):
            _REPO_STATE = {}
    return _REPO_STATE


def _save_repo_state():
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(_REPO_STATE, fh)
        os.replace(tmp, _STATE_FILE)
    except (IOError, OSError):
        pass


def _check_up_to_date(rms_dir):
    """True/False if HEAD equals the live remote tip -- `git ls-remote` vs HEAD,
    exactly as RMS_Update.sh's gate (no fetch, zero-touch). None if undeterminable
    (detached HEAD, no upstream, offline). Resolution mirrors RMS_Update.sh."""
    branch = _git(rms_dir, "rev-parse", "--abbrev-ref", "HEAD")
    head = _git(rms_dir, "rev-parse", "HEAD")
    if not (branch and branch != "HEAD" and head):
        return None
    remote, up = _resolve_upstream(rms_dir, branch)
    if not (remote and up):
        return None
    line = _git(rms_dir, "ls-remote", remote, "refs/heads/%s" % up)
    tip_sha = line.split()[0] if line else None
    if not tip_sha:
        return None
    return head == tip_sha


def rms_repo_status(rms_dir):
    """How current the RMS checkout is. Returns a dict (empty if not a git repo):

      rms_up_to_date      -- HEAD equals the live remote tip (ls-remote vs HEAD,
                             like RMS_Update.sh's gate; no fetch, zero-touch).
      rms_out_of_date_days-- days the checkout has been behind: now minus when we
                             FIRST observed this HEAD to be behind its remote tip
                             (i.e. when the branch ref first moved past it). 0.0
                             while up to date.

    This is deliberately NOT a commit-date lag (a commit's date can long predate
    when it lands on the branch -- a PR that sat for weeks merges today -- so a
    commit-date "days behind" jumps the instant such a commit merges). Instead we
    stamp the wall-clock moment the ref first advanced past this HEAD and count
    from there. Git exposes no ref-move timestamp, so we observe it by polling
    (ls-remote, cached per TTL / re-checked the instant HEAD moves) and persist
    the stamp across restarts. Resolution ~= the poll interval.
    """
    rms_dir = os.path.expanduser(rms_dir or "")
    if not rms_dir or not os.path.isdir(os.path.join(rms_dir, ".git")):
        return {}
    head = _git(rms_dir, "rev-parse", "HEAD")
    if not head:
        return {}

    sig = _head_signal(rms_dir)        # reflog mtime: HEAD-moved trigger for the cache
    mono = time.monotonic()
    cached = _UPTODATE_CACHE.get(rms_dir)
    if cached is not None and mono - cached[0] < _UPTODATE_TTL and sig == cached[2]:
        up = cached[1]
    else:
        up = _check_up_to_date(rms_dir)
        _UPTODATE_CACHE[rms_dir] = (mono, up, sig)

    status = {}
    if up is not None:
        status["rms_up_to_date"] = up

    # Persisted behind-since clock, keyed by checkout, updated on transitions.
    state = _repo_state()
    key = os.path.realpath(rms_dir)
    entry = dict(state.get(key) or {})
    now = time.time()
    changed = False
    if entry.get("head") != head:              # HEAD moved (pulled/reset) -> new clock
        entry = {"head": head, "since": None}
        changed = True
    if up is True and entry.get("since") is not None:
        entry["since"] = None                  # caught up -> stop the clock
        changed = True
    elif up is False and entry.get("since") is None:
        entry["since"] = now                   # first seen behind -> start the clock
        changed = True
    # up is None (offline/undeterminable): leave the clock untouched.
    if changed:
        state[key] = entry
        _save_repo_state()

    if up is not None:
        since = entry.get("since")
        status["rms_out_of_date_days"] = (0.0 if since is None
                                          else round(max(0.0, (now - since) / 86400.0), 1))
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
        # RMS's per-station programmed delay: applied both as a mode-switch
        # stagger and as a pre-capture sleep on every (non-resume) StartCapture,
        # so it widens the no-output window right after a restart. The stall
        # check adds it to the settling grace.
        "capture_wait_seconds": station.capture_wait_seconds,
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
    metrics.update(collect_data_access(station))
    metrics.update(collect_logs(station, max_log_lines, warning_ignore))
    # Startup crashes (import/build failures) die before RMS logging is up, so
    # they never reach the log file scanned above -- only the systemd journal. If
    # the log scan found no fatal, check the capture unit's journal so the real
    # root cause reaches last_error / log_fatal instead of only a downstream stall.
    if not metrics.get("fatal_error_count"):
        metrics.update(collect_journal_fatal(station, metrics.get("main_pid")))
    metrics.update(collect_capture_events(station))
    metrics.update(collect_summary(station))
    metrics.update(collect_upload(station, now))
    metrics.update(collect_disk(station))
    metrics.update(collect_mode(station, now))
    return metrics
