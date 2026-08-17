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
    "camera_unreachable", # camera not pingable for a sustained window (root cause)
    "capture_down",       # capture process for the station not running
    "capture_duplicate",  # more than one StartCapture instance for one camera
    "data_unreadable",    # process alive but data_dir not readable (perms/other user)
    "capture_stalled",    # no FF (night) / no frames (day) within the threshold
    "detection_stalled",  # capturing but no FTPdetectinfo/CALSTARS produced
    "platepar_mismatch",  # config resolution != platepar -> RMS drops the platepar
    "config_fov_mismatch", # config fov_w can't solve the real FOV (astrometry.net)
    "backend_fallback",   # configured gst but capture fell back to OpenCV (cv2)
    "timelapse_missing",  # a finished frame session's ffmpeg failed (no mp4)
    "timelapse_overdue",  # saving frames but no timelapse mp4 produced in ages
    "log_fatal",          # traceback / ImportError / .so / segfault in the log
    "log_warning",        # WARNING-level lines in the scanned log tail
    "watchdog",           # RMS WATCHDOG died/stale/Restarting event
    "disk_low",           # data partition low / critically low
    "upload_backlog",     # upload queue length over threshold
    "clock_unsynced",     # last summary reported clock not synchronized
    "clock_uncertainty",  # last summary clock error over threshold
    "dropped_frames",     # per-night dropped_frame_rate over the universal threshold
    "dropped_frames_live",  # catastrophic LIVE frame loss (mid-night stream failure)
    "oom",                # host OOM-killer fired
    "mem_pressure",       # host memory pressure (PSI) -- the pre-OOM signal
    "udp_rcvbuf_errors",  # host UDP receive-buffer overflows climbing (udp RTSP)
    "nic_errors",         # host NIC RX errors climbing (wire/link)
    "disk_errors",        # host kernel disk I/O errors / read-only remount
    "update_blocked",     # the monitor's own auto-update is stuck on this host
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


