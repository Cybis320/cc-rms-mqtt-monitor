"""Turn raw per-station metrics into a status verdict.

Status levels (worst wins):

    ok        -- everything nominal
    degraded  -- a non-fatal concern (warnings, backlog, stale code)
    error     -- capture down, pipeline stalled, fatal log errors, disk critical

The ``problems`` list explains *why*, so a dashboard can show actionable text
rather than just a colour.

Every check has a stable key (see CHECK_KEYS); a key listed in `disabled`
(config `disabled_checks`) is silently skipped. All checks are on by default.
"""

OK = "ok"
DEGRADED = "degraded"
ERROR = "error"

_RANK = {OK: 0, DEGRADED: 1, ERROR: 2}

# Stable keys for every trigger, usable in config `disabled_checks`.
CHECK_KEYS = (
    "capture_down",       # capture process for the station not running
    "capture_stalled",    # no FF (night) / no frames (day) within the threshold
    "detection_stalled",  # capturing but no FTPdetectinfo/CALSTARS produced
    "timelapse_missing",  # a finished frame session's ffmpeg failed (no mp4)
    "timelapse_overdue",  # saving frames but no timelapse mp4 produced in ages
    "log_fatal",          # traceback / ImportError / .so / segfault in the log
    "log_warning",        # WARNING-level lines in the scanned log tail
    "too_many_stars",     # excessive star candidates while dark (washout / low limit)
    "watchdog",           # RMS WATCHDOG died/stale/Restarting event
    "disk_low",           # data partition low / critically low
    "upload_backlog",     # upload queue length over threshold
    "clock_unsynced",     # last summary reported clock not synchronized
    "clock_uncertainty",  # last summary clock error over threshold
    "dropped_frames",     # dropped frames in the last 10 min
    "oom",                # host OOM-killer fired
    "host_memory",        # host available memory low / critically low
    "udp_rcvbuf_errors",  # host UDP receive-buffer overflows climbing (udp RTSP)
)


def _worse(a, b):
    return a if _RANK[a] >= _RANK[b] else b


def _flagger(disabled):
    """Build a (flag, get_status, get_problems) trio sharing local state."""
    state = {"status": OK, "problems": []}

    def flag(level, key, message):
        if key in disabled:
            return
        state["status"] = _worse(state["status"], level)
        state["problems"].append(message)

    return flag, state


def evaluate(metrics, thresholds, disabled=()):
    """Return (status, problems) for a station's metrics dict."""
    flag, state = _flagger(disabled)

    # --- Capture process -------------------------------------------------
    if not metrics.get("capture_alive"):
        flag(ERROR, "capture_down", "Capture process not running")
        # Process down -> downstream freshness checks are moot.
        return state["status"], state["problems"]

    # --- Capture liveness (expect the right output for day/night) --------
    # expected_output comes from the sun + capture mode (RMS-faithful), not from
    # frame creation. Night -> FF must be fresh; continuous day -> frames must
    # be. "transition"/"idle" expect nothing.
    fits_age = metrics.get("newest_fits_age_s")
    frame_age = metrics.get("newest_frame_age_s")
    session_age = metrics.get("capture_session_age_s")
    expected = metrics.get("expected_output")     # ff/frames/idle/transition/None

    # Fallback when the station has no lat/lon: use the camera's own _d/_n frame
    # tag, then the session-active heuristic.
    if expected is None:
        frame_mode = metrics.get("frame_mode")
        if frame_mode == "night":
            expected = "ff"
        elif frame_mode == "day":
            expected = "frames"
        elif (session_age is not None
              and session_age <= thresholds.capture_active_window_s):
            expected = "ff"   # a session is running but no frame tag -> assume FF
        else:
            expected = "idle"

    if expected == "ff" and fits_age is not None and fits_age >= thresholds.output_fresh_error_s:
        flag(ERROR, "capture_stalled", "Night capture stalled: no FF for %.0fs" % fits_age)
    elif expected == "frames" and frame_age is not None and frame_age >= thresholds.output_fresh_error_s:
        flag(ERROR, "capture_stalled", "Daytime capture stalled: no frames for %.0fs" % frame_age)

    # --- Silent pipeline failure (the ".so missing" class) ---------------
    if (
        expected == "ff"
        and metrics.get("fits_count", 0) > 0
        and session_age is not None
        and session_age > thresholds.detection_grace_s
        and not metrics.get("ftpdetect_present")
        and not metrics.get("calstars_present")
    ):
        flag(ERROR, "detection_stalled",
             "Detection pipeline produced no output after %.0fs of capture" % session_age)

    # --- Timelapse mp4 not generated -------------------------------------
    # (a) ran but ffmpeg failed: a finished session's json exists, mp4 doesn't.
    tl_age = metrics.get("timelapse_session_age_s")
    if (tl_age is not None and tl_age >= thresholds.timelapse_grace_s
            and metrics.get("timelapse_mp4_present") is False):
        flag(DEGRADED, "timelapse_missing",
             "Timelapse mp4 not generated for the last frame session (%.0fs ago)" % tl_age)

    # (b) not generating at all: frames are actively being saved, but no mp4 has
    # appeared in ages (or none ever, despite frames piling up). Latitude-
    # independent -- a polar site that should make mp4s but doesn't is caught.
    if frame_age is not None and frame_age <= thresholds.output_fresh_error_s:
        newest_tl = metrics.get("newest_timelapse_age_s")
        frames_data = metrics.get("frames_data_age_s")
        overdue = None
        if newest_tl is not None:
            if newest_tl > thresholds.timelapse_max_age_s:
                overdue = newest_tl
        elif frames_data is not None and frames_data > thresholds.timelapse_max_age_s:
            overdue = frames_data  # frames accumulating but no mp4 ever produced
        if overdue is not None:
            flag(DEGRADED, "timelapse_overdue",
                 "No timelapse mp4 generated in %.1fh while saving frames" % (overdue / 3600.0))

    # --- Fatal log errors / tracebacks -----------------------------------
    if metrics.get("fatal_error_count"):
        last = metrics.get("last_error") or "see log"
        flag(ERROR, "log_fatal", "Fatal error in log (%dx): %s"
             % (metrics["fatal_error_count"], last))
    if metrics.get("warning_count", 0) >= thresholds.log_warning_warn:
        last = metrics.get("last_warning") or "see log"
        flag(DEGRADED, "log_warning", "Warning in log (%dx): %s"
             % (metrics["warning_count"], last))
    # Star limit too low: frames skipped while dark AND catalog-matched stars are
    # near the candidate cap (so real stars, not washout, are tripping it). A low
    # matched count means the excess candidates are washout/noise -> no alert.
    limit = metrics.get("too_many_stars_limit")
    matched = metrics.get("detected_stars_peak")
    if (metrics.get("too_many_stars_dark_count", 0) >= thresholds.too_many_stars_warn
            and limit and matched is not None
            and matched >= thresholds.too_many_stars_match_ratio * limit):
        flag(DEGRADED, "too_many_stars",
             "Star limit too low: %d matched stars vs cap %d, skipping %d dark frames "
             "-- raise max_star_candidates" % (matched, limit,
                                               metrics["too_many_stars_dark_count"]))
    if metrics.get("last_watchdog_event"):
        flag(DEGRADED, "watchdog", "Watchdog intervention: %s" % metrics["last_watchdog_event"])

    # --- Disk ------------------------------------------------------------
    disk_free = metrics.get("disk_free_gb")
    if disk_free is not None:
        if disk_free <= thresholds.disk_free_error_gb:
            flag(ERROR, "disk_low", "Disk critically low: %.1f GB free" % disk_free)
        elif disk_free <= thresholds.disk_free_warn_gb:
            flag(DEGRADED, "disk_low", "Disk low: %.1f GB free" % disk_free)

    # --- Upload backlog (only meaningful when uploads are queued) --------
    queue = metrics.get("upload_queue_len", 0)
    if queue >= thresholds.upload_queue_warn:
        flag(DEGRADED, "upload_backlog", "Upload backlog: %d files queued" % queue)

    # --- Time sync (from latest observation summary) ---------------------
    summary = metrics.get("summary") or {}
    if str(summary.get("clock_synchronized")).lower() == "false":
        flag(DEGRADED, "clock_unsynced", "Clock not synchronized at last summary")
    clock_err = summary.get("clock_error_uncertainty_ms")
    if clock_err is not None:
        try:
            if float(clock_err) > thresholds.clock_error_warn_ms:
                flag(DEGRADED, "clock_uncertainty", "Clock uncertainty %.0f ms" % float(clock_err))
        except (TypeError, ValueError):
            pass

    # --- Dropped frames (a few are normal; warn only past the threshold) -
    dropped = metrics.get("dropped_frames_10min") or 0
    if dropped >= thresholds.dropped_frames_warn:
        flag(DEGRADED, "dropped_frames",
             "Dropped %d frames in last 10 min" % dropped)

    return state["status"], state["problems"]


