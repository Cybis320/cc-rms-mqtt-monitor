"""Turn raw per-station metrics into a status verdict.

Status levels (worst wins):

    ok        -- everything nominal
    degraded  -- a non-fatal concern (warnings, backlog, stale code)
    error     -- capture down, pipeline stalled, fatal log errors, disk critical

The ``problems`` list explains *why*, so a dashboard can show actionable text
rather than just a colour. This is also where the silent-failure heuristics
live: "alive and capturing but producing no detection output".
"""

OK = "ok"
DEGRADED = "degraded"
ERROR = "error"

_RANK = {OK: 0, DEGRADED: 1, ERROR: 2}


def _worse(a, b):
    return a if _RANK[a] >= _RANK[b] else b


def evaluate(metrics, thresholds):
    """Return (status, problems) for a station's metrics dict."""
    status = OK
    problems = []

    def flag(level, message):
        nonlocal status
        status = _worse(status, level)
        problems.append(message)

    # --- Capture process -------------------------------------------------
    if not metrics.get("capture_alive"):
        flag(ERROR, "Capture process not running")
        # If the process is down, downstream freshness checks are moot.
        return status, problems

    # --- Capture liveness (expect the right output for the camera's mode) --
    # RMS tags each saved frame _d (day) / _n (night), so frame_mode is the
    # camera's OWN current mode -- no need to predict day/night from the sun
    # (and so no false alarm at the boundary). At night the FF files are the
    # product and must stay fresh; by day only frame images are produced. The
    # generous threshold rides through the mode switch, which pauses output
    # briefly while the camera reconfigures.
    fits_age = metrics.get("newest_fits_age_s")
    frame_age = metrics.get("newest_frame_age_s")
    frame_mode = metrics.get("frame_mode")          # "day" / "night" / None
    session_age = metrics.get("capture_session_age_s")

    if frame_mode == "night":
        stalled_age, what = fits_age, "Night capture stalled: no FF for %.0fs"
    elif frame_mode == "day":
        stalled_age, what = frame_age, "Daytime capture stalled: no frames for %.0fs"
    else:
        # No frame tag (save_frames off, or none yet): check FF, but only while
        # a capture session is actually in progress (its captured dir is recent),
        # so a legitimately idle daytime station never alarms.
        active = (session_age is not None
                  and session_age <= thresholds.capture_active_window_s)
        stalled_age = fits_age if active else None
        what = "Capture stalled: no FF for %.0fs"

    if stalled_age is not None and stalled_age >= thresholds.output_fresh_error_s:
        flag(ERROR, what % stalled_age)

    # --- Silent pipeline failure (the ".so missing" class) ---------------
    # FF files are being produced (so detection should be running) but no
    # detection output has appeared after the grace period -> a stage is broken
    # even though capture looks healthy. "FF recently produced" stands in for
    # "this is a detection-eligible session" without predicting the sun.
    ff_active = fits_age is not None and fits_age <= thresholds.output_fresh_error_s
    if (
        ff_active
        and metrics.get("fits_count", 0) > 0
        and session_age is not None
        and session_age > thresholds.detection_grace_s
        and not metrics.get("ftpdetect_present")
        and not metrics.get("calstars_present")
    ):
        flag(ERROR, "Detection pipeline produced no output after %.0fs of capture"
             % session_age)

    # --- Fatal log errors / tracebacks -----------------------------------
    if metrics.get("fatal_error_count"):
        last = metrics.get("last_error") or "see log"
        flag(ERROR, "Fatal error in log (%dx): %s"
             % (metrics["fatal_error_count"], last))
    if metrics.get("last_watchdog_event"):
        flag(DEGRADED, "Watchdog intervention: %s" % metrics["last_watchdog_event"])

    # --- Disk ------------------------------------------------------------
    disk_free = metrics.get("disk_free_gb")
    if disk_free is not None:
        if disk_free <= thresholds.disk_free_error_gb:
            flag(ERROR, "Disk critically low: %.1f GB free" % disk_free)
        elif disk_free <= thresholds.disk_free_warn_gb:
            flag(DEGRADED, "Disk low: %.1f GB free" % disk_free)

    # --- Upload backlog --------------------------------------------------
    queue = metrics.get("upload_queue_len", 0)
    if queue >= thresholds.upload_queue_warn:
        flag(DEGRADED, "Upload backlog: %d files queued" % queue)

    # --- Time sync (from latest observation summary) ---------------------
    summary = metrics.get("summary") or {}
    if str(summary.get("clock_synchronized")).lower() == "false":
        flag(DEGRADED, "Clock not synchronized at last summary")
    clock_err = summary.get("clock_error_uncertainty_ms")
    if clock_err is not None:
        try:
            if float(clock_err) > thresholds.clock_error_warn_ms:
                flag(DEGRADED, "Clock uncertainty %.0f ms" % float(clock_err))
        except (TypeError, ValueError):
            pass

    # --- Dropped frames --------------------------------------------------
    if metrics.get("dropped_frames_10min"):
        flag(DEGRADED, "Dropped %d frames in last 10 min"
             % metrics["dropped_frames_10min"])

    return status, problems


def build_state(metrics, thresholds, host_name, timestamp):
    """Assemble the published JSON state for one station."""
    status, problems = evaluate(metrics, thresholds)
    state = dict(metrics)
    state["status"] = status
    state["problems"] = problems
    state["host"] = host_name
    state["timestamp"] = timestamp
    return state


def evaluate_host(metrics, thresholds):
    """Return (status, problems) for host-wide OS metrics (memory, OOM)."""
    status = OK
    problems = []

    def flag(level, message):
        nonlocal status
        status = _worse(status, level)
        problems.append(message)

    # OOM-killer activity is always significant; killing a python (RMS) process
    # is treated as an error, anything else as degraded.
    if metrics.get("oom_kill_count"):
        victim = metrics.get("last_oom_victim") or "?"
        level = ERROR if "python" in str(victim).lower() else DEGRADED
        flag(level, "OOM-killer fired %dx (last victim: %s)"
             % (metrics["oom_kill_count"], victim))

    avail = metrics.get("mem_available_mb")
    if avail is not None:
        if avail <= thresholds.mem_available_error_mb:
            flag(ERROR, "Host memory critically low: %d MB available" % avail)
        elif avail <= thresholds.mem_available_warn_mb:
            flag(DEGRADED, "Host memory low: %d MB available" % avail)

    return status, problems


def build_host_state(metrics, thresholds, host_name, timestamp):
    status, problems = evaluate_host(metrics, thresholds)
    state = dict(metrics)
    state["status"] = status
    state["problems"] = problems
    state["host"] = host_name
    state["timestamp"] = timestamp
    return state
