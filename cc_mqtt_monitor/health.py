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

    # --- Capture freshness (only while a session is active) --------------
    session_age = metrics.get("capture_session_age_s")
    fits_age = metrics.get("newest_fits_age_s")
    session_active = (
        session_age is not None and session_age <= thresholds.capture_active_window_s
    )
    if session_active and fits_age is not None:
        if fits_age >= thresholds.fits_fresh_error_s:
            flag(ERROR, "Capture stalled: newest FITS is %.0fs old" % fits_age)
        elif fits_age >= thresholds.fits_fresh_warn_s:
            flag(DEGRADED, "Capture lagging: newest FITS is %.0fs old" % fits_age)

    # --- Silent pipeline failure (the ".so missing" class) ---------------
    # Frames are being written but no detection output has appeared after the
    # grace period -> a stage (detection/calibration) is broken even though
    # capture looks healthy.
    if (
        session_active
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
