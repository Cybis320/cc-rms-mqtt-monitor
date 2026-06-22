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
    "platepar_mismatch",  # config resolution != platepar -> RMS drops the platepar
    "timelapse_missing",  # a finished frame session's ffmpeg failed (no mp4)
    "timelapse_overdue",  # saving frames but no timelapse mp4 produced in ages
    "log_fatal",          # traceback / ImportError / .so / segfault in the log
    "log_warning",        # WARNING-level lines in the scanned log tail
    "watchdog",           # RMS WATCHDOG died/stale/Restarting event
    "disk_low",           # data partition low / critically low
    "upload_backlog",     # upload queue length over threshold
    "clock_unsynced",     # last summary reported clock not synchronized
    "clock_uncertainty",  # last summary clock error over threshold
    "dropped_frames",     # dropped frames in the last 10 min
    "oom",                # host OOM-killer fired
    "mem_pressure",       # host memory pressure (PSI) -- the pre-OOM signal
    "udp_rcvbuf_errors",  # host UDP receive-buffer overflows climbing (udp RTSP)
    "nic_errors",         # host NIC RX errors climbing (wire/link)
    "disk_errors",        # host kernel disk I/O errors / read-only remount
)


# Drop-cause labels (also the public `drop_cause` values on a station record).
CAUSE_BACKPRESSURE = "cpu/io back-pressure"
CAUSE_UDP_BUFFER = "network: kernel UDP buffer"
CAUSE_NIC = "network: NIC/wire"
CAUSE_IP_FRAG = "network: IP fragmentation"
CAUSE_LINK_LOSS = "network: link packet loss"
CAUSE_CAMERA_BW = "camera/link bandwidth"
CAUSE_UNCERTAIN = "uncertain"


def _num(metrics, key):
    """A metric as float, or None if absent/non-numeric (defensive: a collector
    that couldn't read a signal leaves it null, which must not count as 0)."""
    val = metrics.get(key)
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _hot(value, threshold):
    return value is not None and value > threshold


