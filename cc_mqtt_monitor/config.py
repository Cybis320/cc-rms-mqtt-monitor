"""Monitor configuration loading.

Configuration is read from a YAML file (see config.example.yaml). Every value
has a sensible default, so a minimal config only needs the broker host. A few
settings can be overridden from the environment for convenience:

    CC_MQTT_BROKER      -> broker.host
    CC_MQTT_PORT        -> broker.port
    CC_MQTT_USERNAME    -> broker.username
    CC_MQTT_PASSWORD    -> broker.password
"""

import os
import socket
from dataclasses import dataclass, field, asdict

try:
    import yaml
except ImportError:  # pragma: no cover - surfaced to the user at runtime
    yaml = None


@dataclass
class BrokerConfig:
    host: str = "mqtt.contrailcast.com"
    # Plaintext 1883 by default: the health feed carries non-sensitive, world-
    # readable telemetry and uses no credentials, so TLS would add a cert-expiry
    # outage mode without protecting anything. Turn TLS on (tls: true, port:
    # 8883) only when you add authentication or transmit sensitive data.
    port: int = 1883
    username: str = None
    password: str = None
    tls: bool = False
    keepalive: int = 60
    client_id_prefix: str = "cc-rms-monitor"


@dataclass
class Thresholds:
    # Capture liveness: error if a station that should be capturing produces no
    # output of any kind (FF file or frame image) for this long. Generous on
    # purpose so it rides through camera day/night mode switches, which pause
    # output for a bit while the camera reconfigures.
    output_fresh_error_s: int = 300
    # A non-continuous station counts as "capturing" only while its latest
    # captured directory was written within this window (tells a real stall
    # from normal daytime idle, without predicting the sun).
    capture_active_window_s: int = 3600
    # Grace period after a capture dir appears before missing detection output
    # (FTPdetectinfo / CALSTARS) is treated as a stalled-pipeline problem.
    detection_grace_s: int = 1800
    # Grace after a frame session before a missing timelapse mp4 is flagged.
    # Generous: encoding can take a long time on weak multicam machines.
    timelapse_grace_s: int = 3600
    # Max time a frame-saving station may go without ANY new timelapse mp4
    # before flagging (catches timelapses that never run at all, incl. polar).
    # Must exceed the longest normal gap between sessions (~a day).
    timelapse_max_age_s: int = 108000   # 30 h
    disk_free_warn_gb: float = 20.0
    disk_free_error_gb: float = 5.0
    # RMS queues ~4 archives per camera per night and drains them same-morning,
    # so the queue is normally 0. Backlog grows ~4/night when uploads fail; 10
    # catches that within ~2 nights while clearing the normal morning transient.
    upload_queue_warn: int = 10
    clock_error_warn_ms: float = 100.0
    # WARNING-level log lines in the scanned tail before flagging (degraded).
    # RMS warnings are rare (~a few per multi-hour log), so 1 = alert on any.
    log_warning_warn: int = 1
    # Dropped frames in the last 10 min before warning (a few are normal).
    dropped_frames_warn: int = 10
    # UDP receive-buffer overflow rate (RcvbufErrors/min, host-wide) to warn
    # ABOVE. Only evaluated when a station uses protocol: udp; alerts on the
    # growth RATE (the raw counter only climbs / resets at boot), not the total.
    # Default 0 = alert on ANY increase (max sensitivity for the initial data-
    # gathering phase); raise it (e.g. 60) once the per-host noise floor is known.
    udp_rcvbuf_errors_per_min_warn: float = 0.0
    # Host memory headroom (MB) before warning / erroring.
    mem_available_warn_mb: int = 800
    mem_available_error_mb: int = 300

    # --- Dropped-frame attribution (classify_drops) ----------------------
    # These set when a signal is "hot" for the elimination logic that pins a
    # dropped-frame burst on a cause. They gate attribution text, and which
    # causes are host-explained vs. worth an on-demand probe -- not severity.
    # Appsink buffer fill (%) that means the RMS consumer is falling behind
    # (CPU/I-O back-pressure) rather than losing frames upstream.
    buffer_fill_warn_pct: float = 70.0
    # Capture process-tree CPU% (summed; can exceed 100 on multicore) that
    # indicates the capture itself is CPU-bound.
    capture_cpu_warn_pct: float = 250.0
    # Host CPU busy% / iowait% / load-per-core that indicate host-wide pressure.
    cpu_busy_warn_pct: float = 90.0
    cpu_iowait_warn_pct: float = 20.0
    load_per_core_warn: float = 2.0
    # NIC RX error and IP-reassembly growth rates (per min) that implicate the
    # wire/NIC or fragmentation. 0 = any increase counts (like udp_rcvbuf).
    nic_rx_errors_per_min_warn: float = 0.0
    ip_reasm_fails_per_min_warn: float = 0.0
    # Decoder-error / pipeline-reconnect counts in the scanned log tail that mark
    # in-pipeline corruption (the symptom of packets lost upstream of decode).
    decoder_errors_warn: int = 1
    pipeline_reconnects_warn: int = 3
    # On-demand probe: ping packet-loss% to the camera that confirms link loss.
    ping_loss_warn_pct: float = 1.0
    # Adaptive escalation: when drops are unexplained-on-host, run a heavy probe
    # at most this often per station, backing off (doubling) up to the max while
    # the camera stays bad -- so a persistently-bad stream is never re-hammered.
    probe_min_interval_s: int = 600
    probe_max_interval_s: int = 3600


