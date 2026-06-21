"""On-demand deep probes for dropped-frame attribution.

The cheap per-cycle collectors (collect.py / oslevel.py) fill the elimination
matrix; these heavier probes CONFIRM the verdict when it matters. They decode a
recent segment to measure the keyframe burst, and ping the camera to measure
link loss -- both too costly to run every cycle (especially on a Pi). They run
either on demand (`--diagnose`) or when the monitor self-escalates: real drops
that the cheap host signals couldn't explain (see should_escalate), where the
cost/benefit finally tilts toward spending a probe.

Every probe is best-effort: a missing ffprobe/ping, an unreachable camera, or a
timeout yields nulls + a note, never an exception. Nothing here writes to the
camera or the RMS data; it only reads a saved segment and sends ICMP.
"""

import time
import shutil
import subprocess

from .collect import newest_segment
from . import health


def _have(tool):
    return shutil.which(tool) is not None


# ---------------------------------------------------------------------------
# Keyframe / bitrate probe (decode-accurate; the camera-bandwidth confirmation)
# ---------------------------------------------------------------------------


def probe_keyframe(station, now=None, timeout=30):
    """Per-frame sizes of the newest finalized segment via ffprobe.

    Returns the keyframe (peak) frame size and mean, in KB, plus the implied
    Mbps. A keyframe peak that climbs into the loss band while host signals stay
    clean is the camera/link-bandwidth confirmation. Null (+ note) when ffprobe
    is absent, no segment exists, or the probe times out.
    """
    result = {"probe_keyframe_peak_kb": None, "probe_keyframe_mean_kb": None,
              "probe_stream_mbps": None, "probe_keyframe_note": None}
    if not _have("ffprobe"):
        result["probe_keyframe_note"] = "ffprobe not installed"
        return result
    seg = newest_segment(station, now)
    if seg is None:
        result["probe_keyframe_note"] = "no video segment (raw_video_save off?)"
        return result
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v",
             "-show_entries", "frame=pkt_size", "-of", "csv=p=0", seg],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout, universal_newlines=True)
    except (OSError, subprocess.SubprocessError):
        result["probe_keyframe_note"] = "ffprobe failed/timed out"
        return result

    sizes = []
    for tok in (out.stdout or "").split():
        try:
            sizes.append(int(tok))
        except ValueError:
            continue
    if not sizes:
        result["probe_keyframe_note"] = "no frame sizes returned"
        return result

    dur = station.raw_video_duration or 30.0
    result["probe_keyframe_peak_kb"] = round(max(sizes) / 1e3, 1)
    result["probe_keyframe_mean_kb"] = round(sum(sizes) / len(sizes) / 1e3, 1)
    result["probe_stream_mbps"] = round(sum(sizes) * 8 / dur / 1e6, 1)
    return result


# ---------------------------------------------------------------------------
# Network probe (camera reachability / sustained loss)
# ---------------------------------------------------------------------------

# NOTE: ping measures SUSTAINED loss/latency. It cannot see the sub-millisecond
# keyframe microburst (paced ICMP isn't a line-rate burst), so a clean ping does
# NOT exonerate the link -- it only flags a link that's lossy even when idle.
def probe_network(station, count=20, interval=0.05, timeout=15):
    """ping the camera host: packet loss% and RTT (avg/max/jitter), best-effort."""
    result = {"probe_ping_loss_pct": None, "probe_ping_rtt_avg_ms": None,
              "probe_ping_rtt_max_ms": None, "probe_ping_note": None}
    host = station.camera_host
    if not host:
        result["probe_ping_note"] = "no camera host in device URL"
        return result
    if not _have("ping"):
        result["probe_ping_note"] = "ping not installed"
        return result
    try:
        out = subprocess.run(
            ["ping", "-n", "-c", str(count), "-i", str(interval), "-W", "1", host],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout, universal_newlines=True)
    except (OSError, subprocess.SubprocessError):
        result["probe_ping_note"] = "ping failed/timed out"
        return result

    for line in (out.stdout or "").splitlines():
        if "packet loss" in line:
            for tok in line.replace(",", " ").split():
                if tok.endswith("%"):
                    try:
                        result["probe_ping_loss_pct"] = float(tok[:-1])
                    except ValueError:
                        pass
                    break
        # "rtt min/avg/max/mdev = 0.3/0.6/2.6/0.4 ms"
        if "/" in line and ("rtt" in line or "round-trip" in line):
            try:
                stats = line.split("=", 1)[1].strip().split()[0].split("/")
                result["probe_ping_rtt_avg_ms"] = float(stats[1])
                result["probe_ping_rtt_max_ms"] = float(stats[2])
            except (IndexError, ValueError):
                pass
    if result["probe_ping_loss_pct"] is None and result["probe_ping_note"] is None:
        result["probe_ping_note"] = "could not parse ping output"
    return result


def run_probe(station, now=None):
    """Run every heavy probe for a station and merge the results into one dict."""
    result = {}
    result.update(probe_keyframe(station, now))
    result.update(probe_network(station))
    return result


# ---------------------------------------------------------------------------
# Adaptive escalation: decide WHEN a probe is worth its cost
# ---------------------------------------------------------------------------

# Per-station escalation state: {station_id: {"next_t": monotonic, "interval"}}.
# The interval doubles each time a probe runs while the station is still bad
# (so a persistently-bad camera is confirmed once, then backed off, not hammered)
# and resets when drops clear.
_ESCALATE = {}

# Causes that the cheap host signals already explain -- probing adds nothing.
_HOST_EXPLAINED = frozenset({
    health.CAUSE_BACKPRESSURE, health.CAUSE_UDP_BUFFER,
    health.CAUSE_NIC, health.CAUSE_IP_FRAG, health.CAUSE_LINK_LOSS,
})


def should_escalate(station, metrics, host_metrics, thresholds, now=None):
    """True if a station's drops warrant spending a heavy probe right now.

    The cost/benefit tilts toward probing only when (a) frames are actually
    dropping past the warn threshold, AND (b) the cheap classifier could NOT
    pin it on a host cause -- i.e. the camera/link-bandwidth or uncertain case,
    which is exactly what a keyframe/ping probe confirms. A per-station backoff
    timer then bounds how often we re-confirm a camera that stays bad.
    """
    dropped = metrics.get("dropped_frames_10min") or 0
    if dropped < thresholds.dropped_frames_warn:
        _ESCALATE.pop(station.station_id, None)   # recovered: reset backoff
        return False

    cause = classify(metrics, host_metrics, thresholds).get("drop_cause")
    if cause in _HOST_EXPLAINED:
        return False   # cheap signal already nailed it; a probe wouldn't add

    now = now or time.monotonic()
    st = _ESCALATE.get(station.station_id)
    if st is not None and now < st["next_t"]:
        return False
    return True


def note_escalation(station, thresholds, now=None):
    """Record that a probe just ran, advancing the per-station backoff timer."""
    now = now or time.monotonic()
    st = _ESCALATE.get(station.station_id)
    interval = thresholds.probe_min_interval_s if st is None else min(
        st["interval"] * 2, thresholds.probe_max_interval_s)
    _ESCALATE[station.station_id] = {"next_t": now + interval, "interval": interval}


def classify(metrics, host_metrics, thresholds):
    """Thin alias so callers don't reach across modules for the classifier."""
    return health.classify_drops(metrics, host_metrics, thresholds)