def classify_drops(metrics, host_metrics, thresholds):
    """Attribute a dropped-frame burst to a probable cause by elimination.

    This is the by-hand CAWEC4 logic encoded: walk the stack cheapest/strongest
    first -- back-pressure (the consumer can't keep up), then each network layer
    that has its OWN positive counter (kernel UDP buffer, NIC, IP fragmentation),
    then in-pipeline decoder corruption with a clean host (the camera/link-burst
    signature), else uncertain. Host signals are host-wide; the per-station
    pipeline signals disambiguate which camera. Returns drop_cause/-confidence/
    -detail, all None when there's no drop to explain.

    `host_metrics` may be empty (e.g. a host with no consenting stations); then
    only per-station signals are used and confidence is reduced accordingly.
    Probe results (probe_ping_loss_pct, probe_keyframe_peak_kb), when present,
    sharpen the verdict but are never required.
    """
    none = {"drop_cause": None, "drop_confidence": None, "drop_detail": None}
    dropped = metrics.get("dropped_frames_10min")
    if not dropped or dropped < thresholds.dropped_frames_warn:
        return none

    h = host_metrics or {}

    def verdict(cause, confidence, detail):
        return {"drop_cause": cause, "drop_confidence": confidence,
                "drop_detail": detail}

    # 1) CPU / I-O back-pressure: the consumer fell behind and the appsink buffer
    #    SPIKED in the lead-up to the drop. We key on the recent MAX fill, not the
    #    fill at the drop line (which has usually recovered to baseline by the time
    #    the 10-min count logs). CPU% is deliberately NOT a trigger -- a busy Pi
    #    runs hot whether or not it drops, so the spike is the discriminator; CPU/
    #    iowait appear only as context to hint cpu- vs disk-bound.
    #    BUT: every fresh (re)connection produces a brief startup buffer-fill
    #    spike, so a spike riding WITH reconnect churn is that transient, not
    #    back-pressure -- only trust the spike on a stable (non-reconnecting)
    #    stream, else fall through to the camera/link verdict below.
    spike = _num(metrics, "buffer_fill_max_recent")
    reconnects = metrics.get("pipeline_reconnects") or 0
    stable = reconnects < thresholds.pipeline_reconnects_warn
    if _hot(spike, thresholds.buffer_fill_spike_pct) and stable:
        ctx = []
        cpu_busy = _num(h, "cpu_busy_pct")
        iowait = _num(h, "cpu_iowait_pct")
        cpu_proc = _num(metrics, "capture_cpu_pct")
        if cpu_busy is not None:
            ctx.append("host cpu %.0f%%" % cpu_busy)
        if iowait is not None:
            ctx.append("iowait %.0f%%" % iowait)
        if cpu_proc is not None:
            ctx.append("capture %.0f%%" % cpu_proc)
        detail = "buffer fill spiked to %.0f%%" % spike
        if ctx:
            detail += " (" + ", ".join(ctx) + ")"
        return verdict(CAUSE_BACKPRESSURE, "high", detail)

    # 2) Network layers with their own positive counter (host-wide rates).
    if _hot(_num(h, "udp_rcvbuf_errors_per_min"), thresholds.udp_rcvbuf_errors_per_min_warn):
        return verdict(CAUSE_UDP_BUFFER, "high",
                       "UDP RcvbufErrors +%.0f/min (raise rmem_max)"
                       % _num(h, "udp_rcvbuf_errors_per_min"))
    if _hot(_num(h, "nic_rx_errors_per_min"), thresholds.nic_rx_errors_per_min_warn):
        return verdict(CAUSE_NIC, "high", "NIC RX errors +%.0f/min (cable/duplex/port)"
                       % _num(h, "nic_rx_errors_per_min"))
    if _hot(_num(h, "ip_reasm_fails_per_min"), thresholds.ip_reasm_fails_per_min_warn):
        return verdict(CAUSE_IP_FRAG, "high", "IP reasm fails +%.0f/min"
                       % _num(h, "ip_reasm_fails_per_min"))

    # 3) A confirming probe, if one has been attached, is decisive.
    ping_loss = _num(metrics, "probe_ping_loss_pct")
    if _hot(ping_loss, thresholds.ping_loss_warn_pct):
        return verdict(CAUSE_LINK_LOSS, "high", "ping loss %.0f%% to camera" % ping_loss)

    # 4) Camera/link: either the stream keeps DROPPING (reconnect loop -- the
    #    camera/connection won't stay up) or it stays up but arrives DAMAGED
    #    (decoder corruption from packets lost upstream, the microburst case),
    #    with the host clean. Reconnect churn vs decoder errors tells them apart;
    #    delivered bitrate / a probed keyframe peak adds the bandwidth detail.
    decoder_err = metrics.get("decoder_errors") or 0
    host_known = any(_num(h, k) is not None for k in
                     ("cpu_busy_pct", "nic_rx_errors_per_min"))
    if decoder_err >= thresholds.decoder_errors_warn or reconnects >= thresholds.pipeline_reconnects_warn:
        detail = []
        if reconnects >= thresholds.pipeline_reconnects_warn:
            detail.append("%d reconnects (camera dropping the stream)" % reconnects)
        elif reconnects:
            detail.append("%d reconnects" % reconnects)
        if decoder_err:
            detail.append("%d decoder errors" % decoder_err)
        peak = _num(metrics, "probe_keyframe_peak_kb")
        mbps = _num(metrics, "probe_stream_mbps") or _num(metrics, "stream_mbps")
        if peak is not None:
            detail.append("keyframe peak %.0f KB" % peak)
        if mbps is not None:
            detail.append("%.1f Mbps" % mbps)
        if host_known:
            detail.append("host clean")
        # Confidence is higher once a probe corroborated it (peak/ping present).
        conf = "high" if peak is not None else ("medium" if host_known else "low")
        return verdict(CAUSE_CAMERA_BW, conf, "; ".join(detail))

    # 5) Nothing positive yet -- real drops, host looks clean, no decoder symptom
    #    captured in the tail. This is exactly when escalating to a probe pays off.
    return verdict(CAUSE_UNCERTAIN, "low",
                   "drops with no host signal; probe to confirm camera/link")


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

    # --- Platepar resolution mismatch (silent astrometry killer) ---------
    # If config width/height != platepar X_res/Y_res, RMS discards the platepar
    # entirely -> the night's detections get NO astrometric calibration (data is
    # captured but scientifically unusable). The station otherwise looks healthy.
    if metrics.get("platepar_res_mismatch"):
        flag(ERROR, "platepar_mismatch",
             "Platepar resolution %sx%s != config %sx%s -- RMS discards the platepar, "
             "no astrometry" % (metrics.get("platepar_x_res"), metrics.get("platepar_y_res"),
                                metrics.get("config_width"), metrics.get("config_height")))

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
    # The attribution (drop_cause/-detail) is computed in build_state and merged
    # into metrics, so the alert says *why*, not just that frames dropped.
    dropped = metrics.get("dropped_frames_10min") or 0
    if dropped >= thresholds.dropped_frames_warn:
        msg = "Dropped %d frames in last 10 min" % dropped
        cause = metrics.get("drop_cause")
        if cause:
            detail = metrics.get("drop_detail")
            msg += " -- likely %s%s" % (cause, (" (%s)" % detail) if detail else "")
        flag(DEGRADED, "dropped_frames", msg)

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

    # Memory pressure (PSI) -- the actual pre-OOM signal. The kernel OOM-killer
    # fires on allocation-failure-after-reclaim, not at a fixed free-MB line, so
    # an absolute MemAvailable threshold both false-alarms on a small (2 GB Pi)
    # host and can miss a fast spike on a big one. `full avgN` from
    # /proc/pressure/memory is the % of time EVERY task was stalled on memory
    # (the box thrashing in reclaim) -- a stall ratio, so it means the same on a
    # Pi and a 32 GB box with no per-host tuning. avg10 reacts fast (warn on the
    # onset); sustained avg60 is the serious, OOM-is-near signal (error).
    full10 = metrics.get("mem_psi_full_avg10")
    full60 = metrics.get("mem_psi_full_avg60")
    avail = metrics.get("mem_available_mb")
    avail_txt = (", %d MB available" % avail) if avail is not None else ""
    if full60 is not None and full60 > thresholds.mem_psi_full_avg60_error:
        flag(ERROR, "mem_pressure",
             "Sustained memory pressure: %.1f%% full-stall over 60s%s (OOM risk)"
             % (full60, avail_txt))
    elif full10 is not None and full10 > thresholds.mem_psi_full_avg10_warn:
        flag(DEGRADED, "mem_pressure",
             "Memory pressure: %.1f%% full-stall over 10s%s" % (full10, avail_txt))

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

    # NIC RX errors climbing: the wire/link itself shedding packets (a cable,
    # duplex mismatch, or dying port) -- distinct from a full socket buffer.
    nic_rate = metrics.get("nic_rx_errors_per_min")
    if nic_rate is not None and nic_rate > thresholds.nic_rx_errors_per_min_warn:
        flag(DEGRADED, "nic_errors",
             "NIC RX errors climbing: %.1f/min (%s total)"
             % (nic_rate, metrics.get("nic_rx_errors")))

    # Disk/storage failure from the kernel log -- the medium-agnostic "disk
    # failing" canary. Unlike iowait (chronically high on a healthy-but-slow SD
    # card, so it can't tell slow from failing), these are actual I/O errors. A
    # filesystem remounted read-only means the disk has effectively given up.
    if metrics.get("disk_fs_readonly"):
        flag(ERROR, "disk_errors", "Filesystem remounted READ-ONLY (disk failing): %s"
             % (metrics.get("last_disk_error") or "see kernel log"))
    elif metrics.get("disk_error_count"):
        flag(DEGRADED, "disk_errors", "Kernel disk I/O errors (%dx): %s"
             % (metrics["disk_error_count"], metrics.get("last_disk_error") or "see kernel log"))

    return state["status"], state["problems"]


def build_state(metrics, thresholds, host_name, timestamp, disabled=(), host_metrics=None):
    """Assemble the published JSON state for one station.

    `host_metrics` (the same cycle's host record) lets the drop classifier use
    host-wide signals (CPU, NIC, UDP, reassembly) to attribute a drop; it's
    optional, so callers without a host record still get per-station attribution.
    """
    metrics = dict(metrics)
    metrics.update(classify_drops(metrics, host_metrics, thresholds))
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
