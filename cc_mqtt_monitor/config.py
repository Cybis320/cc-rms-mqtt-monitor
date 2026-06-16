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
    upload_queue_warn: int = 50
    clock_error_warn_ms: float = 100.0
    # Dropped frames in the last 10 min before warning (a few are normal).
    dropped_frames_warn: int = 10
    # Host memory headroom (MB) before warning / erroring.
    mem_available_warn_mb: int = 800
    mem_available_error_mb: int = 300


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
    # Identifier for this host (defaults to the system hostname).
    host_name: str = None

    # Explicit subscription-group override (the installer's choice). When set, it
    # applies to every station on this host; when null, each station uses its own
    # RMS `camera_group_name`. Published as `group` (+ a slugified `group_slug`).
    group: str = None

    # Health checks to silence, by key (see health.CHECK_KEYS). Empty = all on.
    disabled_checks: list = field(default_factory=list)

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
