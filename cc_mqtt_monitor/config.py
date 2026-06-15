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
    host: str = "localhost"
    port: int = 1883
    username: str = None
    password: str = None
    tls: bool = False
    keepalive: int = 60
    client_id_prefix: str = "cc-rms-monitor"


@dataclass
class Thresholds:
    # Capture freshness: how stale the newest FITS file may get during an
    # active capture session before we warn / error (seconds).
    fits_fresh_warn_s: int = 30
    fits_fresh_error_s: int = 120
    # Frame-image freshness (continuous/daytime output). Frames save every
    # frame_save_aligned_interval (~5 s by default), so these are generous.
    frame_fresh_warn_s: int = 90
    frame_fresh_error_s: int = 300
    # Sun elevation below which night output (FF compression) is expected.
    # Matches the RMS capture horizon (-5:26).
    night_horizon_deg: float = -5.26
    # Fallback only (no station lat/lon): treat a capture session as "active"
    # if its directory was touched within this window, to avoid daytime alarms.
    capture_active_window_s: int = 3600
    # Grace period after a capture dir appears before missing detection output
    # (FTPdetectinfo / CALSTARS) is treated as a stalled-pipeline problem.
    detection_grace_s: int = 1800
    disk_free_warn_gb: float = 20.0
    disk_free_error_gb: float = 5.0
    upload_queue_warn: int = 50
    clock_error_warn_ms: float = 100.0
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
    topic_prefix: str = "contrailcast/rms"

    # Home Assistant MQTT Discovery.
    ha_discovery_enabled: bool = True
    ha_discovery_prefix: str = "homeassistant"

    interval_seconds: int = 60
    # Number of trailing log lines scanned per station per cycle.
    log_tail_lines: int = 4000
    # Identifier for this host (defaults to the system hostname).
    host_name: str = None

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