def _fmt_dur(secs):
    """Human-friendly duration for a problem string ('7m', '2h 5m')."""
    if secs is None:
        return "a while"
    secs = int(secs)
    if secs < 60:
        return "%ds" % secs
    if secs < 3600:
        return "%dm" % (secs // 60)
    return "%dh %dm" % (secs // 3600, (secs % 3600) // 60)


def _resolve_expected(metrics, thresholds):
    """The output RMS should be producing right now: 'ff' | 'frames' | 'idle'.
    Prefers the sun+mode value; falls back to the camera's frame tag then the
    session-active heuristic (mirrors the historical inline logic in evaluate)."""
    expected = metrics.get("expected_output")
    if expected is not None:
        return expected
    frame_mode = metrics.get("frame_mode")
    if frame_mode == "night":
        return "ff"
    if frame_mode == "day":
        return "frames"
    session_age = metrics.get("capture_session_age_s")
    if session_age is not None and session_age <= thresholds.capture_active_window_s:
        return "ff"
    return "idle"


def output_stalled(metrics, thresholds):
    """True when RMS should be producing output (FF at night / frames by day) but
    isn't -- the `capture_stalled` predicate factored out so the monitor loop can
    stall-gate the camera-reachability ping on it (honouring the same post-restart
    settling grace). An idle pipeline (nothing expected) is never 'stalled'.

    Two ways to be stalled:
      * output exists but has gone stale (age >= output_fresh_error_s), or
      * NO output has ever been produced while it's expected and RMS has been in
        the producing session long enough to have made some. This second case is
        what catches a fresh / never-deployed camera that never came up (it has no
        prior FF to go stale), so the ping still fires and an unreachable one
        collapses to a clean 'camera not pingable' instead of watchdog noise.
    """
    capture_age = metrics.get("capture_age_s")
    restart_grace = (thresholds.capture_restart_grace_s
                     + (metrics.get("capture_wait_seconds") or 0))
    if capture_age is not None and capture_age < restart_grace:
        return False
    expected = _resolve_expected(metrics, thresholds)
    if expected == "ff":
        age = metrics.get("newest_fits_age_s")
    elif expected == "frames":
        age = metrics.get("newest_frame_age_s")
    else:
        return False
    if age is not None:
        return age >= thresholds.output_fresh_error_s
    # No output at all: a stall only once it's been expected-and-producing long
    # enough to have made something -- so a just-started session (or a cam still
    # in its settling grace) isn't falsely flagged. Session age preferred; else
    # process age. No time evidence at all => don't claim a stall.
    elapsed = metrics.get("capture_session_age_s")
    if elapsed is None:
        elapsed = capture_age
    return elapsed is not None and elapsed >= thresholds.output_fresh_error_s


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
    #    was ALREADY backing up when frames started dropping. We key on the peak
    #    fill STRICTLY BEFORE the drop (buffer_fill_max_leadup), never the fill at
    #    the drop line: that one is concurrent with the event and proves nothing.
    #    CPU% is deliberately NOT a trigger -- a busy Pi runs hot whether or not it
    #    drops, so the lead-up spike is the discriminator; CPU/iowait appear only
    #    as context to hint cpu- vs disk-bound.
    #    BUT: EVERY fresh (re)connection produces a startup buffer-fill spike, so a
    #    single reconnect anywhere in the scanned window is enough to make the
    #    spike untrustworthy -- this is NOT the pipeline_reconnects_warn "churn is
    #    a problem" question, it's "can this spike be believed at all". Any
    #    reconnect => fall through to the camera/link verdict below.
    spike = _num(metrics, "buffer_fill_max_leadup")
    reconnects = metrics.get("pipeline_reconnects") or 0
    stable = reconnects == 0
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
    # ANY reconnect explains a drop burst: the stream went down, so the frames in
    # that gap are simply gone. Sustained churn (>= pipeline_reconnects_warn) says
    # the camera won't stay up at all; a single one is a one-off stream drop. Both
    # are camera/link, not host back-pressure.
    if decoder_err >= thresholds.decoder_errors_warn or reconnects:
        detail = []
        if reconnects >= thresholds.pipeline_reconnects_warn:
            detail.append("%d reconnects (camera dropping the stream)" % reconnects)
        elif reconnects:
            detail.append("%d reconnect%s (stream dropped and rebuilt)"
                          % (reconnects, "" if reconnects == 1 else "s"))
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

    # 5) Nothing positive from the cheap signals -- real drops, host clean, no
    #    decoder/reconnect symptom in the tail. If a probe has already run it has
    #    EXCLUDED host, link loss and bandwidth (checked above), so the frames are
    #    being lost at the camera itself; otherwise a probe is what confirms that.
    if "probe_ping_note" in metrics:   # a probe was attached (run_probe ran)
        detail = ["probed: no host/network/bandwidth cause"]
        mbps = _num(metrics, "probe_stream_mbps") or _num(metrics, "stream_mbps")
        if mbps is not None:
            detail.append("steady %.1f Mbps" % mbps)
        detail.append("frames lost at the camera -- check the camera itself")
        return verdict(CAUSE_UNCERTAIN, "medium", "; ".join(detail))
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

    # --- Camera unreachable (root cause; collapses the whole cascade) ----
    # When the camera itself hasn't answered a ping for a sustained window (the
    # monitor loop stall-gates and times this, setting camera_standby), the fault
    # is the wire/power/camera. NOTHING downstream matters -- not the stall, not
    # the missing detections/drops/watchdog, not even whether StartCapture is
    # still running -- they all follow from the camera being gone. Report the one
    # root cause and stop. Done here in the monitor (not the bridge) so EVERY
    # consumer, the dashboard included, sees the collapsed record rather than the
    # cascade of downstream symptoms.
    if metrics.get("camera_standby"):
        # Deliberately no camera IP in the text -- this is published to the open
        # feed; the station_id already identifies which camera.
        flag(ERROR, "camera_unreachable",
             "Camera not pingable for %s -- capture/detection checks suppressed "
             "while it's unreachable" % _fmt_dur(metrics.get("camera_unreachable_s")))
        return state["status"], state["problems"]

    # --- Capture process -------------------------------------------------
    # Duplicate StartCapture instances for a SINGLE camera. There is exactly one legitimate
    # instance per station, so any extra is a fault no matter which chain produced it --
    # several different failures end the same way: an external supervisor respawns
    # StartCapture with no already-running guard, and the clones (a full capture tree each,
    # ~700 MB) pile up until the OOM-killer takes the box down. Detecting the STATE rather
    # than any one trigger catches all of those chains. Checked before the liveness bail-out
    # below so it is reported even while the station still looks "up".
    inst = metrics.get("capture_instances")
    if isinstance(inst, int) and inst > 1:
        rss = metrics.get("total_rss_mb")
        flag(ERROR, "capture_duplicate",
             "%d StartCapture instances running for this one camera%s -- they will keep "
             "multiplying and exhaust memory; kill the extras and check what is respawning it"
             % (inst, (" (%.0f MB total)" % rss) if rss else ""))

    if not metrics.get("capture_alive"):
        flag(ERROR, "capture_down", "Capture process not running")
        # Process down -> downstream freshness checks are moot.
        return state["status"], state["problems"]

    # --- Data not readable (multi-user permission denial) ----------------
    # The process is alive (seen via /proc, which is cross-user), but we can't
    # read its data_dir -- and the ~/RMS_data fallback wasn't readable either. So
    # logs/FF/detections are all blank; report THAT, not a phantom stall, and skip
    # the data-dependent checks below (they'd false-fire on empty data).
    if metrics.get("data_dir_readable") is False:
        flag(DEGRADED, "data_unreadable",
             "Capture is running but the monitor can't read its data_dir "
             "(permission denied) -- RMS likely runs as a different user; make the "
             "data tree readable to the monitor's user (shared group + g+rX), or "
             "expose a readable copy at ~/RMS_data/<id>")
        return state["status"], state["problems"]

    # --- Capture liveness (expect the right output for day/night) --------
    # expected_output comes from the sun + capture mode (RMS-faithful), not from
    # frame creation. Night -> FF must be fresh; continuous day -> frames must
    # be. "transition"/"idle" expect nothing.
    fits_age = metrics.get("newest_fits_age_s")
    frame_age = metrics.get("newest_frame_age_s")
    session_age = metrics.get("capture_session_age_s")
    expected = _resolve_expected(metrics, thresholds)  # ff/frames/idle/transition

    # Settling grace: a just-(re)started capture has no fresh output yet, and its
    # newest FF/frame on disk is from before the restart (age spans the downtime).
    # GRMSUpdater restarts the cameras on a host in a stagger, so the tail ones
    # come back minutes apart -- give each one a grace from ITS OWN process start
    # (plus RMS's capture_wait_seconds pre-capture sleep) before a stale age may
    # count as a stall. capture_age None (unknown) => no suppression (fail toward
    # alerting). A genuinely stalled long-running capture has a large age and is
    # unaffected.
    capture_age = metrics.get("capture_age_s")
    restart_grace = (thresholds.capture_restart_grace_s
                     + (metrics.get("capture_wait_seconds") or 0))
    settling = capture_age is not None and capture_age < restart_grace

    if (expected == "ff" and fits_age is not None
            and fits_age >= thresholds.output_fresh_error_s and not settling):
        flag(ERROR, "capture_stalled", "Night capture stalled: no FF for %.0fs" % fits_age)
    elif (expected == "frames" and frame_age is not None
            and frame_age >= thresholds.output_fresh_error_s and not settling):
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

    # --- Capture backend fell back to cv2 ---------------------------------
    # Configured for GStreamer but the log shows it running OpenCV: gst failed to
    # start. The station looks alive; only the backend tells you it's not on the
    # configured path.
    if (metrics.get("media_backend") == "gst"
            and metrics.get("capture_backend") == "cv2"):
        flag(DEGRADED, "backend_fallback",
             "Capture fell back to OpenCV (cv2): configured media_backend=gst but "
             "GStreamer didn't start")

    # --- Config FOV outside astrometry.net's solve range (latent) --------
    # config.fov_w is the scale hint for auto-calibration (searches [0.75x,1.5x]).
    # If the real FOV (platepar fov_h) is outside that, a fresh plate-solve fails
    # -> no recalibration. Degraded: an existing platepar still works for now.
    if metrics.get("config_fov_mismatch"):
        flag(DEGRADED, "config_fov_mismatch",
             "Config fov_w=%s deg can't solve the actual FOV (~%s deg): outside "
             "astrometry.net's 0.75-1.5x range -- a fresh auto-calibration would fail"
             % (metrics.get("config_fov_w"), metrics.get("platepar_fov_h")))

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
             "Frames timelapse (FramesFiles/*_frames_timelapse.mp4) not generated "
             "for the last frame session (%.0fs ago)" % tl_age)

    # (b) not generating at all: frames are actively being saved, but no mp4 has
    # appeared in ages (or none ever, despite frames piling up). Latitude-
    # independent -- a polar site that should make mp4s but doesn't is caught.
    if frame_age is not None and frame_age <= thresholds.output_fresh_error_s:
        newest_tl = metrics.get("newest_timelapse_age_s")
        frames_data = metrics.get("frames_data_age_s")
        overdue = None
        # Only "overdue" if the pipeline is genuinely failing to produce an mp4:
        #   - frames are being saved but NO timelapse mp4 exists at all, or
        #   - the newest COMPLETED session failed to produce its mp4.
        # A present-but-old newest mp4 (timelapse_mp4_present is True) just means no
        # new session has completed recently (skipped night / long or gapped
        # session) -- NOT a fault. Firing on wall-clock age alone false-positives on
        # healthy stations (observed 2026-07-08: 9 healthy AU + USC0F cams, all with
        # mp4 present).
        if newest_tl is None:
            if frames_data is not None and frames_data > thresholds.timelapse_max_age_s:
                overdue = frames_data
        elif metrics.get("timelapse_mp4_present") is False:
            if newest_tl > thresholds.timelapse_max_age_s:
                overdue = newest_tl
        if overdue is not None:
            flag(DEGRADED, "timelapse_overdue",
                 "No frames timelapse (FramesFiles/*_frames_timelapse.mp4) generated "
                 "in %.1fh while saving frames" % (overdue / 3600.0))

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

    # --- Dropped frames --------------------------------------------------
    # UNIVERSAL standard on RMS's per-night dropped_frame_rate (%): essentially
    # every station drops the odd frame (a normal blip), so the old per-cycle
    # raw-count alert (dropped_frames_10min >= 10) was ~62 alerts/day of mostly
    # noise. Instead alert once/night when the whole night's drop rate exceeds the
    # threshold -- the actionable "this station's stream was degraded" signal, which
    # also feeds the weekly operator digest. drop_cause (from build_state) says *why*.
    summary = metrics.get("summary") or {}
    try:
        rate = float(summary.get("dropped_frame_rate"))
    except (TypeError, ValueError):
        rate = None
    if rate is not None and rate > thresholds.dropped_frame_rate_warn_pct:
        cause = metrics.get("drop_cause")
        why = (" -- likely %s" % cause) if cause else ""
        flag(DEGRADED, "dropped_frames",
             "Dropped %.1f%% of frames last night (over %g%%)%s"
             % (rate, thresholds.dropped_frame_rate_warn_pct, why))

    # Catastrophic LIVE guard: a stream dumping frames right now (mid-night failure)
    # pages immediately, without waiting for the nightly summary.
    dropped = metrics.get("dropped_frames_10min") or 0
    if dropped >= thresholds.dropped_frames_catastrophic:
        cause = metrics.get("drop_cause")
        detail = metrics.get("drop_detail")
        msg = "Severe live frame loss: %d frames dropped in last 10 min" % dropped
        if cause:
            msg += " -- likely %s%s" % (cause, (" (%s)" % detail) if detail else "")
        flag(ERROR, "dropped_frames_live", msg)

    return state["status"], state["problems"]


def evaluate_host(metrics, thresholds, disabled=()):
    """Return (status, problems) for host-wide OS metrics (memory, OOM)."""
    flag, state = _flagger(disabled)

    # OOM handling, tied to the only thing that PROVES post-OOM recovery: a REBOOT.
    # oom_kill_count is counted PER BOOT (from `journalctl -k`, current boot; verified
    # against uptime on live hosts), so it means "OOM'd since the last boot and NOT
    # rebooted since". Restarting capture does NOT clear it -- and shouldn't: an OOM can
    # leave capture "up" but DEGRADED (a half-killed process capture_down won't catch),
    # so only a reboot proves the box is clean. Three tiers:
    #   * rebooted since the kill (oom_last_age_s > uptime) -> box is fresh -> CLEAR.
    #   * kill is fresh (<= oom_recent_s) -> active crisis -> ERROR (python) / degraded.
    #   * kill older but still this boot -> DEGRADED "reboot to recover" advisory that
    #     persists until the box is actually rebooted (capture may be up yet degraded).
    # Unparseable age/uptime -> flag, to be safe.
    # The monitor's own auto-update is stuck, so this station is frozen on old code and
    # will silently miss every future fix. Degraded, not error: capture is unaffected.
    blocked = metrics.get("monitor_update_blocked")
    if blocked:
        flag(DEGRADED, "update_blocked",
             "Monitor auto-update is blocked (%s) -- this station is stuck on old code"
             % str(blocked)[:120])

    oom_n = metrics.get("oom_kill_count")
    oom_age = metrics.get("oom_last_age_s")
    uptime = metrics.get("uptime_s")
    cap_age = metrics.get("capture_restart_age_s")
    if oom_n:
        # RECOVERED once the OOM-damaged capture process has been REPLACED. A capture
        # restart is enough -- a full reboot is not required (verified in the field). A
        # reboot also counts, since it restarts capture. Either way the surviving damage
        # is gone; anything still wrong afterwards is a different problem and has its own
        # check (the drop/stall/camera checks), so it must not be blamed on a stale OOM.
        restarted = (oom_age is not None and cap_age is not None and cap_age < oom_age)
        rebooted = (oom_age is not None and uptime is not None and oom_age > uptime + 60)
        if not (restarted or rebooted):
            victim = metrics.get("last_oom_victim") or "?"
            if oom_age is None or oom_age <= thresholds.oom_recent_s:
                level = ERROR if "python" in str(victim).lower() else DEGRADED
                flag(level, "oom", "OOM-killer fired %dx (last victim: %s)" % (oom_n, victim))
            else:
                flag(DEGRADED, "oom",
                     "OOM-killer fired %dx (last victim: %s) and capture has not restarted "
                     "since -- restart capture (or reboot) to clear" % (oom_n, victim))

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


def build_state(metrics, thresholds, host_name, timestamp, disabled=(), host_metrics=None,
                host_label=None):
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
    # `host` is the KEY consumers join on (the host topic's name); `host_label` is the
    # plain hostname for display. They differ because hostnames collide -- see
    # config._instance_suffix -- so the key must not be the label.
    state["host_label"] = host_label or host_name
    state["timestamp"] = timestamp
    return state


def build_host_state(metrics, thresholds, host_name, timestamp, disabled=(), host_label=None):
    status, problems = evaluate_host(metrics, thresholds, disabled)
    state = dict(metrics)
    state["status"] = status
    state["problems"] = problems
    state["host"] = host_name
    # `host` is the KEY consumers join on (the host topic's name); `host_label` is the
    # plain hostname for display. They differ because hostnames collide -- see
    # config._instance_suffix -- so the key must not be the label.
    state["host_label"] = host_label or host_name
    state["timestamp"] = timestamp
    return state