@dataclass
class Config:
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    thresholds: Thresholds = field(default_factory=Thresholds)

    # Where multicam per-station RMS configs live; each <dir>/*/.config defines a
    # station. If empty, we fall back to the single-cam config in rms_dir.
    stations_dir: str = "~/source/Stations"
    # Single-cam install: the RMS checkout whose own .config IS the station.
    # On a multicam box this same file is only the template, so it is ignored
    # whenever stations_dir yields at least one station.
    rms_dir: str = "~/source/RMS"

    # Topic layout. Plain state is published to "<topic_prefix>/<station>/health".
    # The contrailcast broker ACL only permits the "stations/#" namespace, so
    # everything (state + host status/LWT) must live under it.
    topic_prefix: str = "stations"

    interval_seconds: int = 60
    # Number of trailing log lines scanned per station per cycle.
    log_tail_lines: int = 4000

    # Maintenance detection: mark records "expected disruption" so the bridge
    # suppresses alerts for them. Host just rebooted (uptime below this), or an
    # RMS updater process is actually running. Self-healing: a lingering lock/
    # flag file never implies maintenance on its own and is cleaned up.
    boot_grace_s: int = 600
    # Sane RMS-update window: an updater process or lock older than this is
    # treated as stale (a real update completes well within 15 min).
    maintenance_file_max_age_s: int = 900
    maintenance_file: str = None          # optional GRMSUpdater lock to clean up
    # Identifier for this host (defaults to the system hostname).
    host_name: str = None

    # Explicit subscription-group override (the installer's choice). When set, it
    # applies to every station on this host; when null, each station uses its own
    # RMS `camera_group_name`. Published as `group` (+ a slugified `group_slug`).
    group: str = None

    # Health checks to silence, by key (see health.CHECK_KEYS). Empty = all on.
    disabled_checks: list = field(default_factory=list)

    # Let the monitor self-escalate to a heavy on-demand probe (ffprobe keyframe
    # peak + ping loss) when a station drops frames the cheap host signals can't
    # explain. Off => only the manual `--diagnose` runs the heavy probes. The
    # probe is still rate-limited/backed-off per station (see thresholds).
    enable_adaptive_probe: bool = True

    # Extra regex patterns of WARNING-level log lines to NOT alert on, added to
    # the built-in benign defaults (ExtractStars star-cap, numpy/scipy warnings,
    # observation-summary lock race). See collect._DEFAULT_WARNING_IGNORE.
    log_warning_ignore: list = field(default_factory=list)

    def __post_init__(self):
        if not self.host_name:
            self.host_name = socket.gethostname()
        self.stations_dir = os.path.expanduser(self.stations_dir)
        self.rms_dir = os.path.expanduser(self.rms_dir)

    def as_dict(self):
        return asdict(self)


def _coerce(dataclass_obj, data):
    """Apply a dict of overrides onto a dataclass instance, ignoring unknowns."""
    for key, value in (data or {}).items():
        if hasattr(dataclass_obj, key):
            setattr(dataclass_obj, key, value)


def load_config(path=None):
    """Load configuration from a YAML file, or return defaults if path is None."""
    data = {}
    if path:
        if yaml is None:
            raise RuntimeError(
                "PyYAML is required to read a config file. Install it with "
                "'pip install pyyaml', or run without --config to use defaults."
            )
        with open(os.path.expanduser(path)) as fh:
            data = yaml.safe_load(fh) or {}

    broker = BrokerConfig()
    _coerce(broker, data.pop("broker", {}))

    thresholds = Thresholds()
    _coerce(thresholds, data.pop("thresholds", {}))

    cfg = Config(broker=broker, thresholds=thresholds)
    _coerce(cfg, data)

    # Environment overrides (handy for systemd drop-ins / containers).
    cfg.broker.host = os.environ.get("CC_MQTT_BROKER", cfg.broker.host)
    if os.environ.get("CC_MQTT_PORT"):
        cfg.broker.port = int(os.environ["CC_MQTT_PORT"])
    cfg.broker.username = os.environ.get("CC_MQTT_USERNAME", cfg.broker.username)
    cfg.broker.password = os.environ.get("CC_MQTT_PASSWORD", cfg.broker.password)

    cfg.__post_init__()
    return cfg