def evaluate_host(metrics, thresholds, disabled=()):
    """Return (status, problems) for host-wide OS metrics (memory, OOM)."""
    flag, state = _flagger(disabled)

    # OOM-killer activity is always significant; killing a python (RMS) process
    # is treated as an error, anything else as degraded.
    if metrics.get("oom_kill_count"):
        victim = metrics.get("last_oom_victim") or "?"
        level = ERROR if "python" in str(victim).lower() else DEGRADED
        flag(level, "oom", "OOM-killer fired %dx (last victim: %s)"
             % (metrics["oom_kill_count"], victim))

    avail = metrics.get("mem_available_mb")
    if avail is not None:
        if avail <= thresholds.mem_available_error_mb:
            flag(ERROR, "host_memory", "Host memory critically low: %d MB available" % avail)
        elif avail <= thresholds.mem_available_warn_mb:
            flag(DEGRADED, "host_memory", "Host memory low: %d MB available" % avail)

    # UDP receive-buffer overflows climbing (kernel-dropped RTSP datagrams; the
    # host-level analogue of dropped frames). Rate-based: a null rate (first
    # cycle / counter reset) is not flagged. Only present when a station is UDP.
    # Strictly-greater so the default threshold of 0 means "any increase"; a
    # zero rate (no growth this cycle, the common case) never fires.
    rate = metrics.get("udp_rcvbuf_errors_per_min")
    if rate is not None and rate > thresholds.udp_rcvbuf_errors_per_min_warn:
        flag(DEGRADED, "udp_rcvbuf_errors",
             "UDP RcvbufErrors climbing: %.1f/min (%s total, %.4f%% of datagrams)"
             % (rate, metrics.get("udp_rcvbuf_errors"),
                metrics.get("udp_rcvbuf_error_pct") or 0.0))

    return state["status"], state["problems"]


def build_state(metrics, thresholds, host_name, timestamp, disabled=()):
    """Assemble the published JSON state for one station."""
    status, problems = evaluate(metrics, thresholds, disabled)
    state = dict(metrics)
    state["status"] = status
    state["problems"] = problems
    state["host"] = host_name
    state["timestamp"] = timestamp
    return state


def build_host_state(metrics, thresholds, host_name, timestamp, disabled=()):
    status, problems = evaluate_host(metrics, thresholds, disabled)
    state = dict(metrics)
    state["status"] = status
    state["problems"] = problems
    state["host"] = host_name
    state["timestamp"] = timestamp
    return state
